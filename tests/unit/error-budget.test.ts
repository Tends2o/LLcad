import test from "node:test";
import assert from "node:assert/strict";
import {
  errorBudget,
  BUDGET_POLICY,
} from "../../packages/compiler/error-budget.js";
import { sphere, housing } from "../../scripts/fixtures.js";
import { compile } from "../../packages/compiler/index.js";
import {
  differenceSupports,
  compactSupportWithin,
} from "../../packages/compiler/field-regions.js";

test("the ledger only sums certified compatible bounds and reports kernel tolerances separately", () => {
  const facts = {
    sphere: { native_tolerances_mm: { vertex: 1e-7, edge: 1e-7, face: 1e-7 } },
  };
  const ledger = errorBudget(sphere, facts, [], []);
  assert.equal(ledger.status, "within_planned_budget");
  assert.equal(ledger.certified_chain_bound_mm, null);
  assert.ok(
    ledger.entries.some(
      (e) => e.stage === "kernel_boundary_tolerance" && !e.summable,
    ),
  );
  const refined = errorBudget(
    sphere,
    facts,
    [{ feature_id: "patch", report: { geometric_error_bound_mm: "0.0004" } }],
    [
      {
        stage: "export_quantization",
        object: "model.stl",
        bound_mm: 0.0003,
        guarantee: "bounded",
      },
    ],
  );
  assert.equal(refined.certified_chain_bound_mm, 0.0007);
  assert.equal(refined.status, "within_planned_budget");
  const tooMuch = errorBudget(
    sphere,
    facts,
    [{ feature_id: "patch", report: { geometric_error_bound_mm: "0.0008" } }],
    [],
  );
  assert.equal(tooMuch.status, "exceeded");
  assert.match(tooMuch.exceeded[0], /export reserve/);
  const coarseKernel = errorBudget(
    sphere,
    { sphere: { native_tolerances_mm: { vertex: 0.0006 } } },
    [],
    [],
  );
  assert.equal(coarseKernel.status, "exceeded");
  assert.equal(BUDGET_POLICY.kernel_share_of_tolerance, 0.5);
  const sampled = errorBudget(
    sphere,
    { sphere: { conversion_report: { measured_error: 0.02 } } },
    [],
    [],
  );
  assert.ok(
    sampled.uncertified_stages.includes("representation_conversion:sphere"),
  );
  assert.equal(sampled.certified_chain_bound_mm, null);
  const edited = errorBudget(
    sphere,
    {
      sphere: {
        surface_deviation: {
          status: "certified",
          certified_hausdorff_bound_mm: 0.04,
        },
      },
    },
    [],
    [],
  );
  assert.equal(edited.status, "within_planned_budget");
  assert.ok(
    edited.entries.some(
      (e) =>
        e.stage === "field_edit_surface_deviation_intended_change" &&
        !e.summable,
    ),
  );
});

test("compile reports conditioning and budget alternatives without relaxing tolerances", () => {
  const plan = compile(housing);
  assert.ok(plan.conditioning.ulp_to_tolerance_ratio < 1e-6);
  const field = {
    schema_version: "1",
    unit: "mm",
    profile: "render_surface",
    features: [
      {
        id: "f",
        semantic_name: "Feld",
        kind: "field",
        authoritative_representation: "implicit",
        parameters: {},
        construction: {
          operator: "field",
          expression: { op: "sphere", center: ["0", "0", "0"], radius: "5" },
          domain: { min: ["-100", "-100", "-100"], max: ["100", "100", "100"] },
          cell_size: { value: "0.01", unit: "mm" },
        },
      },
    ],
    outputs: ["f"],
  };
  try {
    compile(field);
    assert.fail("budget should be exceeded");
  } catch (e: any) {
    assert.equal(e.code, "BUDGET_EXCEEDED");
    assert.ok(
      e.details.alternatives.some(
        (a: any) => a.action === "increase_cell_size",
      ),
    );
    assert.ok(
      e.details.alternatives.every((a: any) => !/toleran/i.test(a.action)),
    );
  }
});

test("difference supports and containment proofs are exact on decimal inputs", () => {
  const source = { op: "sphere", center: ["0", "0", "0"], radius: "5" };
  const edit = {
    op: "local_field_delta",
    source,
    center: ["5", "0", "0"],
    radius: "1",
    amplitude: "0.04",
  };
  assert.deepEqual(differenceSupports(source, source), []);
  assert.equal(differenceSupports(source, edit)!.length, 1);
  assert.equal(differenceSupports(source, { ...source, radius: "5.1" }), null);
  const region = { center: ["5", "0", "0"], radius: "1.5" };
  assert.equal(compactSupportWithin(edit, region), true);
  assert.equal(
    compactSupportWithin({ ...edit, radius: "1.5000000000000001" }, region),
    false,
  );
  assert.equal(
    compactSupportWithin(
      {
        op: "local_deform",
        source,
        center: ["5", "0", "0"],
        radius: "1",
        displacement: ["0.3", "0.3", "0"],
      },
      region,
    ),
    false,
  );
  assert.equal(
    compactSupportWithin(
      {
        op: "local_deform",
        source,
        center: ["5", "0", "0"],
        radius: "1",
        displacement: ["0.2", "0.2", "0"],
      },
      region,
    ),
    true,
  );
});
