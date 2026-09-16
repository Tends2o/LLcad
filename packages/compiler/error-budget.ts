import { quantity } from "../semantic-ir/units.js";
import type { ModelIR } from "../semantic-ir/schema.js";
/** Error budget ledger (Bauplan 16.1, 16.2): separate tolerance kinds, compatible sums only.
 *
 * Only certified bounds on the same metric (Euclidean surface distance in mm, on the
 * same object chain) are added. Reported kernel tolerances, sampled measurements and
 * field residuals are listed but never summed into a certificate.
 */
export const BUDGET_POLICY = {
  version: 1,
  metric: "euclidean_surface_distance_mm",
  kernel_share_of_tolerance: 0.5,
  export_reserve_share_of_tolerance: 0.25,
  addition_rule:
    "only certified bounds with compatible metric and object are summed; sampled values are reported separately",
};
export type LedgerEntry = {
  stage: string;
  object: string;
  metric: string;
  unit: "mm";
  bound_mm: number | null;
  guarantee: "certified" | "bounded" | "reported_kernel_tolerance" | "sampled";
  summable: boolean;
};
export type Loss = {
  stage: string;
  object: string;
  bound_mm: number;
  guarantee: "certified" | "bounded" | "sampled";
};
export function errorBudget(
  ir: ModelIR,
  facts: Record<string, any> | null,
  refinements: { feature_id: string; report: any }[] = [],
  exportLosses: Loss[] = [],
) {
  const tolerance = quantity(ir.tolerance, "length");
  const entries: LedgerEntry[] = [
    {
      stage: "design_tolerance",
      object: "model",
      metric: BUDGET_POLICY.metric,
      unit: "mm",
      bound_mm: tolerance,
      guarantee: "certified",
      summable: false,
    },
  ];
  const kernel = Object.entries(facts ?? {})
    .flatMap(([fid, fact]: [string, any]) =>
      fact?.native_tolerances_mm
        ? [
            {
              fid,
              value: Math.max(
                ...Object.values<number>(fact.native_tolerances_mm),
              ),
            },
          ]
        : [],
    )
    .sort((a, b) => b.value - a.value)[0];
  if (kernel)
    entries.push({
      stage: "kernel_boundary_tolerance",
      object: kernel.fid,
      metric: "native_boundary_gap_mm",
      unit: "mm",
      bound_mm: kernel.value,
      guarantee: "reported_kernel_tolerance",
      summable: false,
    });
  for (const r of refinements)
    entries.push({
      stage: "nurbs_refinement_rounding",
      object: r.feature_id,
      metric: BUDGET_POLICY.metric,
      unit: "mm",
      bound_mm: Number(r.report.geometric_error_bound_mm),
      guarantee: "certified",
      summable: true,
    });
  for (const [fid, fact] of Object.entries(facts ?? {}) as [string, any][]) {
    const report = fact?.surface_deviation;
    // A certified deviation between two revisions measures the intended change, not an
    // approximation error of the candidate; it is listed for context but never summed.
    if (report)
      entries.push({
        stage: "field_edit_surface_deviation_intended_change",
        object: fid,
        metric: BUDGET_POLICY.metric,
        unit: "mm",
        bound_mm:
          report.status === "certified"
            ? report.certified_hausdorff_bound_mm
            : null,
        guarantee: report.status === "certified" ? "certified" : "sampled",
        summable: false,
      });
    const conversion = fact?.conversion_report;
    if (conversion)
      entries.push({
        stage: "representation_conversion",
        object: fid,
        metric: BUDGET_POLICY.metric,
        unit: "mm",
        bound_mm: conversion.measured_error ?? null,
        guarantee: "sampled",
        summable: false,
      });
  }
  for (const loss of exportLosses)
    entries.push({
      stage: loss.stage,
      object: loss.object,
      metric: BUDGET_POLICY.metric,
      unit: "mm",
      bound_mm: loss.bound_mm,
      guarantee: loss.guarantee,
      summable: loss.guarantee !== "sampled",
    });
  const exceeded: string[] = [];
  if (
    kernel &&
    kernel.value > tolerance * BUDGET_POLICY.kernel_share_of_tolerance
  )
    exceeded.push(
      `kernel_boundary_tolerance ${kernel.value} exceeds ${BUDGET_POLICY.kernel_share_of_tolerance} of tolerance ${tolerance}`,
    );
  const modelStages = entries.filter(
    (e) => e.summable && !e.stage.startsWith("export_"),
  );
  const modelSum = modelStages.reduce((sum, e) => sum + (e.bound_mm ?? 0), 0);
  if (
    modelSum >
    tolerance * (1 - BUDGET_POLICY.export_reserve_share_of_tolerance)
  )
    exceeded.push(
      `certified model-stage bounds ${modelSum} leave less than the export reserve of ${BUDGET_POLICY.export_reserve_share_of_tolerance} of tolerance ${tolerance}`,
    );
  const exportStages = entries.filter(
    (e) => e.summable && e.stage.startsWith("export_"),
  );
  const total =
    modelSum + exportStages.reduce((sum, e) => sum + (e.bound_mm ?? 0), 0);
  if (total > tolerance)
    exceeded.push(`certified chain ${total} exceeds tolerance ${tolerance}`);
  const uncertified = entries.filter(
    (e) =>
      e.metric === BUDGET_POLICY.metric &&
      !e.summable &&
      e.stage !== "design_tolerance",
  );
  return {
    policy: BUDGET_POLICY,
    requested_tolerance_mm: tolerance,
    entries,
    certified_chain_bound_mm:
      modelStages.length + exportStages.length ? total : null,
    chain_note:
      "The certified chain covers the listed certified stages only; native construction carries reported kernel tolerances, not a certified surface bound.",
    uncertified_stages: uncertified.map((e) => e.stage + ":" + e.object),
    exceeded,
    status: exceeded.length ? "exceeded" : "within_planned_budget",
  };
}
