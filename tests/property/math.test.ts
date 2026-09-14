import test from "node:test";
import assert from "node:assert/strict";
import fc from "fast-check";
import {
  multiply,
  inverse,
  normalTransform,
  dot,
  bsplineBasis,
  bezier,
  Mat3,
} from "../../packages/compiler/math.js";
import {
  compile,
  applyPatch,
  fieldContract,
} from "../../packages/compiler/index.js";
import { sphere } from "../../scripts/fixtures.js";
import { Patch } from "../../packages/semantic-ir/schema.js";
test("affine inverse and inverse-transpose normals preserve orthogonality", () => {
  fc.assert(
    fc.property(
      fc.integer({ min: 1, max: 100 }),
      fc.integer({ min: 1, max: 100 }),
      fc.integer({ min: 1, max: 100 }),
      (a, b, c) => {
        const m: Mat3 = [
            [a, 1, 0],
            [0, b, 1],
            [0, 0, c],
          ],
          v: [number, number, number] = [2, 3, -4],
          back = multiply(inverse(m), multiply(m, v));
        assert.ok(back.every((x, i) => Math.abs(x - v[i]) < 1e-9));
        assert.ok(
          Math.abs(dot(normalTransform(m, [0, 0, 1]), multiply(m, [1, 0, 0]))) <
            1e-9,
        );
      },
    ),
    { seed: 2036, numRuns: 100 },
  );
});
test("B-spline partition of unity and Bézier endpoint invariants", () => {
  const knots = [0, 0, 0, 0, 1, 2, 3, 3, 3, 3];
  for (let i = 0; i <= 100; i++) {
    const u = (i * 3) / 100;
    const sum = Array.from({ length: 6 }, (_, j) =>
      bsplineBasis(j, 3, knots, u),
    ).reduce((a, b) => a + b);
    assert.ok(Math.abs(sum - 1) < 1e-12);
  }
  assert.deepEqual(
    bezier(
      [
        [0, 0, 0],
        [1, 3, 0],
        [2, 0, 0],
      ],
      0,
    ),
    [0, 0, 0],
  );
  assert.deepEqual(
    bezier(
      [
        [0, 0, 0],
        [1, 3, 0],
        [2, 0, 0],
      ],
      1,
    ),
    [2, 0, 0],
  );
});
test("stored expression drives geometry and rejects dimensional mistakes or cycles", () => {
  const ir = structuredClone(sphere);
  ir.features[0].parameters.x = { value: "3", unit: "mm" };
  ir.features[0].expressions.radius = {
    fn: "*",
    args: [{ parameter: "x" }, { constant: { value: "2", unit: "1" } }],
  };
  const c = compile(ir);
  assert.equal(c.features[0].values.radius, 6);
  assert.equal(c.ir.features[0].parameters.radius.value, "6.000000000000");
  ir.features[0].expressions.x = { parameter: "radius" };
  assert.throws(
    () => compile(ir),
    (e: any) => e.code === "CONSTRAINT_CONFLICT",
  );
});
test("local deformation rejects folding and provides a conservative inverse Lipschitz bound", () => {
  const source = { op: "sphere", center: ["0", "0", "0"], radius: "2" };
  const result = fieldContract({
    op: "local_deform",
    source,
    center: ["2", "0", "0"],
    radius: "2",
    displacement: ["0.1", "0", "0"],
  });
  assert.equal(result.semantics, "general_implicit");
  assert.ok(result.lipschitz > 1);
  assert.throws(
    () =>
      fieldContract({
        op: "local_deform",
        source,
        center: ["2", "0", "0"],
        radius: "1",
        displacement: ["2", "0", "0"],
      }),
    (e: any) => e.code === "CONSTRAINT_CONFLICT",
  );
});
