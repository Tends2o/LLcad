import test from "node:test";
import assert from "node:assert/strict";
import fc from "fast-check";
import { equalQuantity, quantity } from "../../packages/semantic-ir/units.js";
import { hash } from "../../packages/semantic-ir/hash.js";
test("unit equivalence over 300 random decimal scales", () => {
  fc.assert(
    fc.property(fc.integer({ min: -100000, max: 100000 }), (n) => {
      assert.ok(
        equalQuantity(
          { value: String(n), unit: "mm" },
          { value: String(n * 1000), unit: "um" },
        ),
      );
    }),
    { numRuns: 300, seed: 2036 },
  );
});
test("canonical digest ignores object key order and distinguishes values", () => {
  fc.assert(
    fc.property(fc.integer(), fc.integer(), (a, b) => {
      assert.equal(hash({ a, b }), hash({ b, a }));
      if (a !== b) assert.notEqual(hash({ a }), hash({ a: b }));
    }),
    { numRuns: 200, seed: 2036 },
  );
});
