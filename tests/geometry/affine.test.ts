import test from "node:test";
import assert from "node:assert/strict";
import { affineContract } from "../../packages/compiler/affine.js";
import { fieldContract, compile } from "../../packages/compiler/index.js";
import { ModelIR } from "../../packages/semantic-ir/schema.js";
import { setup, call, finish, importFixture, principal } from "../helpers.js";
import { id } from "../../packages/semantic-ir/hash.js";

const matrix = [
  ["-2", "1", "0"],
  ["0", "1", "0"],
  ["0", "0", "1"],
];
const translation = ["10", "0", "0"];
test("exact affine contracts reject singular and badly conditioned matrices and preserve field semantics", () => {
  const contract = affineContract(matrix, translation);
  assert.deepEqual(contract.determinant, { numerator: "-2", denominator: "1" });
  assert.equal(contract.orientation, "reversing");
  assert.ok(Number(contract.field_distance_scale_lower) > 0);
  for (const m of [
    [
      ["0", "0", "0"],
      ["0", "1", "0"],
      ["0", "0", "1"],
    ],
    [
      ["0.0000000001", "0", "0"],
      ["0", "1", "0"],
      ["0", "0", "1"],
    ],
  ])
    assert.throws(() => affineContract(m, translation));
  const sphere = { op: "sphere", center: ["0", "0", "0"], radius: "2" };
  const general = {
    op: "local_field_delta",
    source: sphere,
    center: ["0", "0", "0"],
    radius: "1",
    amplitude: "0.1",
  };
  for (const source of [sphere, general]) {
    const affine = fieldContract({
      op: "affine_transform",
      matrix,
      translation,
      source,
    });
    assert.equal(
      affine.semantics,
      source === sphere ? "bounded_distance_estimator" : "general_implicit",
    );
    assert.equal(affine.lipschitz, fieldContract(source).lipschitz);
    const rotated = fieldContract({
      op: "rotate",
      source,
      axis: ["1", "2", "3"],
      origin: ["5", "6", "7"],
      angle: { value: "60", unit: "deg" },
    });
    assert.deepEqual(rotated, fieldContract(source));
  }
  assert.throws(
    () =>
      fieldContract({
        op: "rotate",
        source: sphere,
        axis: ["1", "0", "0"],
        origin: ["0", "0", "0"],
        angle: { value: "1", unit: "mm" },
      }),
    (e: any) => e.code === "UNIT_MISMATCH",
  );
});

test("affine construction is editable through hashed plans and preserves native face lineage", async () => {
  const env = setup(),
    s = env.service;
  try {
    const ir = ModelIR.parse({
      schema_version: "1",
      unit: "mm",
      features: [
        {
          id: "box",
          semantic_name: "Grundquader",
          kind: "box",
          parameters: {
            width: { value: "2", unit: "mm" },
            depth: { value: "3", unit: "mm" },
            height: { value: "4", unit: "mm" },
          },
          construction: { operator: "box" },
        },
        {
          id: "affine",
          semantic_name: "Geschertes Spiegelteil",
          kind: "transformation",
          parameters: {},
          depends_on: ["box"],
          construction: { operator: "affine_transform", matrix, translation },
        },
      ],
      outputs: ["affine"],
      constraints: [
        { id: "protect-source", kind: "protected_feature", feature_id: "box" },
      ],
    });
    assert.ok(compile(ir));
    const m = await importFixture(s, ir);
    const inspect = call(s, "cad_inspect", {
      model_id: m.model_id,
      revision: m.revision,
      feature_id: "affine",
    });
    assert.equal(inspect.transformation_contract.orientation, "reversing");
    assert.equal(inspect.known_facts.volume, 48);
    assert.equal(inspect.known_facts.topology.tracked_faces, 6);
    const patch = {
      model_id: m.model_id,
      base_revision: m.revision,
      idempotency_key: id("plan"),
      operations: [
        {
          op: "set_construction",
          feature_id: "affine",
          expected_hash: inspect.construction_hash,
          construction: {
            operator: "affine_transform",
            matrix,
            translation: ["20", "0", "0"],
          },
        },
      ],
    };
    const plan = call(s, "cad_plan_edit", patch);
    assert.deepEqual(plan.changed_features, ["affine"]);
    const draft = call(s, "cad_apply_patch", {
      ...patch,
      idempotency_key: id("apply"),
    });
    await finish(s, draft);
    const binding = {
      model_id: m.model_id,
      base_revision: m.revision,
      transaction_id: draft.transaction_id,
    };
    const validation = await finish(
      s,
      call(s, "cad_validate", { ...binding, idempotency_key: id("validate") }),
    );
    assert.equal(validation.status, "checks_passed_within_profile");
    const commit = call(s, "cad_commit", {
      ...binding,
      validation_digest: validation.digest,
      idempotency_key: id("commit"),
    });
    const old = s.store.revision(principal, m.model_id, m.revision),
      next = s.store.revision(principal, m.model_id, commit.revision);
    assert.equal(
      old.geometry.facts.box.geometry_hash,
      next.geometry.facts.box.geometry_hash,
    );
    assert.ok(
      Math.abs(
        next.geometry.aggregate.bounds[0] -
          old.geometry.aggregate.bounds[0] -
          10,
      ) < 1e-6,
    );
    const stale = s.call(principal, "cad_plan_edit", {
      ...patch,
      base_revision: commit.revision,
      idempotency_key: id("stale"),
    });
    assert.equal(stale.errors[0].code, "STALE_REVISION");
    const current = call(s, "cad_inspect", {
      model_id: m.model_id,
      revision: commit.revision,
      feature_id: "affine",
    });
    const excessive = call(s, "cad_apply_patch", {
      model_id: m.model_id,
      base_revision: commit.revision,
      idempotency_key: id("excessive"),
      operations: [
        {
          op: "set_construction",
          feature_id: "affine",
          expected_hash: current.construction_hash,
          construction: {
            operator: "affine_transform",
            translation,
            matrix: [
              ["1000000", "0", "0"],
              ["0", "1000000", "0"],
              ["0", "0", "1000000"],
            ],
          },
        },
      ],
    });
    const failed = await s.jobs.wait(principal, excessive.job_id);
    assert.equal(failed.status, "failed");
    assert.equal(failed.error.code, "PRECISION_UNSUPPORTED");
    assert.equal(s.store.model(principal, m.model_id).head, commit.revision);
  } finally {
    await env.close();
  }
});
