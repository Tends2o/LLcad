import test from "node:test";
import assert from "node:assert/strict";
import { setup, call, finish, importFixture } from "../helpers.js";
import { sphere } from "../../scripts/fixtures.js";
import { id } from "../../packages/semantic-ir/hash.js";
test("inverse construction solves a target volume and verifies the generated B-Rep volume", async () => {
  const env = setup(),
    s = env.service;
  try {
    const m = await importFixture(s, sphere);
    const target = (4 / 3) * Math.PI * 11 ** 3;
    const draft = call(s, "cad_apply_patch", {
      model_id: m.model_id,
      base_revision: m.revision,
      idempotency_key: id("inverse"),
      operations: [
        {
          op: "solve_volume",
          feature_id: "sphere",
          parameter: "radius",
          expected: { value: "10", unit: "mm" },
          target: { value: target.toFixed(10), unit: "mm3" },
          tolerance: { value: "0.00001", unit: "mm3" },
        },
      ],
    });
    await finish(s, draft);
    const result = await finish(
      s,
      call(s, "cad_validate", {
        model_id: m.model_id,
        base_revision: m.revision,
        transaction_id: draft.transaction_id,
        idempotency_key: id("validate"),
      }),
    );
    assert.equal(result.status, "checks_passed_within_profile");
    const check = result.checks.find(
      (c: any) => c.check_id === "volume-sphere",
    );
    assert.ok(Math.abs(check.measured - target) < 1e-5);
  } finally {
    await env.close();
  }
});
test("distance analysis measures actual separated B-Rep features", async () => {
  const env = setup(),
    s = env.service;
  try {
    const other = structuredClone(sphere.features[0]);
    other.id = "second";
    other.parameters.x = { value: "30", unit: "mm" };
    const m = await importFixture(s, {
      ...sphere,
      features: [sphere.features[0], other],
      outputs: ["sphere", "second"],
    });
    const result = await finish(
      s,
      call(s, "cad_measure", {
        model_id: m.model_id,
        revision: m.revision,
        feature_id: "sphere",
        other_feature_id: "second",
        metric: "distance",
        idempotency_key: id("measure"),
      }),
    );
    assert.ok(Math.abs(result.measurements.distance - 10) < 1e-7);
  } finally {
    await env.close();
  }
});
