import test from "node:test";
import assert from "node:assert/strict";
import { fieldContract } from "../../packages/compiler/index.js";
test("new field primitives retain their mathematical semantics and reject invalid domains", () => {
  for (const node of [
    { op: "plane", normal: ["0", "0", "4"], offset: "2" },
    { op: "cylinder", center: ["0", "0", "0"], radius: "2", half_height: "3" },
    {
      op: "capsule",
      start: ["0", "0", "0"],
      end: ["0", "0", "0"],
      radius: "1",
    },
  ])
    assert.deepEqual(fieldContract(node), {
      semantics: "exact_sdf",
      lipschitz: 1,
    });
  assert.throws(() =>
    fieldContract({ op: "plane", normal: ["0", "0", "0"], offset: "2" }),
  );
  assert.throws(() =>
    fieldContract({ op: "cylinder", radius: "2", half_height: "0" }),
  );
});
test("variable shell thickness has a global positivity and derivative bound; transforms do not promote general fields", () => {
  const source = { op: "sphere", center: ["0", "0", "0"], radius: "3" };
  const shell = {
    op: "shell",
    source,
    thickness: "0.4",
    variations: [{ center: ["3", "0", "0"], radius: "1", amplitude: "-0.2" }],
  };
  assert.deepEqual(fieldContract(shell), {
    semantics: "general_implicit",
    lipschitz: 1.2109375,
  });
  assert.throws(() => fieldContract({ ...shell, thickness: "0.1" }));
  assert.equal(
    fieldContract({
      op: "transform",
      source: shell,
      scale: ["1", "2", "3"],
      translation: ["0", "0", "0"],
    }).semantics,
    "general_implicit",
  );
});
