import test from "node:test";
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { ModelIR, Feature } from "../../packages/semantic-ir/schema.js";
import { compile, applyPatch } from "../../packages/compiler/index.js";
import {
  refineSurface,
  surfaceBasis,
  expandedKnots,
  type Surface,
} from "../../packages/compiler/nurbs.js";
import { patchContinuity } from "../../packages/compiler/patches.js";
import { bsplineBasis } from "../../packages/compiler/math.js";
import * as Q from "../../packages/compiler/bernstein.js";
import { hash, id } from "../../packages/semantic-ir/hash.js";
import { setup, call, finish, importFixture, principal } from "../helpers.js";

const surface = (): Surface => ({
  operator: "nurbs_surface",
  poles: [0, 1, 2, 3, 4].map((u) =>
    [0, 1, 2].map((v) => [String(u), String(v), String((u * v) / 10)]),
  ),
  weights: [0, 1, 2, 3, 4].map((u) =>
    [0, 1, 2].map((v) => String(1 + (u + v) / 10)),
  ),
  u_basis: {
    degree: 2,
    knots: ["0", "0.25", "0.75", "1"],
    multiplicities: [3, 1, 1, 3],
  },
});
const feature = (c: Surface, id = "surface") =>
  Feature.parse({
    id,
    semantic_name: "Freiformfläche",
    kind: "surface",
    parameters: {},
    construction: c,
  });
const fixture = (c = surface()) =>
  ModelIR.parse({
    schema_version: "1",
    unit: "mm",
    profile: "render_surface",
    features: [feature(c)],
    outputs: ["surface"],
  });

// Independent recursive Cox-de Boor evaluator in exact arithmetic, without the insertion code.
function exactPoint(c: Surface, u: string, v: string) {
  const basis = (axis: "u" | "v", value: string) => {
    const b = surfaceBasis(c, axis),
      knots = expandedKnots(b).map(Q.decimal),
      t = Q.decimal(value);
    const n = axis === "u" ? c.poles.length : c.poles[0].length;
    const at = (i: number, p: number): Q.Q => {
      if (Q.cmp(t, Q.one) === 0) return i === n - 1 ? Q.one : Q.zero;
      if (p === 0)
        return Q.cmp(knots[i], t) <= 0 && Q.cmp(t, knots[i + 1]) < 0
          ? Q.one
          : Q.zero;
      const a = Q.sub(knots[i + p], knots[i]),
        b = Q.sub(knots[i + p + 1], knots[i + 1]);
      return Q.add(
        a.n ? Q.mul(Q.div(Q.sub(t, knots[i]), a), at(i, p - 1)) : Q.zero,
        b.n
          ? Q.mul(Q.div(Q.sub(knots[i + p + 1], t), b), at(i + 1, p - 1))
          : Q.zero,
      );
    };
    return Array.from({ length: n }, (_, i) => at(i, b.degree));
  };
  const nu = basis("u", u),
    nv = basis("v", v);
  const h = [0, 1, 2, 3].map((axis) =>
    c.poles
      .flatMap((row, i) =>
        row.map((p, j) =>
          Q.mul(
            Q.mul(nu[i], nv[j]),
            Q.mul(
              Q.decimal(c.weights[i][j]),
              axis === 3 ? Q.one : Q.decimal(p[axis]),
            ),
          ),
        ),
      )
      .reduce(Q.add, Q.zero),
  );
  return h.slice(0, 3).map((x) => Q.div(x, h[3]));
}

test("general rational knot insertion has an outward entire-domain bound and matches independent native OCCT insertion", () => {
  const original = surface(),
    refined = refineSurface(original, "u", ["0.5", "0.25"]);
  const both = refineSurface(refined.construction, "v", ["0.37"]);
  assert.ok(compile(fixture(both.construction)));
  assert.deepEqual(refined.report.control_points_after, [7, 3]);
  const bound = Q.add(
    Q.decimal(refined.report.geometric_error_bound_mm),
    Q.decimal(both.report.geometric_error_bound_mm),
  );
  assert.ok(Q.cmp(bound, Q.decimal("0.000000000001")) < 0);
  for (const u of ["0", "0.13", "0.25", "0.499", "0.5", "0.75", "0.99", "1"])
    for (const v of ["0", "0.2", "0.37", "0.6", "1"]) {
      const before = exactPoint(original, u, v),
        after = exactPoint(both.construction, u, v);
      const error = before
        .map((a, i) => Q.abs(Q.sub(a, after[i])))
        .reduce(Q.add, Q.zero);
      assert.ok(Q.cmp(error, bound) <= 0, `${u},${v}`);
    }
  const native = JSON.parse(
    execFileSync(
      ".venv/bin/python",
      [
        "-c",
        `
import sys,json
sys.path.insert(0,'workers/cad-occt')
from geometry import *
original,refined=json.load(sys.stdin)
surfaces=[BRep_Tool.Surface_s(TopoDS.Face_s(make_feature({'values':{},'construction':c},[]))) for c in [original,refined]]
before=surfaces[0].Copy();before.InsertUKnot(.5,1,1e-12,True);before.InsertUKnot(.25,1,1e-12,True);before.InsertVKnot(.37,1,1e-12,True)
errors=[]
for i in range(21):
 for j in range(21):errors.append(surfaces[0].Value(i/20,j/20).Distance(surfaces[1].Value(i/20,j/20)))
print(json.dumps({'max_error':max(errors),'poles':[before.NbUPoles(),before.NbVPoles()], 'native_pole_error':max(before.Pole(i,j).Distance(surfaces[1].Pole(i,j)) for i in range(1,before.NbUPoles()+1) for j in range(1,before.NbVPoles()+1))}))
`,
      ],
      {
        input: JSON.stringify([original, both.construction]),
        encoding: "utf8",
      },
    ),
  );
  assert.deepEqual(native.poles, [7, 4]);
  assert.ok(native.max_error < 1e-12 && native.native_pole_error < 1e-12);
});

test("nonuniform repeated-knot basis partitions unity and rejects malformed domains before entering the kernel", () => {
  const c = surface(),
    b = surfaceBasis(c, "u"),
    knots = expandedKnots(b).map(Number);
  for (let j = 0; j <= 100; j++) {
    const basis = c.poles.map((_, i) =>
      bsplineBasis(i, b.degree, knots, j / 100),
    );
    assert.ok(basis.every((x) => x >= 0));
    assert.ok(Math.abs(basis.reduce((a, b) => a + b) - 1) < 1e-14);
    assert.equal(bsplineBasis(0, b.degree, knots, 0.8), 0);
  }
  for (const bad of [
    { ...b, multiplicities: [3, 2, 1, 3] },
    { ...b, knots: ["0", "0.25", "0.25000000001", "1"] },
    { ...b, knots: ["0", "0.75", "0.25", "1"] },
    { ...b, knots: ["0", "0.25", "0.75", "0.999999999999999999999999999"] },
    { ...b, degree: 1 },
  ])
    assert.throws(
      () => compile(fixture({ ...c, u_basis: bad })),
      (e: any) => e.code === "INVALID_SCHEMA",
    );
  assert.throws(
    () => refineSurface(c, "u", ["0.25", "0.25"]),
    (e: any) => e.code === "OUT_OF_SCOPE",
  );
  assert.throws(
    () => refineSurface(c, "u", ["0"]),
    (e: any) => e.code === "OUT_OF_SCOPE",
  );
  const badWeights = structuredClone(c);
  badWeights.weights[0][0] = "0";
  assert.throws(() => compile(fixture(badWeights)));
});

test("piecewise exact seam bounds preserve real C2 after insertion and refuse derivative claims at kinks", () => {
  const patches = [0, 2].map((offset) =>
    feature(
      {
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
      "patch-" + offset,
    ),
  );
  const [a, b] = patches;
  a.construction = refineSurface(a.construction as Surface, "v", [
    "0.25",
    "0.5",
    "0.5",
  ]).construction;
  b.construction = refineSurface(b.construction as Surface, "u", [
    "0.5",
  ]).construction;
  b.construction = refineSurface(b.construction as Surface, "v", [
    "0.75",
  ]).construction;
  const report = patchContinuity(a, b);
  assert.equal(report.knot_spans, 4);
  assert.equal(report.second_derivative_bound_mm_per_parameter2, "0");
  assert.equal(report.second_derivatives_defined_at_interior_knots, true);
  assert.ok(report.regularity_proved);
  const kink = feature({
    operator: "nurbs_surface",
    poles: [0, 1].map((u) => [
      [String(u), "0", "0"],
      [String(u), "1", "1"],
      [String(u), "2", "0"],
    ]),
    weights: [
      ["1", "1", "1"],
      ["1", "1", "1"],
    ],
    v_basis: { degree: 1, knots: ["0", "0.5", "1"], multiplicities: [2, 1, 2] },
  });
  const neighbor = structuredClone(kink);
  neighbor.id = "neighbor";
  (neighbor.construction as Surface).poles.forEach((row) =>
    row.forEach((p) => (p[0] = String(Number(p[0]) + 1))),
  );
  const broken = patchContinuity(kink, neighbor);
  assert.equal(broken.position_bound_mm, "0");
  assert.equal(broken.derivative_bound_mm_per_parameter, "0");
  assert.equal(broken.first_derivatives_defined_at_interior_knots, false);
});

test("LLM tools discover refinement hashes, plan bounds, validate and commit; stale and protected edits fail", async () => {
  const env = setup(),
    s = env.service;
  try {
    const m = await importFixture(s, fixture());
    const inspect = call(s, "cad_inspect", {
      model_id: m.model_id,
      revision: m.revision,
      feature_id: "surface",
    });
    assert.ok(
      inspect.available_edit_operations.some(
        (x: any) => x.op === "insert_surface_knots",
      ),
    );
    const patch = {
      model_id: m.model_id,
      base_revision: m.revision,
      idempotency_key: id("refine"),
      operations: [
        {
          op: "insert_surface_knots",
          feature_id: "surface",
          expected_hash: inspect.construction_hash,
          direction: "u",
          knots: ["0.5"],
          maximum_deviation: { value: "0.001", unit: "um" },
        },
      ],
    };
    const plan = call(s, "cad_plan_edit", patch);
    assert.ok(
      Number(plan.refinement_reports[0].report.geometric_error_bound_mm) < 1e-6,
    );
    assert.equal(s.store.model(principal, m.model_id).head, m.revision);
    const draft = call(s, "cad_apply_patch", {
        ...patch,
        idempotency_key: id("apply"),
      }),
      candidate = await finish(s, draft);
    const validation = await finish(
      s,
      call(s, "cad_validate", {
        model_id: m.model_id,
        base_revision: m.revision,
        transaction_id: draft.transaction_id,
        idempotency_key: id("validate"),
      }),
    );
    assert.equal(validation.status, "checks_passed_within_profile");
    const commit = call(s, "cad_commit", {
      model_id: m.model_id,
      base_revision: m.revision,
      transaction_id: draft.transaction_id,
      validation_digest: validation.digest,
      idempotency_key: id("commit"),
    });
    assert.equal(
      s.store.revision(principal, m.model_id, commit.revision).ir_hash,
      s.store.revision(principal, m.model_id, candidate.candidate_revision)
        .ir_hash,
    );
    assert.equal(
      s.store.revision(principal, m.model_id, m.revision).ir.features[0]
        .construction.poles.length,
      5,
    );
    const failed = s.call(principal, "cad_plan_edit", {
      ...patch,
      base_revision: commit.revision,
      idempotency_key: id("stale"),
    });
    assert.equal(failed.errors[0].code, "STALE_REVISION");
    const ir = fixture();
    ir.constraints.push({
      id: "protected",
      kind: "protected_feature",
      feature_id: "surface",
    });
    assert.throws(
      () => applyPatch(ir, { ...patch, operations: patch.operations } as any),
      (e: any) => e.code === "OUT_OF_SCOPE",
    );
    assert.equal(
      inspect.construction_hash,
      hash(fixture().features[0].construction),
    );
  } finally {
    await env.close();
  }
});

test("piecewise rational bounds cover native derivatives with nonuniform spans and unequal partitions", () => {
  const a = feature(refineSurface(surface(), "v", ["0.3"]).construction, "a");
  const b = feature(refineSurface(surface(), "v", ["0.7"]).construction, "b");
  (b.construction as Surface).poles.forEach((row) =>
    row.forEach((p) => {
      p[0] = String(Number(p[0]) + 4);
      p[2] = String(Number(p[2]) + 0.2);
    }),
  );
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
surfaces=[BRep_Tool.Surface_s(TopoDS.Face_s(make_feature(dict(f,values={}),[]))) for f in json.load(sys.stdin)]
maxima=[0,0,0]
for i in range(37):
 values=[]
 for surface,u in zip(surfaces,[1,0]):
  p=gp_Pnt();vectors=[gp_Vec() for _ in range(5)];surface.D2(u,(i+.37)/37,p,*vectors);values.append([p,*vectors])
 errors=[math.sqrt(sum((getattr(x,axis)()-getattr(y,axis)())**2 for axis in ['X','Y','Z'])) for x,y in zip(*values)]
 for k,value in enumerate([errors[0],max(errors[1:3]),max(errors[3:])]):maxima[k]=max(maxima[k],value)
print(json.dumps(maxima))
`,
      ],
      { input: JSON.stringify([a, b]), encoding: "utf8" },
    ),
  );
  assert.equal(report.knot_spans, 3);
  for (const [i, bound] of [
    report.position_bound_mm,
    report.derivative_bound_mm_per_parameter,
    report.second_derivative_bound_mm_per_parameter2,
  ].entries())
    assert.ok(sampled[i] <= Number(bound) + 1e-9);
});
