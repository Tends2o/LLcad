import test from "node:test";
import assert from "node:assert/strict";
import { setup, call, finish, importFixture, principal } from "../helpers.js";
import { sphere, housing, organic } from "../../scripts/fixtures.js";
import { id, hash } from "../../packages/semantic-ir/hash.js";
import { SCOPES } from "../../packages/policy/index.js";
const foreign = { tenant: "tenant-b", user: "bob", scopes: SCOPES };
const patch = (m: any, radius = "11") => ({
  model_id: m.model_id,
  base_revision: m.revision,
  idempotency_key: id("patch"),
  operations: [
    {
      op: "set_parameter",
      feature_id: "sphere",
      parameter: "radius",
      expected: { value: "10", unit: "mm" },
      value: { value: radius, unit: "mm" },
    },
  ],
});
test("model, job, selection, resource and artifact ACLs reject foreign principals", async () => {
  const env = setup(),
    s = env.service;
  try {
    const m = await importFixture(s, sphere);
    const draft = call(s, "cad_apply_patch", patch(m));
    assert.equal(
      s.call(foreign, "cad_get_model", { model_id: m.model_id }).errors[0].code,
      "ACCESS_DENIED",
    );
    assert.equal(
      s.call(foreign, "cad_job_get", { job_id: draft.job_id }).errors[0].code,
      "ACCESS_DENIED",
    );
    const a = s.store.artifact(
      principal,
      "private",
      "text/plain",
      m.model_id,
      m.revision,
      {},
    );
    assert.throws(
      () => s.store.getArtifact(foreign, a.artifact_id),
      (e: any) => e.code === "ACCESS_DENIED",
    );
    assert.throws(
      () =>
        s.resource(
          foreign,
          `cad://models/${m.model_id}/revisions/${m.revision}/summary`,
        ),
      (e: any) => e.code === "ACCESS_DENIED",
    );
    const sameTenant = { ...foreign, tenant: principal.tenant };
    assert.equal(
      s.call(sameTenant, "cad_get_model", { model_id: m.model_id }).errors[0]
        .code,
      "ACCESS_DENIED",
    );
    await finish(s, draft);
  } finally {
    await env.close();
  }
});
test("concurrent duplicates execute once; changed payload conflicts; stale candidate cannot commit", async () => {
  const env = setup(),
    s = env.service;
  try {
    const m = await importFixture(s, sphere);
    const request = patch(m);
    const responses = await Promise.all(
      Array.from({ length: 12 }, async () =>
        call(s, "cad_apply_patch", request),
      ),
    );
    assert.equal(new Set(responses.map((x) => x.job_id)).size, 1);
    assert.equal(
      s.call(principal, "cad_apply_patch", {
        ...request,
        operations: patch(m, "12").operations,
      }).errors[0].code,
      "IDEMPOTENCY_CONFLICT",
    );
    const other = call(s, "cad_apply_patch", patch(m, "12"));
    const a = responses[0];
    await finish(s, a);
    await finish(s, other);
    const proofA = await finish(
      s,
      call(s, "cad_validate", {
        model_id: m.model_id,
        base_revision: m.revision,
        transaction_id: a.transaction_id,
        idempotency_key: id("validate"),
      }),
    );
    const proofB = await finish(
      s,
      call(s, "cad_validate", {
        model_id: m.model_id,
        base_revision: m.revision,
        transaction_id: other.transaction_id,
        idempotency_key: id("validate"),
      }),
    );
    const commit = {
      model_id: m.model_id,
      base_revision: m.revision,
      transaction_id: a.transaction_id,
      validation_digest: proofA.digest,
      idempotency_key: id("commit"),
    };
    const done = call(s, "cad_commit", commit);
    assert.equal(call(s, "cad_commit", commit).revision, done.revision);
    assert.equal(
      s.call(principal, "cad_commit", {
        ...commit,
        transaction_id: other.transaction_id,
        validation_digest: proofB.digest,
        idempotency_key: id("stale"),
      }).errors[0].code,
      "STALE_REVISION",
    );
    assert.equal(
      s.store.get(
        "SELECT COUNT(*) AS n FROM revisions WHERE model=? AND quality=?",
        m.model_id,
        "checks_passed_within_profile",
      ).n,
      2,
    );
  } finally {
    await env.close();
  }
});
test("missing hook, missing commit scope and mismatched proof all fail closed", async () => {
  const env = setup(),
    s = env.service;
  try {
    const m = await importFixture(s, sphere);
    const draft = call(s, "cad_apply_patch", patch(m));
    await finish(s, draft);
    const v = await finish(
      s,
      call(s, "cad_validate", {
        model_id: m.model_id,
        base_revision: m.revision,
        transaction_id: draft.transaction_id,
        idempotency_key: id("validate"),
      }),
    );
    const c = {
      model_id: m.model_id,
      base_revision: m.revision,
      transaction_id: draft.transaction_id,
      validation_digest: v.digest,
      idempotency_key: id("commit"),
    };
    assert.equal(
      s.call({ ...principal, scopes: ["model:read"] }, "cad_commit", c)
        .errors[0].code,
      "ACCESS_DENIED",
    );
    assert.equal(
      s.call(principal, "cad_commit", {
        ...c,
        validation_digest: "0".repeat(64),
      }).errors[0].code,
      "VALIDATION_REQUIRED",
    );
    s.gates.disableForTest("before_commit");
    assert.equal(
      s.call(principal, "cad_commit", c).errors[0].code,
      "POLICY_GATE_FAILED",
    );
    assert.equal(
      call(s, "cad_get_model", { model_id: m.model_id }).revision,
      m.revision,
    );
  } finally {
    await env.close();
  }
});
test("expired or foreign selection handles never rebind silently", async () => {
  const env = setup(),
    s = env.service;
  try {
    const m = await importFixture(s, sphere);
    const found = call(s, "cad_find", { model_id: m.model_id, query: "Kugel" });
    const handle = found.matches[0].selection_handle;
    s.store.run("UPDATE selections SET expires=0 WHERE id=?", handle);
    assert.equal(
      s.call(principal, "cad_inspect", {
        model_id: m.model_id,
        selection_handle: handle,
      }).errors[0].code,
      "STALE_REVISION",
    );
    assert.equal(
      s.call(principal, "cad_inspect", { model_id: m.model_id }).errors[0].code,
      "AMBIGUOUS_SELECTION",
    );
  } finally {
    await env.close();
  }
});
test("cancellation and discard fence late worker results", async () => {
  const env = setup(),
    s = env.service;
  try {
    const m = await importFixture(s, sphere);
    const a = call(s, "cad_apply_patch", patch(m));
    call(s, "cad_job_cancel", {
      job_id: a.job_id,
      idempotency_key: id("cancel"),
    });
    assert.equal(s.jobs.get(principal, a.job_id).status, "cancelled");
    await new Promise((r) => setTimeout(r, 100));
    assert.equal(
      call(s, "cad_get_model", { model_id: m.model_id }).revision,
      m.revision,
    );
    const b = call(s, "cad_apply_patch", patch(m));
    await finish(s, b);
    call(s, "cad_discard", {
      model_id: m.model_id,
      base_revision: m.revision,
      transaction_id: b.transaction_id,
      idempotency_key: id("discard"),
    });
    assert.equal(
      s.call(principal, "cad_validate", {
        model_id: m.model_id,
        base_revision: m.revision,
        transaction_id: b.transaction_id,
        idempotency_key: id("validation"),
      }).errors[0].code,
      "CONSTRAINT_CONFLICT",
    );
  } finally {
    await env.close();
  }
});
test("local field edit proves entire protected remote region, overlap blocks validation", async () => {
  const env = setup(),
    s = env.service;
  try {
    const m = await importFixture(s, organic);
    const source = (organic.features[0].construction as any).expression;
    const make = (radius: string) => ({
      model_id: m.model_id,
      base_revision: m.revision,
      idempotency_key: id("field-edit"),
      operations: [
        {
          op: "set_field",
          feature_id: "organic",
          expected_hash: hash(source),
          expression: {
            op: "local_field_delta",
            source,
            center: ["5", "0", "0"],
            radius,
            amplitude: "0.02",
          },
        },
      ],
    });
    for (const [radius, status] of [
      ["1", "checks_passed_within_profile"],
      ["10", "failed"],
    ]) {
      const a = call(s, "cad_apply_patch", make(radius));
      await finish(s, a);
      const report = await finish(
        s,
        call(s, "cad_validate", {
          model_id: m.model_id,
          base_revision: m.revision,
          transaction_id: a.transaction_id,
          idempotency_key: id("validate"),
        }),
      );
      assert.equal(report.status, status);
    }
  } finally {
    await env.close();
  }
});
test("imported IR cannot smuggle another user’s native artifact", async () => {
  const env = setup(),
    s = env.service;
  try {
    const privateArtifact = s.store.artifact(
      foreign,
      "secret",
      "application/octet-stream",
      null,
      null,
      {},
    );
    const m = call(s, "cad_create_model", {
      name: "Import",
      idempotency_key: id("create"),
    });
    const ir = {
      schema_version: "1",
      unit: "mm",
      features: [
        {
          id: "import",
          semantic_name: "smuggled",
          kind: "imported",
          parameters: {},
          construction: {
            operator: "imported",
            artifact_id: privateArtifact.artifact_id,
            format: "step",
            source_unit: "mm",
          },
        },
      ],
      outputs: ["import"],
    };
    const a = s.store.artifact(
      principal,
      JSON.stringify(ir),
      "application/json",
      m.model_id,
      m.revision,
      {},
    );
    assert.equal(
      s.call(principal, "cad_import", {
        model_id: m.model_id,
        base_revision: m.revision,
        artifact_id: a.artifact_id,
        format: "ir",
        source_unit: "mm",
        idempotency_key: id("import"),
      }).errors[0].code,
      "ACCESS_DENIED",
    );
  } finally {
    await env.close();
  }
});
test("requested dimensions are checked even without user-authored constraints", async () => {
  const env = setup(),
    s = env.service;
  try {
    const m = await importFixture(s, sphere),
      a = call(s, "cad_apply_patch", patch(m));
    await finish(s, a);
    const tx = s.store.transaction(principal, a.transaction_id);
    tx.result.facts.sphere.dimensions.radius = 7;
    s.store.run(
      "UPDATE transactions SET result=? WHERE id=?",
      JSON.stringify(tx.result),
      tx.id,
    );
    const v = await finish(
      s,
      call(s, "cad_validate", {
        model_id: m.model_id,
        base_revision: m.revision,
        transaction_id: tx.id,
        idempotency_key: id("validate"),
      }),
    );
    assert.equal(v.status, "failed");
  } finally {
    await env.close();
  }
});
