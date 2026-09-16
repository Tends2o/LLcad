import test from "node:test";
import assert from "node:assert/strict";
import {
  compile,
  applyPatch,
  fieldContract,
} from "../../packages/compiler/index.js";
import {
  evaluate,
  solveMonotone,
} from "../../packages/compiler/expressions.js";
import { quantity, equalQuantity } from "../../packages/semantic-ir/units.js";
import { Patch } from "../../packages/semantic-ir/schema.js";
import { housing, sphere, organic } from "../../scripts/fixtures.js";
const failure = (code: string) => (error: any) => error.code === code;
test("decimal units preserve 20 µm and reject length/angle mixing", () => {
  assert.equal(quantity({ value: "20", unit: "um" }), 0.02);
  assert.ok(
    equalQuantity({ value: "0.020", unit: "mm" }, { value: "20", unit: "um" }),
  );
  assert.throws(
    () => quantity({ value: "1", unit: "rad" }, "length"),
    failure("UNIT_MISMATCH"),
  );
});
test("strict patch rejects forged validation and executable payloads", () => {
  const patch = {
    model_id: "model",
    base_revision: "rev",
    idempotency_key: "a".repeat(16),
    operations: [
      {
        op: "set_parameter",
        feature_id: "sphere",
        parameter: "radius",
        expected: { value: "10", unit: "mm" },
        value: { value: "2", unit: "mm" },
      },
    ],
  };
  assert.ok(Patch.safeParse(patch).success);
  for (const extra of [
    { validated: true },
    { execute_python: "import os" },
    { tenant: "another" },
  ])
    assert.equal(Patch.safeParse({ ...patch, ...extra }).success, false);
  for (const value of ["NaN", "Infinity", "1e10", " 1", "-0.0\n"])
    assert.equal(
      Patch.safeParse({
        ...patch,
        operations: [{ ...patch.operations[0], value: { value, unit: "mm" } }],
      }).success,
      false,
    );
});
test("DAG compilation rejects cycles, absent refs, wrong dimensions and degenerate primitives", () => {
  const s = structuredClone(sphere);
  s.features[0].depends_on = ["sphere"];
  assert.throws(() => compile(s), failure("CONSTRAINT_CONFLICT"));
  s.features[0].depends_on = ["missing"];
  assert.throws(() => compile(s), failure("INVALID_SCHEMA"));
  s.features[0].depends_on = [];
  s.features[0].parameters.radius = { value: "3", unit: "rad" };
  assert.throws(() => compile(s), failure("UNIT_MISMATCH"));
  s.features[0].parameters.radius = { value: "0", unit: "mm" };
  assert.throws(() => compile(s), failure("GEOMETRY_INVALID"));
});
test("strip compilation accepts coplanar paths with pads and rejects tilted, empty or degenerate input", () => {
  const strip = (paths: string[][][], pads: any[] = []) => {
    const s = structuredClone(sphere);
    s.features = [
      {
        id: "trace",
        semantic_name: "trace",
        kind: "strip",
        owner_part: "part-main",
        local_frame: "world",
        authoritative_representation: "brep",
        parameters: {
          width: { value: "0.3", unit: "mm" },
          height: { value: "0.035", unit: "mm" },
        },
        expressions: {},
        parameter_sources: {},
        construction: { operator: "strip", paths, pads },
        depends_on: [],
        protected_relations: [],
      } as any,
    ];
    s.outputs = ["trace"];
    return s;
  };
  const plan = compile(
    strip(
      [
        [
          ["0", "0", "1.565"],
          ["5", "0", "1.565"],
          ["5", "4", "1.565"],
        ],
      ],
      [{ center: ["0", "0", "1.565"], width: "0.8", depth: "0.9" }],
    ),
  );
  assert.equal(plan.features[0].values.width, 0.3);
  assert.equal(plan.features[0].values.height, 0.035);
  assert.throws(
    () =>
      compile(
        strip([
          [
            ["0", "0", "0"],
            ["5", "0", "0.5"],
          ],
        ]),
      ),
    failure("GEOMETRY_INVALID"),
  );
  assert.throws(
    () =>
      compile(
        strip([
          [
            ["0", "0", "0"],
            ["0", "0", "0"],
          ],
        ]),
      ),
    failure("GEOMETRY_INVALID"),
  );
  assert.throws(
    () =>
      compile(
        strip(
          [
            [
              ["0", "0", "0"],
              ["5", "0", "0"],
            ],
          ],
          [{ center: ["0", "0", "0"], width: "0", depth: "1" }],
        ),
      ),
    failure("GEOMETRY_INVALID"),
  );
});
test("dirty subgraph is local and protected widths reject changes", () => {
  const p = Patch.parse({
    model_id: "m",
    base_revision: "r",
    idempotency_key: "k".repeat(16),
    operations: [
      {
        op: "set_parameter",
        feature_id: "feat-groove-07",
        parameter: "depth",
        expected: { value: "0.80", unit: "mm" },
        value: { value: "0.82", unit: "mm" },
      },
    ],
  });
  const plan = applyPatch(housing, p);
  assert.deepEqual(plan.dirty_features, ["feat-groove-07", "feat-hole-01"]);
  assert.equal(plan.hashes["feat-base"], compile(housing).hashes["feat-base"]);
  p.operations[0] = {
    op: "set_parameter",
    feature_id: "feat-groove-07",
    parameter: "width",
    expected: { value: "1.20", unit: "mm" },
    value: { value: "1.21", unit: "mm" },
  };
  assert.throws(() => applyPatch(housing, p), failure("OUT_OF_SCOPE"));
});
test("field semantics degrade under anisotropic scaling and local deltas", () => {
  const source = { op: "sphere", center: ["0", "0", "0"], radius: "1" };
  assert.equal(fieldContract(source).semantics, "exact_sdf");
  assert.equal(
    fieldContract({
      op: "transform",
      source,
      scale: ["1", "2", "1"],
      translation: ["0", "0", "0"],
    }).semantics,
    "bounded_distance_estimator",
  );
  const delta = fieldContract({
    op: "local_field_delta",
    source,
    center: ["0", "0", "0"],
    radius: "1",
    amplitude: "2",
  });
  assert.equal(delta.semantics, "general_implicit");
  assert.ok(delta.lipschitz > 5);
});
test("safe mathematical expressions validate dimensions, domains and depth", () => {
  const c = (v: string, unit: any = "1") => ({ constant: { value: v, unit } });
  assert.equal(
    evaluate({ fn: "+", args: [c("1", "mm"), c("20", "um")] }, {}).value,
    1.02,
  );
  assert.throws(
    () => evaluate({ fn: "sin", args: [c("2", "mm")] }, {}),
    failure("UNIT_MISMATCH"),
  );
  assert.throws(
    () => evaluate({ fn: "/", args: [c("2"), c("0")] }, {}),
    failure("CONSTRAINT_CONFLICT"),
  );
  assert.throws(
    () => evaluate({ fn: "sqrt", args: [c("-1")] }, {}),
    failure("UNIT_MISMATCH"),
  );
  let expression: any = c("1");
  for (let i = 0; i < 40; i++) expression = { fn: "abs", args: [expression] };
  assert.throws(() => evaluate(expression, {}), failure("BUDGET_EXCEEDED"));
});
test("bounded inverse solver computes an analytic dimension and diagnoses infeasibility", () => {
  const result = solveMonotone((x) => Math.PI * x * x, Math.PI * 25, 0, 10);
  assert.ok(Math.abs(result.value - 5) < 1e-8);
  assert.throws(
    () => solveMonotone((x) => x, 20, 0, 10),
    failure("CONSTRAINT_CONFLICT"),
  );
});
