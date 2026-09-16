import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { AjvJsonSchemaValidator } from "@modelcontextprotocol/sdk/validation/ajv";
import { setup, call, finish, importFixture, principal } from "../helpers.js";
import { sphere } from "../../scripts/fixtures.js";
import { hash, id } from "../../packages/semantic-ir/hash.js";
import { ToolSchemas, ToolName } from "../../packages/semantic-ir/schema.js";
import {
  FailureResponse,
  outputJSONSchema,
  ValidationReport,
} from "../../packages/semantic-ir/results.js";
import {
  assertValidation,
  assertJobResult,
} from "../../packages/model-service/result-contracts.js";
import { Worker } from "../../packages/job-service/worker.js";

function rows(s: ReturnType<typeof setup>["service"]) {
  return Object.fromEntries(
    [
      "models",
      "revisions",
      "transactions",
      "idempotency",
      "outbox",
      "audit",
      "selections",
      "cache",
      "artifacts",
    ].map((table) => [table, s.store.all("SELECT * FROM " + table)]),
  );
}
function intercept(
  s: ReturnType<typeof setup>["service"],
  tool: ToolName,
  alter: (result: any) => void,
) {
  const original = s.dispatch;
  s.dispatch = function (p, name, input) {
    const result = original.call(this, p, name, input);
    if (name === tool) alter(result);
    return result;
  };
  return () => {
    s.dispatch = original;
  };
}
test("all 23 advertised output schemas match generated artifacts and reject incomplete successes", async () => {
  const env = setup();
  const validator = new AjvJsonSchemaValidator();
  try {
    for (const name of Object.keys(ToolSchemas) as ToolName[]) {
      const schema = outputJSONSchema(name);
      assert.deepEqual(
        JSON.parse(readFileSync(`schemas/${name}.result.schema.json`, "utf8")),
        schema,
      );
      const check = validator.getValidator(JSON.parse(JSON.stringify(schema)));
      assert.equal(
        check({ status: "ok", errors: [], trace_id: id("trace") }).valid,
        false,
        name,
      );
      const failed = env.service.call(principal, name, {
        unregistered_input: true,
      });
      assert.equal(failed.status, "failed", name);
      assert.ok(FailureResponse.safeParse(failed).success, name);
      assert.ok(check(failed).valid, name);
    }
  } finally {
    await env.close();
  }
});

test("invalid, nonfinite, oversized and wrongly bound replies roll back writes and idempotency", async () => {
  const env = setup(),
    s = env.service;
  try {
    for (const alter of [
      (r: any) => {
        delete r.revision;
      },
      (r: any) => {
        r.extra = "private-invalid-payload";
      },
      (r: any) => {
        r.measurements = Infinity;
      },
      (r: any) => {
        r.warnings = ["private-invalid-payload".repeat(4000)];
      },
    ]) {
      const before = rows(s),
        request = { name: "Atomic reply", idempotency_key: id("create") };
      const restore = intercept(s, "cad_create_model", alter);
      const result = s.call(principal, "cad_create_model", request);
      restore();
      assert.equal(result.status, "failed");
      assert.equal(result.committed, false);
      assert.ok(
        ["OUTPUT_CONTRACT_VIOLATION", "BUDGET_EXCEEDED"].includes(
          result.errors[0].code,
        ),
      );
      assert.ok(!JSON.stringify(result).includes("private-invalid-payload"));
      // before_request audit is intentionally outside the write transaction.
      const after = rows(s);
      delete before.audit;
      delete after.audit;
      assert.deepEqual(after, before);
      const created = call(s, "cad_create_model", request);
      assert.equal(
        call(s, "cad_create_model", request).revision,
        created.revision,
      );
    }
    const m = await importFixture(s, sphere);
    const before = rows(s);
    let restore = intercept(s, "cad_find", (r) => {
      r.matches[0].reason = "x".repeat(40000);
    });
    assert.equal(
      s.call(principal, "cad_find", { model_id: m.model_id, query: "Kugel" })
        .errors[0].code,
      "BUDGET_EXCEEDED",
    );
    restore();
    assert.deepEqual(rows(s).selections, before.selections);
    restore = intercept(s, "cad_get_model", (r) => {
      r.model_id = "foreign-model";
    });
    assert.equal(
      s.call(principal, "cad_get_model", { model_id: m.model_id }).errors[0]
        .code,
      "OUTPUT_CONTRACT_VIOLATION",
    );
    restore();
  } finally {
    await env.close();
  }
});

test("malformed commit replies preserve the head, validated candidate, outbox and retry key", async () => {
  const env = setup(),
    s = env.service;
  try {
    const m = await importFixture(s, sphere);
    const patch = call(s, "cad_apply_patch", {
      model_id: m.model_id,
      base_revision: m.revision,
      idempotency_key: id("patch"),
      operations: [
        {
          op: "set_parameter",
          feature_id: "sphere",
          parameter: "radius",
          expected: { value: "10", unit: "mm" },
          value: { value: "11", unit: "mm" },
        },
      ],
    });
    await finish(s, patch);
    const validation = await finish(
      s,
      call(s, "cad_validate", {
        model_id: m.model_id,
        base_revision: m.revision,
        transaction_id: patch.transaction_id,
        idempotency_key: id("validate"),
      }),
    );
    const request = {
      model_id: m.model_id,
      base_revision: m.revision,
      transaction_id: patch.transaction_id,
      validation_digest: validation.digest,
      idempotency_key: id("commit"),
    };
    const before = rows(s);
    const restore = intercept(s, "cad_commit", (r) => {
      delete r.validation_digest;
    });
    const rejected = s.call(principal, "cad_commit", request);
    restore();
    assert.equal(rejected.errors[0].code, "OUTPUT_CONTRACT_VIOLATION");
    const after = rows(s);
    delete before.audit;
    delete after.audit;
    assert.deepEqual(after, before);
    assert.equal(s.store.model(principal, m.model_id).head, m.revision);
    const committed = call(s, "cad_commit", request);
    assert.notEqual(committed.revision, m.revision);
    const stored = s.store.get(
      "SELECT result FROM idempotency WHERE key=?",
      request.idempotency_key,
    ).result;
    const corrupt = JSON.parse(stored);
    delete corrupt.revision;
    s.store.run(
      "UPDATE idempotency SET result=? WHERE key=?",
      JSON.stringify(corrupt),
      request.idempotency_key,
    );
    assert.equal(
      s.call(principal, "cad_commit", request).errors[0].code,
      "OUTPUT_CONTRACT_VIOLATION",
    );
    assert.equal(s.store.model(principal, m.model_id).head, committed.revision);
    // Reading an old replay neither repairs nor rewrites its immutable response.
    assert.equal(
      s.store.get(
        "SELECT result FROM idempotency WHERE key=?",
        request.idempotency_key,
      ).result,
      JSON.stringify(corrupt),
    );
    s.store.run(
      "UPDATE idempotency SET result=? WHERE key=?",
      stored,
      request.idempotency_key,
    );
    const replay = call(s, "cad_commit", request);
    assert.equal(replay.revision, committed.revision);
    assert.notEqual(replay.trace_id, committed.trace_id);
  } finally {
    await env.close();
  }
});

test("worker cancellation runs only after a valid reply commits", async () => {
  const env = setup(),
    s = env.service;
  let cancellations = 0;
  try {
    const m = call(s, "cad_create_model", {
      name: "Cancel",
      idempotency_key: id("create"),
    });
    const queued = call(s, "cad_apply_patch", {
      model_id: m.model_id,
      base_revision: m.revision,
      idempotency_key: id("patch"),
      operations: [
        { op: "add_feature", feature: sphere.features[0] },
        { op: "set_outputs", outputs: ["sphere"] },
      ],
    });
    // No event-loop yield: the actual queued worker has not started.
    (s.jobs as any).activeID = queued.job_id;
    (s.jobs as any).active = {
      cancel() {
        cancellations++;
      },
    };
    const request = { job_id: queued.job_id, idempotency_key: id("cancel") };
    const restore = intercept(s, "cad_job_cancel", (r) => {
      r.unregistered = true;
    });
    assert.equal(
      s.call(principal, "cad_job_cancel", request).errors[0].code,
      "OUTPUT_CONTRACT_VIOLATION",
    );
    restore();
    assert.equal(cancellations, 0);
    assert.equal(s.jobs.get(principal, queued.job_id).status, "queued");
    assert.equal(call(s, "cad_job_cancel", request).status, "cancelled");
    assert.equal(cancellations, 1);
    assert.equal(call(s, "cad_job_cancel", request).status, "cancelled");
    assert.equal(cancellations, 1);
  } finally {
    (s.jobs as any).active = null;
    (s.jobs as any).activeID = null;
    await env.close();
  }
});

test("invalid native job results cannot publish a candidate or cache", async () => {
  const env = setup(),
    s = env.service;
  const run = Worker.prototype.run;
  try {
    Worker.prototype.run = async function (...args) {
      const result = await run.apply(this, args);
      result.facts.sphere.dimensions.radius = NaN;
      return result;
    };
    const m = call(s, "cad_create_model", {
      name: "Worker contract",
      idempotency_key: id("create"),
    });
    const before = rows(s);
    const queued = call(s, "cad_apply_patch", {
      model_id: m.model_id,
      base_revision: m.revision,
      idempotency_key: id("patch"),
      operations: [
        { op: "add_feature", feature: sphere.features[0] },
        { op: "set_outputs", outputs: ["sphere"] },
      ],
    });
    const failed = await s.jobs.wait(principal, queued.job_id);
    assert.equal(failed.status, "failed");
    assert.equal(failed.error.code, "OUTPUT_CONTRACT_VIOLATION");
    assert.deepEqual(rows(s).revisions, before.revisions);
    assert.deepEqual(rows(s).cache, before.cache);
    assert.equal(
      s.store.transaction(principal, queued.transaction_id).state,
      "failed",
    );
    const response = s.call(principal, "cad_job_get", {
      job_id: queued.job_id,
    });
    assert.equal(response.errors[0].code, "OUTPUT_CONTRACT_VIOLATION");
    assert.ok(
      new AjvJsonSchemaValidator().getValidator(
        JSON.parse(JSON.stringify(outputJSONSchema("cad_job_get"))),
      )(response).valid,
    );
  } finally {
    Worker.prototype.run = run;
    await env.close();
  }
});

test("full validation resources preserve their digest and reject malformed or contradictory proofs", async () => {
  const env = setup(),
    s = env.service;
  try {
    const m = await importFixture(s, sphere);
    const tx = s.store.get(
      "SELECT * FROM transactions WHERE committed_revision=?",
      m.revision,
    );
    const uri = `cad://transactions/${tx.id}/validation`;
    const report = s.resource(principal, uri);
    assert.deepEqual(report, m.validation);
    assert.equal(report.schema_version, "2");
    const binding = {
      candidate: tx.candidate,
      ir_hash: hash(JSON.parse(tx.plan).ir),
      facts: JSON.parse(tx.result).facts,
    };
    const legacy = structuredClone(report);
    delete legacy.schema_version;
    delete legacy.error_budget;
    for (const check of legacy.checks) {
      delete check.revision;
      delete check.engine_build;
      delete check.source_geometry_hash;
    }
    const { digest: oldDigest, ...legacyBody } = legacy;
    legacy.digest = hash(legacyBody);
    s.store.run(
      "UPDATE transactions SET validation=? WHERE id=?",
      JSON.stringify(legacy),
      tx.id,
    );
    assert.deepEqual(s.resource(principal, uri), legacy);
    s.store.run(
      "UPDATE transactions SET validation=? WHERE id=?",
      tx.validation,
      tx.id,
    );
    assert.ok(ValidationReport.safeParse(report).success);
    assert.ok(
      report.checks.some(
        (c: any) =>
          c.guarantee ===
          "reported_kernel_tolerances_not_a_global_surface_error_certificate",
      ),
    );
    const validator = new AjvJsonSchemaValidator().getValidator(
      JSON.parse(readFileSync("schemas/validation-result.schema.json", "utf8")),
    );
    assert.ok(validator(report).valid);
    const job = s.store.get(
      "SELECT id FROM jobs WHERE tx=? AND kind='validate'",
      tx.id,
    );
    const summary = call(s, "cad_job_get", { job_id: job.id }).result;
    assert.equal(summary.digest, report.digest);
    assert.equal(summary.check_count, report.checks.length);
    assert.deepEqual(summary.checks, []);
    assert.equal(summary.validation_uri, uri);
    for (const mutate of [
      (r: any) => {
        r.checks[0].guarantee = "globally_certified";
      },
      (r: any) => {
        r.checks[0].status = "failed";
      },
      (r: any) => {
        r.checks[0].measured = Infinity;
      },
      (r: any) => {
        delete r.warnings;
      },
      (r: any) => {
        r.checks[0].revision = "foreign-candidate";
      },
      (r: any) => {
        r.checks[0].engine_build = "foreign-engine";
      },
      (r: any) => {
        r.checks[0].source_geometry_hash = "0".repeat(64);
      },
    ]) {
      const altered = structuredClone(report);
      mutate(altered);
      const { digest, ...body } = altered;
      altered.digest = hash(body);
      assert.throws(() => assertValidation(altered, binding));
      // Persist JSON-compatible corruption and exercise the authorized resource.
      s.store.run(
        "UPDATE transactions SET validation=? WHERE id=?",
        JSON.stringify(altered),
        tx.id,
      );
      if (
        Number.isFinite(altered.checks[0].measured) ||
        altered.checks[0].measured !== Infinity
      )
        assert.throws(() => s.resource(principal, uri));
      s.store.run(
        "UPDATE transactions SET validation=? WHERE id=?",
        tx.validation,
        tx.id,
      );
    }
    assert.deepEqual(s.resource(principal, uri), report);
  } finally {
    await env.close();
  }
});

test("legacy export jobs remain readable while new export results require a package manifest", async () => {
  const env = setup(),
    s = env.service;
  try {
    const m = await importFixture(s, sphere);
    const queued = call(s, "cad_export", {
      model_id: m.model_id,
      revision: m.revision,
      format: "step",
      idempotency_key: id("export"),
    });
    const exported = await finish(s, queued);
    assert.equal(exported.result_schema_version, "1");
    const withoutManifest = structuredClone(exported);
    delete withoutManifest.package_manifest;
    assert.throws(() => assertJobResult("export", withoutManifest));
    s.store.run(
      "UPDATE jobs SET result=? WHERE id=?",
      JSON.stringify(withoutManifest),
      queued.job_id,
    );
    assert.equal(
      s.call(principal, "cad_job_get", { job_id: queued.job_id }).errors[0]
        .code,
      "OUTPUT_CONTRACT_VIOLATION",
    );
    const legacy = structuredClone(withoutManifest);
    delete legacy.result_schema_version;
    assert.throws(() => assertJobResult("export", legacy));
    const saved = JSON.stringify(legacy);
    s.store.run("UPDATE jobs SET result=? WHERE id=?", saved, queued.job_id);
    assert.deepEqual(
      call(s, "cad_job_get", { job_id: queued.job_id }).result,
      legacy,
    );
    assert.equal(
      s.store.get("SELECT result FROM jobs WHERE id=?", queued.job_id).result,
      saved,
    );
    s.store.run(
      "UPDATE jobs SET result=? WHERE id=?",
      JSON.stringify(exported),
      queued.job_id,
    );
    assert.deepEqual(
      call(s, "cad_job_get", { job_id: queued.job_id }).result,
      exported,
    );
  } finally {
    await env.close();
  }
});
