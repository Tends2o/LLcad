import test from "node:test";
import assert from "node:assert/strict";
import { setup, call, finish, importFixture, principal } from "../helpers.js";
import { housing, organic } from "../../scripts/fixtures.js";
import { ModelIR } from "../../packages/semantic-ir/schema.js";
import { id } from "../../packages/semantic-ir/hash.js";
const q = (value: string, unit = "mm") => ({ value, unit });

const twoBoxes = ModelIR.parse({
  schema_version: "1",
  unit: "mm",
  features: [
    {
      id: "plate",
      semantic_name: "Platte",
      kind: "box",
      parameters: { width: q("10"), depth: q("10"), height: q("4") },
      construction: { operator: "box" },
    },
    {
      id: "block",
      semantic_name: "Block",
      kind: "box",
      parameters: {
        width: q("2"),
        depth: q("2"),
        height: q("2"),
        x: q("12"),
        z: q("1"),
      },
      construction: { operator: "box" },
    },
    {
      id: "cavity",
      semantic_name: "Hohlraum",
      kind: "box",
      parameters: {
        width: q("8"),
        depth: q("8"),
        height: q("2"),
        x: q("1"),
        y: q("1"),
        z: q("1"),
      },
      construction: { operator: "box" },
    },
    {
      id: "shell-body",
      semantic_name: "Hohlkörper",
      kind: "difference",
      parameters: {},
      construction: { operator: "difference" },
      depends_on: ["plate", "cavity"],
    },
  ],
  outputs: ["shell-body", "block"],
});

test("surface distance, wall thickness and motion clearance report their proof strength", async () => {
  const env = setup(),
    s = env.service;
  try {
    const m = await importFixture(s, twoBoxes);
    const distance = await finish(
      s,
      call(s, "cad_measure", {
        model_id: m.model_id,
        revision: m.revision,
        feature_id: "shell-body",
        other_feature_id: "block",
        metric: "surface_distance",
        idempotency_key: id("surface-distance"),
      }),
    );
    assert.equal(distance.guarantee, "sampled");
    assert.equal(distance.certified_error_bound, null);
    assert.equal(
      distance.measurements.exact_minimum_distance_mm.guarantee,
      "exact_for_declared_domain",
    );
    assert.ok(
      Math.abs(distance.measurements.exact_minimum_distance_mm.value - 2) <
        1e-9,
    );
    assert.equal(
      distance.measurements.sampled_hausdorff_mm.guarantee,
      "sampled",
    );
    assert.match(
      distance.measurements.sampled_hausdorff_mm.note,
      /not an upper bound/,
    );
    assert.equal(distance.measurements.volume_iou.value, 0);
    const wall = await finish(
      s,
      call(s, "cad_measure", {
        model_id: m.model_id,
        revision: m.revision,
        feature_id: "shell-body",
        metric: "wall_thickness",
        idempotency_key: id("wall"),
      }),
    );
    assert.equal(wall.guarantee, "sampled");
    assert.ok(
      Math.abs(wall.measurements.minimum_mm - 1) < 1e-6,
      String(wall.measurements.minimum_mm),
    );
    assert.match(wall.coverage, /not_a_global_minimum_certificate/);
    const motion = await finish(
      s,
      call(s, "cad_measure", {
        model_id: m.model_id,
        revision: m.revision,
        feature_id: "shell-body",
        other_feature_id: "block",
        metric: "clearance",
        minimum_clearance: q("0.5"),
        motion: { translation: ["-4", "0", "0"], steps: 8 },
        idempotency_key: id("motion"),
      }),
    );
    assert.equal(motion.motion_or_global_wall_certificate, false);
    assert.equal(motion.measurements.collision_sampled, true);
    assert.equal(motion.measurements.minimum_satisfied_at_samples, false);
    assert.equal(motion.measurements.samples.length, 9);
    assert.equal(
      s.call(principal, "cad_measure", {
        model_id: m.model_id,
        revision: m.revision,
        feature_id: "shell-body",
        metric: "wall_thickness",
        other_feature_id: "block",
        idempotency_key: id("bad"),
      }).errors[0].code,
      "INVALID_SCHEMA",
    );
    assert.equal(
      s.call(principal, "cad_measure", {
        model_id: m.model_id,
        revision: m.revision,
        feature_id: "shell-body",
        metric: "distance",
        other_feature_id: "block",
        motion: { translation: ["1", "0", "0"] },
        idempotency_key: id("bad-motion"),
      }).errors[0].code,
      "INVALID_SCHEMA",
    );
    const capabilities = call(s, "cad_capabilities", {});
    assert.ok(capabilities.analysis.metrics.includes("wall_thickness"));
    assert.ok(
      capabilities.quality_profiles.includes("manufacturing_candidate"),
    );
  } finally {
    await env.close();
  }
});

test("blend-free mating regions are certified by interval arithmetic in measurement and validation", async () => {
  const env = setup(),
    s = env.service;
  try {
    const blend = {
      op: "smooth_union",
      a: { op: "sphere", center: ["0", "0", "0"], radius: "5" },
      b: { op: "sphere", center: ["12", "0", "0"], radius: "5" },
      k: "2",
    };
    const ir = {
      ...organic,
      features: [
        {
          ...organic.features[0],
          construction: {
            ...organic.features[0].construction,
            expression: blend,
            domain: { min: ["-7", "-7", "-7"], max: ["19", "7", "7"] },
            cell_size: q("0.5"),
          },
        },
      ],
      constraints: [
        {
          id: "mating-face",
          kind: "blend_free_region",
          feature_id: "organic",
          min: ["-7", "-7", "-7"],
          max: ["-3", "7", "7"],
        },
      ],
    };
    const m = await importFixture(s, ir);
    const check = m.validation.checks.find(
      (c: any) => c.check_id === "mating-face",
    );
    assert.equal(check.status, "passed");
    assert.equal(check.guarantee, "bounded");
    assert.equal(check.measured.status, "certified");
    const near = await finish(
      s,
      call(s, "cad_measure", {
        model_id: m.model_id,
        revision: m.revision,
        feature_id: "organic",
        metric: "blend_activity",
        region: { min: ["4", "-2", "-2"], max: ["8", "2", "2"] },
        idempotency_key: id("near"),
      }),
    );
    assert.equal(near.guarantee, "not_certified");
    assert.equal(near.measurements.blends[0].status, "possibly_active");
    const far = await finish(
      s,
      call(s, "cad_measure", {
        model_id: m.model_id,
        revision: m.revision,
        feature_id: "organic",
        metric: "blend_activity",
        region: { min: ["-7", "-7", "-7"], max: ["-3", "7", "7"] },
        idempotency_key: id("far"),
      }),
    );
    assert.equal(far.guarantee, "bounded");
    assert.equal(far.measurements.status, "certified");
    const failing = {
      ...ir,
      constraints: [
        {
          id: "mating-face",
          kind: "blend_free_region",
          feature_id: "organic",
          min: ["4", "-2", "-2"],
          max: ["8", "2", "2"],
        },
      ],
    };
    const model = call(s, "cad_create_model", {
      name: "Blend",
      profile: "render_surface",
      idempotency_key: id("blend"),
    });
    const upload = s.store.artifact(
      principal,
      JSON.stringify(failing),
      "application/json",
      model.model_id,
      model.revision,
      {},
    );
    const draft = call(s, "cad_import", {
      model_id: model.model_id,
      base_revision: model.revision,
      artifact_id: upload.artifact_id,
      format: "ir",
      source_unit: "mm",
      idempotency_key: id("import"),
    });
    await finish(s, draft);
    const validation = await finish(
      s,
      call(s, "cad_validate", {
        model_id: model.model_id,
        base_revision: model.revision,
        transaction_id: draft.transaction_id,
        idempotency_key: id("validate"),
      }),
    );
    assert.equal(validation.status, "failed");
    assert.equal(
      validation.checks.find((c: any) => c.check_id === "mating-face").status,
      "failed",
    );
    assert.equal(
      s.call(principal, "cad_measure", {
        model_id: m.model_id,
        revision: m.revision,
        feature_id: "organic",
        metric: "blend_activity",
        idempotency_key: id("no-region"),
      }).errors[0].code,
      "INVALID_SCHEMA",
    );
  } finally {
    await env.close();
  }
});

test("housing measures keep working alongside the new metrics", async () => {
  const env = setup(),
    s = env.service;
  try {
    const m = await importFixture(s, housing);
    const wall = await finish(
      s,
      call(s, "cad_measure", {
        model_id: m.model_id,
        revision: m.revision,
        feature_id: housing.outputs[0],
        metric: "wall_thickness",
        idempotency_key: id("housing-wall"),
      }),
    );
    assert.ok(wall.measurements.minimum_mm > 0);
    assert.ok(wall.measurements.samples > 50);
  } finally {
    await env.close();
  }
});
