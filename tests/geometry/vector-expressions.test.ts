import test from "node:test";
import assert from "node:assert/strict";
import fc from "fast-check";
import { evaluate } from "../../packages/compiler/expressions.js";
import { ModelIR } from "../../packages/semantic-ir/schema.js";
import { id } from "../../packages/semantic-ir/hash.js";
import { setup, call, finish, importFixture } from "../helpers.js";
const q = (value: string, unit = "mm") => ({ value, unit });
const c = (value: string, unit = "mm"): any => ({ constant: q(value, unit) });
const p = (parameter: string): any => ({ parameter });
const fn = (fn: string, ...args: any[]): any => ({ fn, args });
const vec = (values: number[], unit = "mm") =>
  fn("vec3", ...values.map((x) => c(String(x), unit)));

test("typed vector expressions preserve dimensions and Euclidean identities across randomized values", () => {
  fc.assert(
    fc.property(
      fc.tuple(
        ...Array.from({ length: 6 }, () =>
          fc.integer({ min: -1000, max: 1000 }),
        ),
      ),
      (values) => {
        const a = values.slice(0, 3),
          b = values.slice(3),
          av = vec(a),
          bv = vec(b);
        const product = evaluate(fn("dot", av, bv), {});
        assert.equal(
          product.value,
          a.reduce((sum, x, i) => sum + x * b[i], 0),
        );
        assert.equal(product.length, 2);
        const distance = evaluate(fn("norm", fn("-", av, bv)), {});
        assert.equal(distance.length, 1);
        assert.ok(
          Math.abs(distance.value - Math.hypot(...a.map((x, i) => x - b[i]))) <
            1e-10,
        );
        const scaled = evaluate(
          fn("norm", fn("/", fn("*", c("2", "1"), av), c("2", "1"))),
          {},
        );
        assert.ok(Math.abs(scaled.value - Math.hypot(...a)) < 1e-10);
        assert.ok(
          Math.abs(
            evaluate(fn("sqrt", fn("dot", av, av)), {}).value -
              Math.hypot(...a),
          ) < 1e-10,
        );
      },
    ),
    { seed: 6725, numRuns: 150 },
  );
  assert.equal(
    evaluate(
      fn("norm", fn("vec3", c("0.003", "m"), c("4000", "um"), c("12"))),
      {},
    ).value,
    13,
  );
  assert.equal(evaluate(fn("norm", vec([0, 0, 0])), {}).value, 0);
  assert.throws(
    () => evaluate(p("constructor"), {}),
    (e: any) => e.code === "INVALID_SCHEMA",
  );
  for (const invalid of [
    vec([1, 2, 3]),
    fn("vec3", c("1"), c("2", "deg"), c("3")),
    fn("dot", c("1"), vec([1, 2, 3])),
    fn("*", vec([1, 2, 3]), vec([3, 2, 1])),
    fn("/", c("1"), vec([1, 2, 3])),
    fn("sin", vec([1, 2, 3])),
  ]) {
    assert.throws(
      () => evaluate(invalid, {}),
      (e: any) => e.code === "UNIT_MISMATCH",
    );
  }
  assert.throws(
    () => evaluate(fn("norm", fn("/", vec([1, 2, 3]), c("0", "1"))), {}),
    (e: any) => e.code === "CONSTRAINT_CONFLICT",
  );
});

test("a stored vector norm drives independently measured native CAD dimensions", async () => {
  const env = setup(),
    s = env.service;
  try {
    const ir = ModelIR.parse({
      schema_version: "1",
      unit: "mm",
      features: [
        {
          id: "ball",
          semantic_name: "Formelkugel",
          kind: "sphere",
          parameters: { radius: q("1"), x: q("3"), y: q("4"), z: q("12") },
          expressions: {
            radius: fn("norm", fn("vec3", p("x"), p("y"), p("z"))),
          },
          construction: { operator: "sphere" },
        },
      ],
      outputs: ["ball"],
    });
    const m = await importFixture(s, ir),
      measured = call(s, "cad_inspect", {
        model_id: m.model_id,
        feature_id: "ball",
      });
    assert.ok(Math.abs(measured.known_facts.dimensions.radius - 13) < 1e-7);
    assert.ok(
      Math.abs(measured.known_facts.volume - (4 * Math.PI * 13 ** 3) / 3) <
        1e-6,
    );
  } finally {
    await env.close();
  }
});

test("four-variable vector constraints use native solver Jacobians and persist through validation and commit", async () => {
  const env = setup(),
    s = env.service;
  try {
    const ir = ModelIR.parse({
      schema_version: "1",
      unit: "mm",
      features: [
        {
          id: "plate",
          semantic_name: "Normquader",
          kind: "box",
          construction: { operator: "box" },
          parameters: { width: q("2.5"), depth: q("4.5"), height: q("11") },
        },
        {
          id: "scale",
          semantic_name: "Bezugsradius",
          kind: "sphere",
          construction: { operator: "sphere" },
          parameters: { radius: q("1.1"), x: q("20") },
        },
      ],
      outputs: ["plate", "scale"],
    });
    const m = await importFixture(s, ir);
    const v = fn(
      "/",
      fn("*", fn("vec3", p("w"), p("d"), p("h")), fn("/", p("r"), c("1"))),
      c("1", "1"),
    );
    const problem = {
      variables: [
        ["w", "plate", "width", "2.5", "1", "6"],
        ["d", "plate", "depth", "4.5", "1", "6"],
        ["h", "plate", "height", "11", "10", "14"],
        ["r", "scale", "radius", "1.1", "0.5", "1.5"],
      ].map(([name, feature_id, parameter, expected, lower, upper]) => ({
        name,
        feature_id,
        parameter,
        expected: q(expected),
        lower: q(lower),
        upper: q(upper),
      })),
      equations: [
        {
          id: "diagonal",
          expression: fn("-", fn("/", fn("norm", v), c("13")), c("1", "1")),
        },
        {
          id: "ratio",
          expression: fn("/", fn("dot", v, vec([4, -3, 0], "1")), c("1")),
        },
        {
          id: "height",
          expression: fn(
            "-",
            fn("/", fn("dot", v, vec([0, 0, 1], "1")), c("12")),
            c("1", "1"),
          ),
        },
        {
          id: "scale",
          expression: fn("-", fn("/", p("r"), c("1")), c("1", "1")),
        },
      ].map((e) => ({ ...e, relation: "eq", tolerance: "0.00000001" })),
      max_iterations: 100,
    };
    const solution = await finish(
      s,
      call(s, "cad_solve_constraints", {
        model_id: m.model_id,
        base_revision: m.revision,
        problem,
        idempotency_key: id("solve"),
      }),
    );
    const draft = call(s, "cad_apply_patch", {
      model_id: m.model_id,
      base_revision: m.revision,
      operations: solution.operations,
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
    const committed = call(s, "cad_commit", {
      ...binding,
      validation_digest: validation.digest,
      idempotency_key: id("commit"),
    });
    const actual = call(s, "cad_inspect", {
      model_id: m.model_id,
      feature_id: "plate",
    });
    for (const [metric, target] of Object.entries({
      width: 3,
      depth: 4,
      height: 12,
    }))
      assert.ok(
        Math.abs(actual.known_facts.dimensions[metric] - target) < 1e-6,
      );
    assert.equal(actual.protected_constraints.length, 4);
    assert.equal(solution.solver.global_optimum_claimed, false);
    const reference = call(s, "cad_inspect", {
      model_id: m.model_id,
      feature_id: "scale",
    });
    const broken = call(s, "cad_apply_patch", {
      model_id: m.model_id,
      base_revision: committed.revision,
      operations: [
        {
          op: "set_parameter",
          feature_id: "scale",
          parameter: "radius",
          expected: reference.parameters.radius,
          value: q("1.2"),
        },
      ],
      idempotency_key: id("break"),
    });
    await finish(s, broken);
    const failed = await finish(
      s,
      call(s, "cad_validate", {
        model_id: m.model_id,
        base_revision: committed.revision,
        transaction_id: broken.transaction_id,
        idempotency_key: id("check-broken"),
      }),
    );
    assert.equal(failed.status, "failed");
    assert.equal(
      call(s, "cad_get_model", { model_id: m.model_id }).revision,
      committed.revision,
    );
  } finally {
    await env.close();
  }
});
