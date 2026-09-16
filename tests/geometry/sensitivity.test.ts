import test from "node:test";
import assert from "node:assert/strict";
import { setup, call, finish, importFixture, principal } from "../helpers.js";
import { housing, organic } from "../../scripts/fixtures.js";
import { ModelIR } from "../../packages/semantic-ir/schema.js";
import { id } from "../../packages/semantic-ir/hash.js";
const q = (value: string, unit = "mm") => ({ value, unit });
const param = (parameter: string) => ({ parameter });
const constant = (value: string) => ({ constant: q(value) });
const fn = (fn: string, ...args: any[]) => ({ fn, args });

test("plan sensitivity explains which registered quantities a changed parameter drives", async () => {
  const env = setup(),
    s = env.service;
  try {
    const m = await importFixture(s, housing);
    const plan = call(s, "cad_plan_edit", {
      model_id: m.model_id,
      base_revision: m.revision,
      idempotency_key: id("plan"),
      operations: [
        {
          op: "set_parameter",
          feature_id: "feat-groove-07",
          parameter: "depth",
          expected: q("0.80"),
          value: q("0.82"),
        },
      ],
    });
    const wall = plan.sensitivity.find(
      (e: any) => e.quantity === "remaining_wall",
    );
    assert.equal(wall.derivative, -1);
    assert.equal(wall.parameter, "depth");
    assert.equal(wall.method, "registered_analytic_dimension");
    assert.ok(
      plan.sensitivity.some(
        (e: any) => e.quantity === "depth" && e.derivative === 1,
      ),
    );
    assert.ok(
      plan.sensitivity.some(
        (e: any) =>
          e.feature_id === "feat-hole-01" &&
          e.quantity === "dependent_geometry",
      ),
    );
    assert.ok(!plan.sensitivity.some((e: any) => e.quantity === "width"));
    const expression = ModelIR.parse({
      ...housing,
      features: housing.features.map((f) =>
        f.id === "feat-hole-01"
          ? {
              ...f,
              expressions: {
                depth: fn("*", param("radius"), { constant: q("2", "1") }),
              },
            }
          : f,
      ),
      constraints: housing.constraints.filter(
        (c) => c.id !== "constraint-hole-radius",
      ),
    });
    const e = await importFixture(s, expression);
    const derived = call(s, "cad_plan_edit", {
      model_id: e.model_id,
      base_revision: e.revision,
      idempotency_key: id("plan-expression"),
      operations: [
        {
          op: "set_parameter",
          feature_id: "feat-hole-01",
          parameter: "radius",
          expected: q("1.5"),
          value: q("1.4"),
        },
      ],
    });
    const chain = derived.sensitivity.find(
      (x: any) => x.quantity === "parameter:depth",
    );
    assert.equal(chain.method, "expression_finite_difference");
    assert.ok(Math.abs(chain.derivative - 2) < 1e-6);
  } finally {
    await env.close();
  }
});

test("implicit level-set curvature is measured at an explicit world point and refused off-domain or on creases", async () => {
  const env = setup(),
    s = env.service;
  try {
    const m = await importFixture(s, organic);
    const measure = (args: any) =>
      call(s, "cad_measure", {
        model_id: m.model_id,
        revision: m.revision,
        feature_id: "organic",
        metric: "curvature",
        idempotency_key: id("curvature"),
        ...args,
      });
    const result = await finish(s, measure({ point: ["3", "4", "0"] }));
    assert.equal(
      result.method,
      "central_finite_difference_hessian_with_interval_regularity",
    );
    assert.ok(Math.abs(result.measurements.mean_curvature_per_mm + 0.2) < 1e-5);
    assert.ok(
      Math.abs(result.measurements.gaussian_curvature_per_mm2 - 0.04) < 1e-5,
    );
    assert.equal(result.certified_error_bound, null);
    assert.equal(
      s.call(principal, "cad_measure", {
        model_id: m.model_id,
        revision: m.revision,
        feature_id: "organic",
        metric: "curvature",
        idempotency_key: id("outside"),
        point: ["9", "0", "0"],
      }).errors[0].code,
      "OUT_OF_SCOPE",
    );
    assert.equal(
      s.call(principal, "cad_measure", {
        model_id: m.model_id,
        revision: m.revision,
        feature_id: "organic",
        metric: "curvature",
        idempotency_key: id("nopoint"),
      }).errors[0].code,
      "OUT_OF_SCOPE",
    );
    const creased = structuredClone(organic) as any;
    creased.features[0].construction.expression = {
      op: "union",
      a: { op: "box", center: ["0", "0", "0"], half_size: ["2", "2", "2"] },
      b: { op: "box", center: ["2", "0", "0"], half_size: ["2", "1", "1"] },
    };
    creased.constraints = [];
    const c = await importFixture(s, creased);
    const refused = await s.jobs.wait(
      principal,
      call(s, "cad_measure", {
        model_id: c.model_id,
        revision: c.revision,
        feature_id: "organic",
        metric: "curvature",
        idempotency_key: id("crease"),
        point: ["2", "1", "1"],
      }).job_id,
    );
    assert.equal(refused.status, "failed");
    assert.equal(refused.error.code, "PRECISION_UNSUPPORTED");
  } finally {
    await env.close();
  }
});

test("soft objectives with robust losses and solver diagnostics travel through the checked job contract", async () => {
  const env = setup(),
    s = env.service;
  try {
    const ir = ModelIR.parse({
      schema_version: "1",
      unit: "mm",
      features: [
        {
          id: "plate",
          semantic_name: "Platte",
          kind: "box",
          construction: { operator: "box" },
          parameters: { width: q("10"), depth: q("5"), height: q("2") },
        },
      ],
      outputs: ["plate"],
    });
    const m = await importFixture(s, ir);
    const area = {
      id: "area-48",
      relation: "eq",
      tolerance: "0.00000001",
      expression: fn(
        "-",
        fn(
          "/",
          fn("*", param("w"), param("d")),
          fn("*", constant("8"), constant("6")),
        ),
        { constant: q("1", "1") },
      ),
    };
    const solved = await finish(
      s,
      call(s, "cad_solve_constraints", {
        model_id: m.model_id,
        base_revision: m.revision,
        idempotency_key: id("solve"),
        problem: {
          variables: [
            {
              name: "w",
              feature_id: "plate",
              parameter: "width",
              expected: q("10"),
              lower: q("7"),
              upper: q("12"),
            },
            {
              name: "d",
              feature_id: "plate",
              parameter: "depth",
              expected: q("5"),
              lower: q("3"),
              upper: q("7"),
            },
          ],
          equations: [area, { ...area, id: "area-again" }],
          objectives: [
            {
              id: "prefer-square",
              expression: fn("-", fn("/", param("w"), param("d")), {
                constant: q("1", "1"),
              }),
              weight: "5",
              loss: "huber",
            },
          ],
          regularization: "0",
        },
      }),
    );
    assert.equal(solved.solver.diagnostics.jacobian_rank, 1);
    assert.equal(solved.solver.diagnostics.redundant_constraints.length, 1);
    assert.equal(solved.solver.objective_terms[0].loss, "huber");
    assert.ok(Math.abs(solved.solver.values.w - solved.solver.values.d) < 0.6);
    assert.equal(
      s.call(principal, "cad_solve_constraints", {
        model_id: m.model_id,
        base_revision: m.revision,
        idempotency_key: id("dimensional"),
        problem: {
          variables: [
            {
              name: "w",
              feature_id: "plate",
              parameter: "width",
              expected: q("10"),
              lower: q("7"),
              upper: q("12"),
            },
            {
              name: "d",
              feature_id: "plate",
              parameter: "depth",
              expected: q("5"),
              lower: q("3"),
              upper: q("7"),
            },
          ],
          equations: [area],
          objectives: [{ id: "bad", expression: param("w") }],
        },
      }).errors[0].code,
      "UNIT_MISMATCH",
    );
  } finally {
    await env.close();
  }
});
