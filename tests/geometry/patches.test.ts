import test from "node:test";
import assert from "node:assert/strict";
import { setup, call, finish, importFixture, principal } from "../helpers.js";
import { ModelIR } from "../../packages/semantic-ir/schema.js";
import { compile } from "../../packages/compiler/index.js";
import { hash, id } from "../../packages/semantic-ir/hash.js";
import { patchContinuity } from "../../packages/compiler/patches.js";
import { execFileSync } from "node:child_process";
const fixture = () =>
  ModelIR.parse({
    schema_version: "1",
    unit: "mm",
    profile: "render_surface",
    features: [0, 2].map((offset, i) => ({
      id: "patch-" + i,
      semantic_name: "Patch " + i,
      kind: "surface",
      parameters: {},
      construction: {
        operator: "nurbs_surface",
        poles: [0, 1, 2].map((u) =>
          [0, 1, 2].map((v) => [String(u + offset), String(v), "0"]),
        ),
        weights: [
          ["1", "1", "1"],
          ["1", "1", "1"],
          ["1", "1", "1"],
        ],
      },
    })),
    outputs: ["patch-0", "patch-1"],
    constraints: [
      {
        id: "seam",
        kind: "patch_continuity",
        feature_id: "patch-0",
        neighbor_feature_id: "patch-1",
        continuity: "C1",
        tolerance: { value: "0.00001", unit: "mm" },
      },
      {
        id: "fixed-neighbor",
        kind: "protected_feature",
        feature_id: "patch-1",
      },
    ],
  });

test("local pole edits retain a complete C1 seam and protected neighbor; broken seams cannot commit", async () => {
  const env = setup(),
    s = env.service;
  try {
    const ir = fixture();
    const model = await importFixture(s, ir);
    const surface = ir.features[0].construction;
    assert.equal(surface.operator, "nurbs_surface");
    if (surface.operator !== "nurbs_surface") throw new Error("fixture");
    const original = structuredClone(surface.poles),
      changed = structuredClone(original);
    changed[0][1][2] = "0.2";
    const edit = async (poles: typeof changed) => {
      const draft = call(s, "cad_apply_patch", {
        model_id: model.model_id,
        base_revision: model.revision,
        idempotency_key: id("patch"),
        operations: [
          {
            op: "set_surface_poles",
            feature_id: "patch-0",
            expected_hash: hash(original),
            poles,
          },
        ],
      });
      const candidate = await finish(s, draft);
      const validation = await finish(
        s,
        call(s, "cad_validate", {
          model_id: model.model_id,
          base_revision: model.revision,
          transaction_id: draft.transaction_id,
          idempotency_key: id("validate"),
        }),
      );
      return { draft, candidate, validation };
    };
    const good = await edit(changed);
    assert.equal(good.validation.status, "checks_passed_within_profile");
    const seam = good.validation.checks.find((c: any) => c.check_id === "seam");
    assert.equal(seam.measured.position_bound_mm, "0");
    assert.equal(seam.measured.derivative_bound_mm_per_parameter, "0");
    const before = s.store.revision(principal, model.model_id, model.revision);
    const after = s.store.revision(
      principal,
      model.model_id,
      good.candidate.candidate_revision,
    );
    assert.equal(
      before.geometry.facts["patch-1"].geometry_hash,
      after.geometry.facts["patch-1"].geometry_hash,
    );
    const broken = structuredClone(changed);
    broken[2][1][2] = "0.1";
    const bad = await edit(broken);
    assert.equal(bad.validation.status, "failed");
    assert.equal(
      bad.validation.checks.find((c: any) => c.check_id === "seam").status,
      "failed",
    );
    const rejected = s.call(principal, "cad_commit", {
      model_id: model.model_id,
      base_revision: model.revision,
      transaction_id: bad.draft.transaction_id,
      validation_digest: bad.validation.digest,
      idempotency_key: id("commit"),
    });
    assert.equal(rejected.status, "failed");
    const committed = call(s, "cad_commit", {
      model_id: model.model_id,
      base_revision: model.revision,
      transaction_id: good.draft.transaction_id,
      validation_digest: good.validation.digest,
      idempotency_key: id("commit"),
    });
    assert.equal(
      s.store.revision(principal, model.model_id, committed.revision).ir_hash,
      after.ir_hash,
    );
    assert.equal(
      s.store.model(principal, model.model_id).head,
      committed.revision,
    );
  } finally {
    await env.close();
  }
});

test("rational patch bounds detect derivative-only kinks with nonconstant positive weights", () => {
  const ir = fixture(),
    a = ir.features[0],
    b = ir.features[1];
  if (
    a.construction.operator !== "nurbs_surface" ||
    b.construction.operator !== "nurbs_surface"
  )
    throw new Error("fixture");
  a.construction.poles[1][1][2] = "0.1";
  const report = patchContinuity(a, b);
  assert.equal(report.position_bound_mm, "0");
  assert.ok(Number(report.derivative_bound_mm_per_parameter) > 0);
  b.construction.weights[1][1] = "2";
  assert.ok(compile(ir));
  const rational = patchContinuity(a, b);
  assert.ok(Number(rational.derivative_bound_mm_per_parameter) > 0);
});

test("exact rational C2 bounds preserve nonconstant weights and detect curvature-only changes", () => {
  const ir = fixture(),
    a = ir.features[0],
    b = ir.features[1];
  if (
    a.construction.operator !== "nurbs_surface" ||
    b.construction.operator !== "nurbs_surface"
  )
    throw new Error("fixture");
  for (const patch of [a.construction, b.construction])
    patch.weights = patch.weights.map(() => ["1", "2", "1"]);
  let report = patchContinuity(a, b);
  assert.equal(report.position_bound_mm, "0");
  assert.equal(report.derivative_bound_mm_per_parameter, "0");
  assert.equal(report.second_derivative_bound_mm_per_parameter2, "0");
  assert.ok(report.regularity_proved);
  b.construction.poles[2][1][2] = "0.1";
  report = patchContinuity(a, b);
  assert.equal(report.position_bound_mm, "0");
  assert.equal(report.derivative_bound_mm_per_parameter, "0");
  assert.ok(Number(report.second_derivative_bound_mm_per_parameter2) > 0);
});

test("rational bounds cover independent native OCCT derivatives for unequal degrees", () => {
  const ir = fixture(),
    a = ir.features[0],
    b = ir.features[1];
  if (
    a.construction.operator !== "nurbs_surface" ||
    b.construction.operator !== "nurbs_surface"
  )
    throw new Error("fixture");
  a.construction.weights = [
    ["1", "1.3", "0.8"],
    ["1.2", "1", "2"],
    ["1", "2", "1"],
  ];
  a.construction.poles[1][1][2] = "0.4";
  b.construction.poles = [0, 1, 2].map((u) =>
    [0, 1, 2, 3].map((v) => [
      String(u + 2),
      String((v * 2) / 3),
      String(u * v * 0.03),
    ]),
  );
  b.construction.weights = [0, 1, 2].map(() => ["1.5", "0.8", "1.2", "2"]);
  const report = patchContinuity(a, b);
  const sampled = JSON.parse(
    execFileSync(
      ".venv/bin/python",
      [
        "-c",
        `
import sys,json,math
sys.path.insert(0,'workers/cad-occt')
from geometry import *
features=json.load(sys.stdin)
surfaces=[BRep_Tool.Surface_s(TopoDS.Face_s(make_feature(dict(f,values={}),[]))) for f in features]
maxima=[0,0,0]
for i in range(33):
    values=[]
    for surface,u in zip(surfaces,[1,0]):
        point=gp_Pnt();vectors=[gp_Vec() for _ in range(5)]
        surface.D2(u,i/32,point,*vectors)
        values.append([point,*vectors])
    errors=[math.sqrt(sum((getattr(x,axis)()-getattr(y,axis)())**2 for axis in ['X','Y','Z'])) for x,y in zip(*values)]
    for k,value in enumerate([errors[0],max(errors[1:3]),max(errors[3:])]):maxima[k]=max(maxima[k],value)
print(json.dumps(maxima))
`,
      ],
      { input: JSON.stringify([a, b]), encoding: "utf8" },
    ),
  );
  for (const [i, bound] of [
    report.position_bound_mm,
    report.derivative_bound_mm_per_parameter,
    report.second_derivative_bound_mm_per_parameter2,
  ].entries())
    assert.ok(sampled[i] <= Number(bound) + 1e-9);
  b.construction.poles = b.construction.poles.map((row) =>
    row.map(() => ["0", "0", "0"]),
  );
  assert.equal(patchContinuity(a, b).regularity_proved, false);
  b.construction.poles = Array.from({ length: 7 }, () => [
    ["0", "0", "0"],
    ["0", "1", "0"],
  ]);
  assert.throws(
    () => patchContinuity(a, b),
    (e: any) => e.code === "BUDGET_EXCEEDED",
  );
});
