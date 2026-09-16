import { ModelIR } from "../semantic-ir/schema.js";
import { compile, REGISTRY_HASH } from "../compiler/index.js";
import { quantity, equalQuantity } from "../semantic-ir/units.js";
import { hash } from "../semantic-ir/hash.js";
import { POLICY_HASH } from "../policy/index.js";
import { patchContinuity } from "../compiler/patches.js";
import { Decimal } from "decimal.js";
import { equationValues, checkEquation } from "../compiler/constraints.js";
import {
  sameFieldInRegion,
  differenceSupports,
  compactSupportWithin,
} from "../compiler/field-regions.js";
import { compileStructure } from "../compiler/structure.js";
import { MeshQuality } from "../semantic-ir/mesh.js";
import { NATIVE_MESH_SOURCE_HASH } from "../compiler/native-build.js";
import * as R from "../compiler/bernstein.js";
import { errorBudget } from "../compiler/error-budget.js";
/** Preserve every binary64 bit when comparing an interval to decimal intent. */
function binaryRational(value: number) {
  const bytes = Buffer.alloc(8);
  bytes.writeDoubleBE(value);
  const bits = bytes.readBigUInt64BE(),
    exponent = Number((bits >> 52n) & 2047n);
  const mantissa = (bits & ((1n << 52n) - 1n)) + (exponent ? 1n << 52n : 0n);
  const power = exponent ? exponent - 1075 : -1074;
  return R.fraction(
    (bits >> 63n ? -mantissa : mantissa) << BigInt(Math.max(power, 0)),
    1n << BigInt(Math.max(-power, 0)),
  );
}
function meshVolumeWithin(
  interval: unknown,
  target: string,
  tolerance: string,
) {
  if (
    !Array.isArray(interval) ||
    interval.length !== 2 ||
    !interval.every((n) => typeof n === "number" && Number.isFinite(n)) ||
    interval[0] > interval[1]
  )
    return false;
  const center = R.decimal(target),
    radius = R.decimal(tolerance);
  return (
    R.cmp(radius, R.zero) >= 0 &&
    R.cmp(binaryRational(interval[0]), R.sub(center, radius)) >= 0 &&
    R.cmp(binaryRational(interval[1]), R.add(center, radius)) <= 0
  );
}
type Check = {
  revision: string;
  engine_build: string;
  source_geometry_hash: string;
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
  refinements: { feature_id: string; report: any }[] = [],
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
      revision: candidate,
      engine_build: result.engine_build,
      source_geometry_hash: hash(result.facts[target] ?? result.facts),
      check_id: id,
      target,
      status: pass ? "passed" : "failed",
      measured,
      requested: requested ?? null,
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
    if (ir.profile === "precision_cad" && fact?.native_tolerances_mm) {
      const native = Object.values<number>(fact.native_tolerances_mm);
      add(
        "native-tolerance-" + f.id,
        f.id,
        native.every(
          (value) =>
            Number.isFinite(value) &&
            value >= 0 &&
            value <= quantity(ir.tolerance, "length"),
        ),
        fact.native_tolerances_mm,
        "OCCT_BRep_Tool_Tolerance",
        "native_boundary_tolerance_metadata",
        "reported_kernel_tolerances_not_a_global_surface_error_certificate",
        ir.tolerance,
      );
    }
    add(
      "geometry-" + f.id,
      f.id,
      !!fact?.valid,
      fact?.valid ?? null,
      f.authoritative_representation === "mesh"
        ? "indexed_mesh_nondegeneracy_and_unique_triangles"
        : f.authoritative_representation === "implicit"
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
  if (ir.profile === "watertight_solid") {
    add(
      "mesh-outputs",
      "model",
      plan.outputs.length > 0,
      plan.outputs.length,
      "declared_mesh_outputs",
      "all_outputs",
      "exact_for_declared_domain",
    );
    for (const target of [...plan.outputs, "model"]) {
      const fact = target === "model" ? result.aggregate : result.facts[target];
      const parsed = MeshQuality.safeParse(fact?.mesh_quality);
      const q = parsed.success ? parsed.data : null;
      const bound =
        !!q &&
        q.geometry_hash === fact.geometry_hash &&
        q.native.source_hash === NATIVE_MESH_SOURCE_HASH &&
        q.watertight_solid === Object.values(q.checks).every(Boolean);
      add(
        "mesh-report-" + target,
        target,
        bound,
        q
          ? {
              geometry_hash: q.geometry_hash,
              native_source_hash: q.native.source_hash,
            }
          : null,
        "strict_mesh_report_and_build_binding",
        "entire_indexed_mesh",
        "exact_for_declared_domain",
      );
      const decisions = q
        ? {
            nondegenerate: q.native.degenerate_triangles === 0,
            unique_triangles: q.topology.duplicate_triangles === 0,
            closed_vertex_manifold:
              q.topology.closed_vertex_manifold &&
              [
                q.topology.boundary_edges,
                q.topology.nonmanifold_edges,
                q.topology.nonmanifold_vertices,
                q.topology.isolated_vertices,
              ].every((n) => n === 0),
            consistent_orientation:
              q.topology.consistent_orientation &&
              q.topology.inconsistently_oriented_edges === 0,
            no_self_intersections:
              !q.native.self_intersections_found &&
              q.native.intersection_examples.length === 0,
            nested_shell_orientation:
              q.native.volume_checked &&
              q.native.nested_orientation_valid &&
              q.native.bounded_solids > 0 &&
              q.native.signed_volume_interval_mm3[0] > 0 &&
              q.native.components.length === q.topology.surface_components &&
              q.native.components.every(
                (c) => c.outward === (c.nesting_depth % 2 === 0),
              ),
          }
        : null;
      for (const key of [
        "nondegenerate",
        "unique_triangles",
        "closed_vertex_manifold",
        "consistent_orientation",
        "no_self_intersections",
        "nested_shell_orientation",
      ] as const)
        add(
          "mesh-" + key + "-" + target,
          target,
          bound && !!decisions?.[key] && !!q?.checks[key],
          decisions?.[key] ?? null,
          "indexed_topology_and_CGAL_EPECK_" + key,
          "entire_indexed_mesh",
          "exact_for_declared_domain",
        );
    }
  }
  if (ir.profile === "manufacturing_candidate" && ir.manufacturing) {
    const rules = ir.manufacturing;
    const wall = quantity(rules.minimum_wall, "length");
    for (const target of plan.outputs) {
      const report = result.facts[target]?.manufacturing_rules;
      add(
        "manufacturing-wall-" + target,
        target,
        typeof report?.wall_thickness?.minimum_mm === "number" &&
          report.wall_thickness.minimum_mm >= wall,
        report?.wall_thickness ?? null,
        "inward_normal_ray_to_first_exit_over_area_weighted_surface_samples",
        "finite_surface_samples_not_a_global_minimum_certificate",
        "sampled",
        { minimum_wall_mm: wall, process: rules.process },
      );
      if (rules.maximum_overhang) {
        const limit =
          (quantity(rules.maximum_overhang, "angle") * 180) / Math.PI;
        add(
          "manufacturing-overhang-" + target,
          target,
          typeof report?.overhang?.maximum_sampled_deg === "number" &&
            report.overhang.maximum_sampled_deg <= limit + 1e-9,
          report?.overhang ?? null,
          "downward_facing_surface_normal_samples_against_build_direction",
          "finite_surface_samples",
          "sampled",
          {
            maximum_overhang_deg: limit,
            build_direction: rules.build_direction,
          },
        );
      }
    }
    if (rules.minimum_hole_diameter) {
      const minimum = quantity(rules.minimum_hole_diameter, "length");
      for (const f of ir.features)
        if (f.construction.operator === "hole") {
          const diameter = 2 * quantity(f.parameters.radius, "length");
          add(
            "manufacturing-hole-" + f.id,
            f.id,
            diameter >= minimum,
            diameter,
            "declared_hole_parameter",
            "registered_hole_features_only",
            "exact_for_declared_domain",
            { minimum_hole_diameter_mm: minimum },
          );
        }
    }
  }
  if (
    ir.profile === "precision_cad" ||
    ir.profile === "manufacturing_candidate"
  )
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
          (report.first_derivatives_defined_at_interior_knots &&
            new Decimal(report.derivative_bound_mm_per_parameter).lte(
              tolerance,
            ))) &&
        (c.continuity !== "C2" ||
          (report.regularity_proved &&
            report.second_derivatives_defined_at_interior_knots &&
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
      const meshInterval =
        f.authoritative_representation === "mesh"
          ? fact?.mesh_quality?.native?.signed_volume_interval_mm3
          : null;
      add(
        c.id,
        f.id,
        typeof measured === "number" &&
          (f.authoritative_representation === "mesh"
            ? meshVolumeWithin(meshInterval, c.target.value, c.tolerance.value)
            : Math.abs(measured - target) <= tolerance),
        meshInterval ?? measured ?? null,
        f.authoritative_representation === "mesh"
          ? "exact_oriented_tetrahedra_with_outward_binary64_interval"
          : "OCCT_adaptive_volume_integration",
        "feature_volume",
        f.authoritative_representation === "mesh" ? "bounded" : "sampled",
        f.authoritative_representation === "mesh"
          ? { target: c.target, tolerance: c.tolerance }
          : target,
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
    } else if (c.kind === "surface_deviation") {
      const old = baseIR.features.find((x) => x.id === f.id);
      const maximum = quantity(c.maximum, "length");
      const report = fact?.surface_deviation;
      const unchanged =
        !!old &&
        old.construction.operator === "field" &&
        f.construction.operator === "field" &&
        hash(old.construction.expression) === hash(f.construction.expression) &&
        hash(old.construction.domain) === hash(f.construction.domain);
      const sameFrame =
        !!old &&
        old.local_frame === f.local_frame &&
        compileStructure(baseIR).placements[old.local_frame].hash ===
          compileStructure(ir).placements[f.local_frame].hash;
      const certified =
        !!report &&
        report.status === "certified" &&
        typeof report.certified_hausdorff_bound_mm === "number" &&
        report.certified_hausdorff_bound_mm <= maximum &&
        report.epsilon_mm <= maximum;
      add(
        c.id,
        f.id,
        !old || (sameFrame && (unchanged || certified)),
        !old
          ? { status: "no_reference_revision" }
          : unchanged
            ? {
                status: "identical_expression",
                certified_hausdorff_bound_mm: 0,
              }
            : (report ?? null),
        !old || unchanged
          ? "canonical_expression_identity"
          : "interval_arithmetic_gradient_flow_certificate",
        "entire_declared_field_domain",
        !old || unchanged || certified ? "bounded" : "sampled",
        { maximum_mm: maximum },
      );
    } else if (c.kind === "change_region") {
      const old: any = baseIR.features.find((x) => x.id === f.id);
      const current: any = f.construction;
      const region = { center: c.center, radius: c.radius };
      const sameFrame =
        !!old &&
        old.local_frame === f.local_frame &&
        (c.local_frame ?? "world") === f.local_frame &&
        compileStructure(baseIR).placements[old.local_frame].hash ===
          compileStructure(ir).placements[f.local_frame].hash &&
        hash(old.construction.domain) === hash(current.domain);
      const supports = old
        ? differenceSupports(old.construction.expression, current.expression)
        : [];
      const contained =
        supports !== null &&
        supports.every((node: any) => compactSupportWithin(node, region));
      add(
        c.id,
        f.id,
        !old || (sameFrame && contained),
        {
          differing_supports: supports === null ? null : supports.length,
          change_region: region,
          compute_region: {
            center: c.center,
            radius: (
              Number(c.radius) + Number(c.compute_margin ?? "0")
            ).toString(),
            note: "evaluation and certificates may use this larger region; geometry may only change inside change_region",
          },
          global_change: supports === null,
        },
        "exact_rational_compact_support_containment",
        "every_differing_compact_edit_between_base_and_candidate",
        "exact_for_declared_domain",
      );
    } else if (c.kind === "blend_free_region") {
      const report = fact?.blend_free_regions?.[c.id];
      add(
        c.id,
        f.id,
        report?.status === "certified",
        report ?? null,
        "interval_arithmetic_blend_inactivity",
        "entire_declared_region",
        report?.status === "certified" ? "bounded" : "sampled",
        { min: c.min, max: c.max },
      );
    } else if (c.kind === "protected_region") {
      const old = baseIR.features.find((x) => x.id === f.id);
      let proven = !old;
      const oldFrames = compileStructure(baseIR).placements,
        newFrames = compileStructure(ir).placements;
      const regionFrame = c.local_frame ?? "world";
      const sameFrame =
        !!old &&
        old.local_frame === f.local_frame &&
        oldFrames[old.local_frame].hash === newFrames[f.local_frame].hash;
      if (
        old &&
        f.construction.operator === "field" &&
        old.construction.operator === "field"
      ) {
        const sameDomain =
          hash(f.construction.domain) === hash(old.construction.domain);
        proven =
          sameDomain &&
          sameFrame &&
          f.local_frame === regionFrame &&
          sameFieldInRegion(
            old.construction.expression,
            f.construction.expression,
            c,
          );
      } else if (old)
        proven =
          !!baseResult?.facts?.[f.id] &&
          baseResult.facts[f.id].geometry_hash === fact?.geometry_hash;
      add(
        c.id,
        f.id,
        proven,
        proven,
        f.construction.operator === "field"
          ? "exact_rational_compact_support_disjoint_from_AABB"
          : "immutable_geometry_hash",
        f.construction.operator === "field"
          ? "entire_protected_region"
          : "entire_feature_implies_region_unchanged",
        "exact_for_declared_domain",
      );
    }
  }
  const budget = errorBudget(ir, result.facts, refinements);
  add(
    "error-budget",
    "model",
    budget.status === "within_planned_budget",
    budget,
    "compatible_error_budget_ledger",
    "listed_certified_and_reported_stages",
    budget.certified_chain_bound_mm === null ? "sampled" : "bounded",
    { tolerance_mm: budget.requested_tolerance_mm, policy: budget.policy },
  );
  const failed = checks.filter((c) => c.status === "failed");
  const body = {
    schema_version: "2",
    candidate_revision: candidate,
    ir_hash: hash(ir),
    geometry_digest: hash(result.facts),
    registry_hash: REGISTRY_HASH,
    policy_hash: POLICY_HASH,
    engine_build: result.engine_build,
    profile: ir.profile,
    status: failed.length ? "failed" : "checks_passed_within_profile",
    checks,
    error_budget: budget,
    warnings: ["Keine globale Fertigungs- oder Statikzertifizierung."],
    created_at: new Date().toISOString(),
  };
  return { ...body, digest: hash(body) };
}
export function compare(before: ModelIR, after: ModelIR, bg: any, ag: any) {
  const oldFrames = compileStructure(before).placements,
    newFrames = compileStructure(after).placements;
  return {
    structure_changed:
      hash(before.structure ?? null) !== hash(after.structure ?? null),
    changed_features: after.features
      .filter(
        (f) =>
          hash(f) !==
            hash(before.features.find((x) => x.id === f.id) ?? null) ||
          oldFrames[f.local_frame]?.hash !== newFrames[f.local_frame]?.hash,
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
