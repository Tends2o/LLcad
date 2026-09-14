import { ModelIR } from "../semantic-ir/schema.js";
import { compile, REGISTRY_HASH } from "../compiler/index.js";
import { quantity, equalQuantity } from "../semantic-ir/units.js";
import { hash } from "../semantic-ir/hash.js";
import { POLICY_HASH } from "../policy/index.js";
import { patchContinuity } from "../compiler/patches.js";
import { Decimal } from "decimal.js";
import { equationValues, checkEquation } from "../compiler/constraints.js";
type Check = {
  check_id: string;
  target: string;
  method: string;
  guarantee: string;
  status: string;
  measured: unknown;
  requested?: unknown;
  coverage: string;
  error_bound: number | null;
};
export function validate(
  ir: ModelIR,
  result: any,
  baseIR: ModelIR,
  baseResult: any,
  candidate: string,
) {
  const checks: Check[] = [];
  const plan = compile(ir);
  function add(
    id: string,
    target: string,
    pass: boolean,
    measured: unknown,
    method: string,
    coverage: string,
    guarantee = "sampled",
    requested?: unknown,
  ) {
    checks.push({
      check_id: id,
      target,
      status: pass ? "passed" : "failed",
      measured,
      requested,
      method,
      coverage,
      guarantee,
      error_bound: null,
    });
  }
  add(
    "structure",
    "model",
    true,
    plan.ir.schema_version,
    "strict_ir_compiler",
    "all_features",
    "exact_for_declared_domain",
  );
  for (const f of ir.features) {
    const fact = result.facts[f.id];
    add(
      "geometry-" + f.id,
      f.id,
      !!fact?.valid,
      fact?.valid ?? null,
      f.authoritative_representation === "implicit"
        ? "analytic_field_contract"
        : "OCCT_BRepCheck_Analyzer",
      "operator_output",
    );
    const required: Record<string, string[]> = {
      box: ["width", "depth", "height"],
      sphere: ["radius"],
      cylinder: ["radius", "height"],
      groove: ["depth", "width"],
      hole: ["radius", "depth"],
      pocket: ["width", "length", "depth"],
    };
    for (const metric of required[f.construction.operator] ?? []) {
      const measured = fact?.dimensions?.[metric],
        target = quantity(f.parameters[metric], "length");
      add(
        "dimension-" + f.id + "-" + metric,
        f.id,
        typeof measured === "number" &&
          Math.abs(measured - target) <= plan.tolerance,
        measured ?? null,
        "OCCT_constructed_boundary_dimension",
        "feature_construction_stage",
        "sampled",
        target,
      );
    }
  }
  if (ir.profile === "precision_cad")
    add(
      "solid",
      "model",
      !!result.aggregate &&
        result.aggregate.precision_solid_only &&
        result.aggregate.solids > 0 &&
        result.aggregate.volume > 0,
      result.aggregate?.volume ?? null,
      "OCCT_volume_and_solid_count",
      "declared_outputs",
    );
  for (const c of ir.constraints) {
    const fact = result.facts[c.feature_id],
      f = ir.features.find((f) => f.id === c.feature_id)!;
    if (c.kind === "equation") {
      const result = checkEquation(c, equationValues(ir, c.bindings));
      add(
        c.id,
        f.id,
        result.passed,
        result,
        "typed_normalized_equation_recheck",
        "declared_parameter_relationship",
        "sampled",
      );
    } else if (c.kind === "patch_continuity") {
      const report = patchContinuity(
        f,
        ir.features.find((x) => x.id === c.neighbor_feature_id)!,
      );
      const tolerance = quantity(c.tolerance, "length");
      const pass =
        new Decimal(report.position_bound_mm).lte(tolerance) &&
        (c.continuity === "C0" ||
          new Decimal(report.derivative_bound_mm_per_parameter).lte(
            tolerance,
          )) &&
        (c.continuity !== "C2" ||
          (report.regularity_proved &&
            new Decimal(report.second_derivative_bound_mm_per_parameter2).lte(
              tolerance,
            )));
      add(
        c.id,
        f.id,
        pass,
        report,
        report.method,
        "entire_rational_IR_patch_seam_and_declared_derivatives",
        "exact_for_declared_domain",
        { continuity: c.continuity, tolerance_mm: tolerance },
      );
    } else if (c.kind === "volume") {
      const measured = fact?.volume,
        target = Number(c.target.value),
        tolerance = Number(c.tolerance.value);
      add(
        c.id,
        f.id,
        typeof measured === "number" &&
          Math.abs(measured - target) <= tolerance,
        measured ?? null,
        "OCCT_adaptive_volume_integration",
        "feature_volume",
        "sampled",
        target,
      );
    } else if (c.kind === "parameter" || c.kind === "protected_parameter") {
      const q = f.parameters[c.parameter],
        old = baseIR.features.find((x) => x.id === f.id)?.parameters[
          c.parameter
        ];
      if (c.kind === "protected_parameter")
        add(
          c.id,
          f.id,
          !old || equalQuantity(q, old),
          q,
          "decimal_parameter_equality",
          "parameter_only",
          "exact_for_declared_domain",
          old,
        );
      else {
        const v = quantity(q),
          target = quantity(c.target);
        add(
          c.id,
          f.id,
          Math.abs(v - target) <= quantity(c.tolerance),
          v,
          "typed_parameter_evaluator",
          "parameter_only",
          "exact_for_declared_domain",
          target,
        );
      }
    } else if (c.kind === "dimension" || c.kind === "minimum") {
      const measured = fact?.dimensions?.[c.metric] ?? fact?.[c.metric];
      const target = quantity(c.target);
      const pass =
        typeof measured === "number" &&
        Number.isFinite(measured) &&
        (c.kind === "minimum"
          ? measured >= target
          : Math.abs(measured - target) <= quantity(c.tolerance));
      add(
        c.id,
        f.id,
        pass,
        measured ?? null,
        "OCCT_boundary_dimension",
        c.metric === "remaining_wall"
          ? "planar_box_zone_only"
          : "feature_boundary",
        "sampled",
        target,
      );
    } else if (c.kind === "protected_feature") {
      const before = baseResult?.facts?.[f.id];
      add(
        c.id,
        f.id,
        !before || before.geometry_hash === fact?.geometry_hash,
        fact?.geometry_hash,
        "immutable_geometry_hash",
        "entire_feature",
        "exact_for_declared_domain",
      );
    } else if (c.kind === "protected_bounds") {
      const before = baseResult?.facts?.[f.id]?.bounds,
        after = fact?.bounds;
      const error =
        before && after
          ? Math.max(
              ...before.map((x: number, i: number) => Math.abs(x - after[i])),
            )
          : 0;
      add(
        c.id,
        f.id,
        !!after && error <= quantity(c.tolerance),
        error,
        "OCCT_bounding_box_comparison",
        "axis_aligned_extents_only",
      );
    } else if (c.kind === "protected_region") {
      const old = baseIR.features.find((x) => x.id === f.id);
      let proven = !old;
      if (
        old &&
        f.construction.operator === "field" &&
        old.construction.operator === "field"
      ) {
        const sameDomain =
          hash(f.construction.domain) === hash(old.construction.domain);
        let expression = f.construction.expression;
        const target = hash(old.construction.expression);
        proven = true;
        let iterations = 0;
        while (hash(expression) !== target && iterations++ < 32) {
          if (!["local_field_delta", "local_deform"].includes(expression.op)) {
            proven = false;
            break;
          }
          const center = expression.center.map(Number),
            min = c.min.map(Number),
            max = c.max.map(Number);
          const distance = Math.hypot(
            ...center.map((x: number, i: number) =>
              Math.max(min[i] - x, 0, x - max[i]),
            ),
          );
          const effectRadius =
            Number(expression.radius) +
            (expression.op === "local_deform"
              ? Math.hypot(...expression.displacement.map(Number))
              : 0);
          if (distance < effectRadius) {
            proven = false;
            break;
          }
          expression = expression.source;
        }
        proven = proven && sameDomain && hash(expression) === target;
      } else if (old) proven = hash(old) === hash(f);
      add(
        c.id,
        f.id,
        proven,
        proven,
        "compact_support_disjoint_from_AABB",
        "entire_protected_region",
        "exact_for_declared_domain",
      );
    }
  }
  const failed = checks.filter((c) => c.status === "failed");
  const body = {
    candidate_revision: candidate,
    ir_hash: hash(ir),
    geometry_digest: hash(result.facts),
    registry_hash: REGISTRY_HASH,
    policy_hash: POLICY_HASH,
    engine_build: result.engine_build,
    profile: ir.profile,
    status: failed.length ? "failed" : "checks_passed_within_profile",
    checks,
    warnings: ["Keine globale Fertigungs- oder Statikzertifizierung."],
    created_at: new Date().toISOString(),
  };
  return { ...body, digest: hash(body) };
}
export function compare(before: ModelIR, after: ModelIR, bg: any, ag: any) {
  return {
    changed_features: after.features
      .filter(
        (f) =>
          hash(f) !== hash(before.features.find((x) => x.id === f.id) ?? null),
      )
      .map((f) => f.id),
    removed_features: before.features
      .filter((f) => !after.features.some((x) => x.id === f.id))
      .map((f) => f.id),
    dimensions: after.features.flatMap((f) =>
      Object.entries(ag?.facts?.[f.id]?.dimensions ?? {}).map(
        ([metric, value]) => ({
          feature_id: f.id,
          metric,
          before: bg?.facts?.[f.id]?.dimensions?.[metric] ?? null,
          after: value,
        }),
      ),
    ),
    volume_delta:
      typeof bg?.aggregate?.volume === "number" &&
      typeof ag?.aggregate?.volume === "number"
        ? ag.aggregate.volume - bg.aggregate.volume
        : null,
    coverage: "declared_dimensions_and_per_feature_hashes",
  };
}
