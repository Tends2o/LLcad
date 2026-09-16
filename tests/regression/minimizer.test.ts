import test from "node:test";
import assert from "node:assert/strict";
import { minimize, compileOracle } from "../../scripts/minimize-fixture.js";
import { housing } from "../../scripts/fixtures.js";

test("the fixture minimiser shrinks a failing IR to the feature that causes the compiler error", async () => {
  const broken = {
    ...housing,
    features: [
      ...housing.features,
      {
        id: "bad-union",
        semantic_name: "Vereinigung mit einem Eingang",
        kind: "union",
        parameters: {},
        construction: { operator: "union" },
        depends_on: ["feat-base"],
      },
    ],
    outputs: [...housing.outputs, "bad-union"],
  };
  assert.equal(compileOracle(broken), "INVALID_SCHEMA");
  const result = await minimize(broken, "INVALID_SCHEMA", compileOracle);
  assert.equal(result.features, 2);
  assert.deepEqual(result.ir.features.map((f: any) => f.id).sort(), [
    "bad-union",
    "feat-base",
  ]);
  assert.equal(result.constraints, 0);
  assert.equal(compileOracle(result.ir), "INVALID_SCHEMA");
  assert.ok(result.steps > 0);
  await assert.rejects(
    () => minimize(housing, "INVALID_SCHEMA", compileOracle),
    /keinen Fehler/,
  );
});
