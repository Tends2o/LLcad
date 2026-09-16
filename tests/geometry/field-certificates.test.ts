import test from "node:test";
import assert from "node:assert/strict";
import { setup, call, finish, importFixture, principal } from "../helpers.js";
import { organic } from "../../scripts/fixtures.js";
import { id, hash } from "../../packages/semantic-ir/hash.js";

const q = (value: string) => ({ value, unit: "mm" });
const source = (organic.features[0].construction as any).expression;
const withLimit = {
  ...organic,
  constraints: [
    ...organic.constraints,
    {
      id: "deviation-limit",
      kind: "surface_deviation",
      feature_id: "organic",
      maximum: q("0.05"),
    },
  ],
};

test("a surface_deviation constraint is certified by interval arithmetic for compact edits and refuses larger global changes", async () => {
  const env = setup(),
    s = env.service;
  try {
    const model = await importFixture(s, withLimit);
    const change = async (revision: string, before: any, after: any) => {
      const draft = call(s, "cad_apply_patch", {
        model_id: model.model_id,
        base_revision: revision,
        idempotency_key: id("deviation"),
        operations: [
          {
            op: "set_field",
            feature_id: "organic",
            expected_hash: hash(before),
            expression: after,
          },
        ],
      });
      const candidate = await finish(s, draft);
      const validation = await finish(
        s,
        call(s, "cad_validate", {
          model_id: model.model_id,
          base_revision: revision,
          transaction_id: draft.transaction_id,
          idempotency_key: id("validate"),
        }),
      );
      return { draft, candidate, validation };
    };
    const dent = {
      op: "local_field_delta",
      source,
      center: ["5", "0", "0"],
      radius: "1",
      amplitude: "0.04",
    };
    const good = await change(model.revision, source, dent);
    const check = good.validation.checks.find(
      (c: any) => c.check_id === "deviation-limit",
    );
    assert.equal(good.validation.status, "checks_passed_within_profile");
    assert.equal(check.guarantee, "bounded");
    assert.equal(check.measured.status, "certified");
    assert.ok(check.measured.certified_hausdorff_bound_mm <= 0.05);
    assert.ok(check.measured.certified_hausdorff_bound_mm > 0.03);
    assert.equal(check.measured.identical_cells > 0, true);
    const committed = call(s, "cad_commit", {
      model_id: model.model_id,
      base_revision: model.revision,
      transaction_id: good.draft.transaction_id,
      validation_digest: good.validation.digest,
      idempotency_key: id("commit"),
    });
    const measured = await finish(
      s,
      call(s, "cad_measure", {
        model_id: model.model_id,
        revision: committed.revision,
        other_revision: model.revision,
        feature_id: "organic",
        metric: "surface_deviation",
        maximum_deviation: q("0.1"),
        idempotency_key: id("measure"),
      }),
    );
    assert.equal(measured.guarantee, "bounded");
    assert.ok(measured.certified_error_bound <= 0.05);
    assert.equal(measured.coverage, "entire_declared_domain");
    const bad = await change(committed.revision, dent, {
      ...dent,
      source: { ...source, radius: "5.2" },
    });
    assert.equal(bad.validation.status, "failed");
    const failed = bad.validation.checks.find(
      (c: any) => c.check_id === "deviation-limit",
    );
    assert.equal(failed.status, "failed");
    assert.equal(failed.measured.status, "not_certified");
    assert.deepEqual(Object.keys(failed.measured.failure_reasons), [
      "deviation_exceeds_epsilon",
    ]);
    assert.equal(
      s.call(principal, "cad_commit", {
        model_id: model.model_id,
        base_revision: committed.revision,
        transaction_id: bad.draft.transaction_id,
        validation_digest: bad.validation.digest,
        idempotency_key: id("reject"),
      }).status,
      "failed",
    );
    assert.equal(
      s.call(principal, "cad_measure", {
        model_id: model.model_id,
        revision: committed.revision,
        feature_id: "organic",
        metric: "surface_deviation",
        idempotency_key: id("incomplete"),
      }).errors[0].code,
      "INVALID_SCHEMA",
    );
    assert.equal(
      s.call(principal, "cad_measure", {
        model_id: model.model_id,
        metric: "volume",
        feature_id: "organic",
        other_revision: model.revision,
      }).errors[0].code,
      "INVALID_SCHEMA",
    );
  } finally {
    await env.close();
  }
});

test("interval pruning and QEF dual contouring are registered extraction options with reported diagnostics", async () => {
  const env = setup(),
    s = env.service;
  try {
    const ir = structuredClone(organic) as any;
    ir.features[0].construction.extraction = {
      method: "dual_contouring",
      pruning: "interval",
    };
    const model = await importFixture(s, ir);
    const preview = await finish(
      s,
      call(s, "cad_render", {
        model_id: model.model_id,
        revision: model.revision,
        idempotency_key: id("render"),
      }),
    );
    const mesh = JSON.parse(
      s.store.readBlob(preview.artifacts[0].hash).toString(),
    ).meshes[0];
    assert.equal(mesh.method, "dual_contouring_qef");
    assert.equal(mesh.pruning, "interval");
    assert.equal(mesh.quality, "preview_only");
    assert.equal(mesh.certified_bound, null);
    assert.ok(mesh.pruned_cells > 0);
    assert.ok(mesh.triangles.length > 100);
    const inspected = call(s, "cad_inspect", {
      model_id: model.model_id,
      feature_id: "organic",
    });
    assert.match(
      inspected.known_facts.interval_evaluation_error_model,
      /IEEE754_binary64/,
    );
  } finally {
    await env.close();
  }
});
