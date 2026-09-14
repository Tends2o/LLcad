import test from "node:test";
import assert from "node:assert/strict";
import { setup, call, finish, importFixture, principal } from "../helpers.js";
import { ModelIR } from "../../packages/semantic-ir/schema.js";
import { id } from "../../packages/semantic-ir/hash.js";
const q = (value: string) => ({ value, unit: "mm" });
const fixture = ModelIR.parse({
  schema_version: "1",
  unit: "mm",
  profile: "render_surface",
  features: [
    {
      id: "ball",
      semantic_name: "Kugel",
      kind: "sphere",
      construction: { operator: "sphere" },
      parameters: { radius: q("2") },
    },
    {
      id: "box",
      semantic_name: "Quader",
      kind: "box",
      construction: { operator: "box" },
      parameters: { width: q("1"), depth: q("1"), height: q("1"), x: q("4") },
    },
    {
      id: "circle",
      semantic_name: "Kreis",
      kind: "circle",
      construction: { operator: "circle" },
      parameters: { radius: q("4") },
    },
    {
      id: "line-x",
      semantic_name: "X-Linie",
      kind: "line",
      construction: {
        operator: "line",
        start: ["0", "0", "0"],
        end: ["1", "0", "0"],
      },
      parameters: {},
    },
    {
      id: "line-y",
      semantic_name: "Y-Linie",
      kind: "line",
      construction: {
        operator: "line",
        start: ["0", "0", "0"],
        end: ["0", "1", "0"],
      },
      parameters: {},
    },
  ],
  outputs: ["ball", "box", "circle", "line-x", "line-y"],
});

test("native point curvature, oriented angle and static clearance are measured through isolated jobs", async () => {
  const env = setup(),
    s = env.service;
  try {
    const m = await importFixture(s, fixture);
    const binding = { model_id: m.model_id, revision: m.revision };
    const measure = (args: any) =>
      finish(
        s,
        call(s, "cad_measure", {
          ...binding,
          ...args,
          idempotency_key: id("measure"),
        }),
      );
    const curvature = await measure({
      feature_id: "ball",
      metric: "curvature",
    });
    assert.deepEqual(
      curvature.measurements.principal_curvatures_per_mm,
      [-0.5, -0.5],
    );
    assert.equal(curvature.measurements.gaussian_curvature_per_mm2, 0.25);
    assert.equal(curvature.certified_error_bound, null);
    assert.ok(curvature.metrics.cache_hits >= 5);
    const curve = await measure({
      feature_id: "circle",
      metric: "curvature",
      curve_parameter: "0.25",
    });
    assert.ok(Math.abs(curve.measurements.curvature_per_mm - 0.25) < 1e-12);
    assert.equal(curve.measurements.torsion_per_mm, 0);
    const angle = await measure({
      feature_id: "line-x",
      other_feature_id: "line-y",
      metric: "angle",
    });
    assert.ok(Math.abs(angle.measurements.angle_deg - 90) < 1e-12);
    const inspected = call(s, "cad_inspect", { ...binding, feature_id: "box" });
    const top = inspected.face_page.faces.find(
      (f: any) => f.origins[0].role === "top",
    );
    const side = inspected.face_page.faces.find(
      (f: any) => f.origins[0].role === "x_max",
    );
    assert.equal(top.uv_bounds.length, 4);
    const normalAngle = await measure({
      feature_id: "box",
      face_id: top.face_id,
      other_feature_id: "box",
      other_face_id: side.face_id,
      metric: "angle",
    });
    assert.ok(Math.abs(normalAngle.measurements.angle_deg - 90) < 1e-12);
    const free = await measure({
      feature_id: "ball",
      other_feature_id: "box",
      metric: "clearance",
      minimum_clearance: q("1.9"),
    });
    assert.ok(Math.abs(free.measurements.clearance_mm - 2) < 1e-7);
    assert.equal(free.measurements.minimum_satisfied, true);
    assert.equal(free.motion_or_global_wall_certificate, false);
    const tight = await measure({
      feature_id: "ball",
      other_feature_id: "box",
      metric: "clearance",
      minimum_clearance: q("2.1"),
    });
    assert.equal(tight.measurements.minimum_satisfied, false);
    assert.equal(
      call(s, "cad_get_model", { model_id: m.model_id }).revision,
      m.revision,
    );
  } finally {
    await env.close();
  }
});

test("analysis rejects ambiguous, unbound and foreign faces, outside UV samples and occupied overlap", async () => {
  const env = setup(),
    s = env.service;
  try {
    const m = await importFixture(s, fixture),
      binding = {
        model_id: m.model_id,
        revision: m.revision,
        metric: "curvature",
        idempotency_key: id("analysis"),
      };
    assert.equal(
      s.call(principal, "cad_measure", { ...binding, feature_id: "box" })
        .errors[0].code,
      "AMBIGUOUS_SELECTION",
    );
    const face = call(s, "cad_inspect", {
      model_id: m.model_id,
      revision: m.revision,
      feature_id: "ball",
    }).face_page.faces[0];
    assert.equal(
      s.call(principal, "cad_measure", {
        ...binding,
        revision: undefined,
        feature_id: "ball",
        face_id: face.face_id,
      }).errors[0].code,
      "INVALID_SCHEMA",
    );
    assert.equal(
      s.call(principal, "cad_measure", {
        ...binding,
        feature_id: "box",
        face_id: face.face_id,
      }).errors[0].code,
      "STALE_REVISION",
    );
    assert.equal(
      s.call({ ...principal, user: "bob" }, "cad_measure", {
        ...binding,
        feature_id: "ball",
      }).errors[0].code,
      "ACCESS_DENIED",
    );
    const outside = call(s, "cad_measure", {
      ...binding,
      feature_id: "ball",
      uv: ["100", "100"],
    });
    const failed = await s.jobs.wait(principal, outside.job_id);
    assert.equal(failed.status, "failed");
    assert.equal(failed.error.code, "OUT_OF_SCOPE");
    const containing = await importFixture(s, {
      ...fixture,
      features: fixture.features
        .filter((f) => f.id === "ball" || f.id === "box")
        .map((f) =>
          f.id === "box"
            ? {
                ...f,
                parameters: {
                  width: q("0.5"),
                  depth: q("0.5"),
                  height: q("0.5"),
                },
              }
            : f,
        ),
      outputs: ["ball", "box"],
    });
    const clearance = await finish(
      s,
      call(s, "cad_measure", {
        model_id: containing.model_id,
        metric: "clearance",
        feature_id: "ball",
        other_feature_id: "box",
        minimum_clearance: q("0.1"),
        idempotency_key: id("clearance"),
      }),
    );
    assert.equal(clearance.measurements.contains_or_intersects_solid, true);
    assert.equal(clearance.measurements.minimum_satisfied, false);
  } finally {
    await env.close();
  }
});
