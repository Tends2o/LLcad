import { z } from "zod";
import { AccessPayload } from "./access.js";
import {
  Constraint,
  Feature,
  Frame,
  Id,
  ModelStructure,
  PatchOperation,
  ToolName,
} from "./schema.js";

// Open records are confined to declared diagnostic/engine metadata. Tool roots,
// transaction bindings, pages, artifacts and validation evidence are closed.
const text = z.string();
const strings = z.array(text);
const count = z.number().int().nonnegative();
export const Digest = text.regex(/^[a-f0-9]{64}$/);
export const JsonData = z.json();
export const Metadata = z.record(text, JsonData);
const point = z.tuple([z.number(), z.number(), z.number()]);
const bounds = z.tuple([
  z.number(),
  z.number(),
  z.number(),
  z.number(),
  z.number(),
  z.number(),
]);
const pageOffset = count.nullable();
const quality = z.enum(["preview_only", "checks_passed_within_profile"]);
const profile = z.enum([
  "precision_cad",
  "render_surface",
  "watertight_solid",
  "manufacturing_candidate",
]);
const source = Feature.shape.purpose.unwrap();
const parameters = Feature.shape.parameters;
const expressions = Feature.shape.expressions.removeDefault();
const constraints = z.array(Constraint);
const actions = { recommended_next_actions: strings };
export const ErrorDetail = z.strictObject({
  code: text.min(1),
  message: text,
  details: Metadata,
});
export const ValidationCheck = z.strictObject({
  check_id: text,
  target: text,
  method: text,
  guarantee: z.enum([
    "sampled",
    "bounded",
    "exact_for_declared_domain",
    "reported_kernel_tolerances_not_a_global_surface_error_certificate",
  ]),
  coverage: text,
  status: z.enum(["passed", "failed"]),
  measured: JsonData,
  requested: JsonData.optional(),
  error_bound: z.number().nonnegative().nullable(),
});
export const ValidationCheckV2 = ValidationCheck.extend({
  requested: JsonData,
  revision: Id,
  engine_build: text,
  source_geometry_hash: Digest,
});
export const LegacyValidationReport = z.strictObject({
  candidate_revision: Id,
  ir_hash: Digest,
  geometry_digest: Digest,
  registry_hash: Digest,
  policy_hash: Digest,
  engine_build: text,
  profile,
  status: z.enum(["failed", "checks_passed_within_profile"]),
  digest: Digest,
  checks: z.array(ValidationCheck).min(1),
  warnings: strings,
  created_at: z.iso.datetime(),
});
export const ValidationReportV2 = LegacyValidationReport.extend({
  schema_version: z.literal("2"),
  checks: z.array(ValidationCheckV2).min(1),
  error_budget: Metadata.optional(),
});
export const ValidationReport = z.union([
  LegacyValidationReport,
  ValidationReportV2,
]);
// The digest still identifies the full immutable resource, not this check page.
const checkPage = {
  check_count: count,
  validation_uri: text.regex(
    /^cad:\/\/transactions\/[a-zA-Z0-9_-]+\/validation$/,
  ),
};
export const ValidationSummary = z.union([
  LegacyValidationReport.extend({
    ...checkPage,
    checks: z.array(ValidationCheck).max(16),
  }),
  ValidationReportV2.extend({
    ...checkPage,
    checks: z.array(ValidationCheckV2).max(16),
  }),
]);
const artifactLink = {
  artifact_id: Id,
  uri: text.regex(/^cad:\/\/artifacts\/[a-zA-Z0-9_-]+\/manifest$/),
  download: text.regex(/^\/api\/artifacts\/[a-zA-Z0-9_-]+$/),
  hash: Digest,
};
export const Artifact = z.strictObject({
  ...artifactLink,
  mime: text,
  size: count,
  manifest: Metadata,
});
export const ArtifactResource = Artifact.omit({ uri: true, download: true });
const exported = {
  artifacts: z.array(Artifact).min(1),
  package_manifest: z.strictObject(artifactLink),
};
const placement = z.strictObject({
  rotation: z.tuple([point, point, point]),
  translation: point,
  path: z.array(Id),
  hash: Digest,
});
const estimate = z.strictObject({
  features: count,
  instances: count,
  maximum_seconds: z.number().nonnegative(),
});
const counts = { assemblies: count, parts: count, frames: count };
const binding = { model_id: Id, revision: Id };
const baseBinding = { model_id: Id, base_revision: Id };
const queued = z.strictObject({
  status: z.literal("queued"),
  model_id: Id.nullable(),
  job_id: Id,
  transaction_id: Id.nullable(),
  committed: z.literal(false),
  ...actions,
});
const candidate = queued.extend({
  transaction_id: Id,
  candidate_revision: Id,
  ...baseBinding,
  repair_attempt: z
    .strictObject({
      attempt: count,
      of_transaction: Id,
      cause: text,
      remaining: count,
      intent_comparison: z.strictObject({
        original_changed_features: z.array(Id),
        this_changed_features: z.array(Id),
        same_targets: z.boolean(),
      }),
      previous_attempts: z.array(
        z.strictObject({
          transaction_id: Id,
          cause: text.nullable(),
          state: text,
          cost_seconds: z.number().nonnegative().nullable(),
        }),
      ),
    })
    .optional(),
});
const planned = z.strictObject({
  status: z.literal("planned"),
  ...baseBinding,
  target_registry_hash: Digest,
  source_ir_hash: Digest,
  candidate_ir_hash: Digest,
  feature_count: count,
  protected_constraints: constraints,
  estimate,
  geometry_may_change: z.literal(true),
  ...actions,
  committed: z.literal(false),
});
const metrics = Metadata.describe(
  "Measured worker resource counters; keys depend on the registered engine.",
);
const facts = Metadata.describe(
  "Registered engine facts, with finite JSON values. Native and implicit facts have different fields.",
);
export const NativeWorkerResult = z.strictObject({
  status: z.enum(["candidate_ready", "succeeded"]),
  facts: z.record(Id, facts),
  aggregate: Metadata.nullable(),
  files: strings,
  engine_build: text,
  metrics,
  blobs: z.record(text, Digest),
});
const evaluated = z.strictObject({
  status: z.literal("candidate_ready"),
  transaction_id: Id,
  candidate_revision: Id,
  model_id: Id,
  changed_features: z.array(Id),
  dependent_features: z.array(Id),
  measurements: facts.nullable(),
  metrics,
  validation_status: z.literal("required"),
  committed: z.literal(false),
  ...actions,
});
const solver = z.strictObject({
  status: z.literal("converged"),
  values: z.record(text, z.number()),
  residuals: z.array(
    z.strictObject({
      id: Id,
      residual: z.number(),
      violation: z.number().nonnegative(),
      tolerance: z.number().nonnegative(),
    }),
  ),
  iterations: count,
  expression_evaluations: count,
  engine: text,
  global_optimum_claimed: z.literal(false),
  objective_terms: z.array(
    z.strictObject({
      id: Id,
      value: z.number(),
      loss: z.enum(["none", "huber", "cauchy"]),
    }),
  ),
  diagnostics: Metadata,
});
const solved = z.strictObject({
  status: z.literal("succeeded"),
  ...baseBinding,
  solver,
  committed: z.literal(false),
  operations: z.array(PatchOperation).min(1),
  ...actions,
});
const measured = z.strictObject({
  status: z.literal("succeeded"),
  ...binding,
  measurements: z.strictObject({ distance: z.number().nonnegative() }),
  unit: z.literal("mm"),
  method: text,
  coverage: text,
});
const pair = z.tuple([z.number(), z.number()]);
const surfaceSample = z.strictObject({
  kind: z.literal("surface"),
  point_mm: point,
  uv: pair,
  uv_bounds: z.tuple([z.number(), z.number(), z.number(), z.number()]),
  direction_kind: z.literal("oriented_face_normal"),
  direction: point,
});
const curveSample = z.strictObject({
  kind: z.literal("curve"),
  point_mm: point,
  curve_parameter: z.number().min(0).max(1),
  native_parameter: z.number(),
  direction_kind: z.literal("oriented_curve_tangent"),
  direction: point,
});
const differential = z.strictObject({
  status: z.literal("succeeded"),
  ...binding,
  metrics,
  method: z.literal("OCCT_native_differential_geometry"),
  coverage: z.literal("specified_local_parameter_points"),
  certified_error_bound: z.null(),
});
const sample = z.union([surfaceSample, curveSample]);
const implicitCurvature = z.strictObject({
  status: z.literal("succeeded"),
  ...binding,
  metrics,
  metric: z.literal("curvature"),
  unit: z.literal("per_mm_and_per_mm2"),
  method: z.literal(
    "central_finite_difference_hessian_with_interval_regularity",
  ),
  coverage: z.literal("single_point_and_its_two_step_cell"),
  certified_error_bound: z.null(),
  domain_frame: Id,
  measurements: Metadata,
});
const primitiveFit = z.strictObject({
  status: z.literal("succeeded"),
  ...binding,
  metrics,
  metric: z.literal("fit_primitives"),
  unit: z.literal("mm"),
  method: z.literal("normal_region_growing_with_least_squares_primitive_fits"),
  coverage: text,
  certified_error_bound: z.null(),
  guarantee: z.literal("sampled"),
  measurements: Metadata,
});
const sampledAnalysis = (metric: string, method: z.ZodTypeAny) =>
  z.strictObject({
    status: z.literal("succeeded"),
    ...binding,
    metrics,
    metric: z.literal(metric),
    unit: z.literal("mm"),
    method,
    coverage: text,
    certified_error_bound: z.null(),
    guarantee: z.enum(["sampled", "bounded", "not_certified"]),
    measurements: Metadata,
  });
const surfaceDistance = sampledAnalysis(
  "surface_distance",
  z.literal("sampled_chamfer_hausdorff_exact_extrema_and_boolean_iou"),
);
const wallThickness = sampledAnalysis("wall_thickness", text);
const blendActivity = sampledAnalysis(
  "blend_activity",
  z.literal("interval_arithmetic_blend_inactivity"),
).extend({ domain_frame: Id });
const motionClearance = z.strictObject({
  status: z.literal("succeeded"),
  ...binding,
  metrics,
  metric: z.literal("clearance"),
  unit: z.literal("mm"),
  method: z.literal(
    "OCCT_BRepExtrema_DistShapeShape_along_sampled_linear_motion",
  ),
  coverage: text,
  certified_error_bound: z.null(),
  motion_or_global_wall_certificate: z.literal(false),
  measurements: Metadata,
});
const analysed = z.union([
  implicitCurvature,
  primitiveFit,
  surfaceDistance,
  wallThickness,
  blendActivity,
  motionClearance,
  differential.extend({
    metric: z.literal("curvature"),
    unit: z.literal("per_mm_and_per_mm2"),
    measurements: z.union([
      surfaceSample.extend({
        principal_curvatures_per_mm: pair,
        mean_curvature_per_mm: z.number(),
        gaussian_curvature_per_mm2: z.number(),
        signed_convention: z.literal("OCCT_SLProps_with_face_orientation"),
      }),
      curveSample.extend({
        curvature_per_mm: z.number().nonnegative(),
        torsion_per_mm: z.number().nullable(),
        torsion_status: z.enum(["defined", "undefined_or_C3_unavailable"]),
      }),
    ]),
  }),
  differential.extend({
    metric: z.literal("angle"),
    unit: z.literal("rad"),
    measurements: z.strictObject({
      angle_rad: z.number().min(0).max(Math.PI),
      angle_deg: z.number().min(0).max(180),
      first: sample,
      second: sample,
    }),
  }),
  z.strictObject({
    status: z.literal("succeeded"),
    ...binding,
    metrics,
    metric: z.literal("surface_deviation"),
    unit: z.literal("mm"),
    method: z.literal("interval_arithmetic_gradient_flow_certificate"),
    coverage: z.literal("entire_declared_domain"),
    certified_error_bound: z.number().nonnegative().nullable(),
    guarantee: z.enum(["bounded", "not_certified"]),
    domain_frame: Id,
    measurements: Metadata,
  }),
  z.strictObject({
    status: z.literal("succeeded"),
    ...binding,
    metrics,
    metric: z.literal("clearance"),
    unit: z.literal("mm"),
    method: z.literal("OCCT_BRepExtrema_DistShapeShape"),
    coverage: z.literal("two_static_BRep_occupied_regions"),
    certified_error_bound: z.null(),
    motion_or_global_wall_certificate: z.literal(false),
    measurements: z.strictObject({
      clearance_mm: z.number().nonnegative(),
      contains_or_intersects_solid: z.boolean(),
      requested_minimum_mm: z.number().nonnegative(),
      minimum_satisfied: z.boolean(),
      contact_within_native_tolerance: z.boolean(),
      penetration_depth_mm: z.null(),
    }),
  }),
]);
const rendered = z.strictObject({
  status: z.literal("succeeded"),
  artifacts: z.array(Artifact).min(1),
  metrics,
});
const nativeExported = rendered.extend({
  result_schema_version: z.literal("1"),
  package_manifest: exported.package_manifest,
});
// Read-only compatibility for exports persisted before result versioning and
// package manifests. New workers must satisfy nativeExported instead.
export const LegacyExportResult = rendered.extend({
  package_manifest: exported.package_manifest.optional(),
});
const probed = z.strictObject({
  status: z.literal("succeeded"),
  ...baseBinding,
  structure: Metadata,
  continuation: z.union([
    candidate,
    z.strictObject({ status: z.literal("failed"), error: ErrorDetail }),
  ]),
  metrics,
});
export const JobResultSchemas = {
  evaluate: evaluated,
  validate: ValidationReport,
  solve: solved,
  measure: measured,
  analysis: analysed,
  render: rendered,
  export: nativeExported,
  probe: probed,
};
export type JobKind = keyof typeof JobResultSchemas;
const jobResult = z.union([
  evaluated,
  ValidationReport,
  ValidationSummary,
  solved,
  measured,
  analysed,
  rendered,
  nativeExported,
  LegacyExportResult,
  probed,
]);
export const JobView = z.strictObject({
  job_id: Id,
  model_id: Id,
  transaction_id: Id.nullable(),
  status: z.enum(["queued", "running", "succeeded", "failed", "cancelled"]),
  attempts: count,
  phase: z
    .enum([
      "queued",
      "native_execution",
      "validating",
      "persisting",
      "succeeded",
      "failed",
      "cancelled",
    ])
    .nullable(),
  budget_seconds: z.number().positive().nullable(),
  started_at: z.iso.datetime().nullable(),
  heartbeat_at: z.iso.datetime().nullable(),
  finished_at: z.iso.datetime().nullable(),
  elapsed_seconds: z.number().nonnegative().nullable(),
  result: jobResult.nullable(),
  error: ErrorDetail.nullable(),
});
const face = z.strictObject({
  face_id: Id,
  origins: z
    .array(
      z.strictObject({
        key: text,
        feature_id: Id,
        role: text,
        occurrences: z
          .array(z.strictObject({ feature_id: Id, index: count }))
          .optional(),
      }),
    )
    .max(16),
  area: z.number().nonnegative(),
  center: point,
  bounds: bounds.nullable(),
  surface: text,
  uv_bounds: z
    .tuple([z.number(), z.number(), z.number(), z.number()])
    .optional(),
  adjacent_face_ids: z.array(Id).max(256).optional(),
  unit: z.literal("mm"),
  area_unit: z.literal("mm2"),
});
const inspect = z.strictObject({
  ...binding,
  selected_entities: z.array(Id).min(1),
  unit: z.literal("mm"),
  selection_handle: Id,
  selected_face: face.extend({ geometry_feature_id: Id }).nullable(),
  face_page: z.strictObject({
    geometry_feature_id: Id,
    total: count,
    faces: z.array(face).max(16),
    next_offset: pageOffset,
  }),
  role: text,
  quality,
  profile,
  known_facts: facts.nullable(),
  purpose: source.nullable(),
  parameters,
  expressions,
  parameter_sources: Feature.shape.parameter_sources.removeDefault(),
  construction_summary: Feature.shape.construction,
  local_frame: Id,
  owner_part: Id,
  context_hash: Digest,
  frame_to_world: placement,
  protected_constraints: constraints,
  likely_dependencies: z.array(Id),
  lineage: Feature.shape.lineage.unwrap().nullable(),
  assumptions: strings,
  uncertainty: strings,
  available_edit_operations: z.array(
    z.union([
      z.strictObject({
        op: z.enum([
          "set_feature_context",
          "set_construction",
          "set_surface_poles",
          "insert_surface_knots",
        ]),
      }),
      z.strictObject({
        op: z.enum(["set_parameter", "set_expression"]),
        parameter: text,
      }),
      z.strictObject({
        op: z.literal("set_pattern_occurrence"),
        index_base: z.literal(0),
        override_null: text,
      }),
    ]),
  ),
  construction_hash: Digest,
  pattern_contract: z
    .strictObject({
      source_feature: Id,
      index_base: z.literal(0),
      placement: text,
      coordinate_frame: Id,
      override_translation: text,
      default_geometry: text,
      composition: text,
    })
    .nullable(),
  transformation_contract: z
    .strictObject({
      orientation: z.enum(["preserving", "reversing"]),
      determinant: z.strictObject({ numerator: text, denominator: text }),
      inverse_spectral_norm_upper: text,
      field_distance_scale_lower: text,
      normal_rule: text,
      native_floating_point_error_bound: z.null(),
    })
    .nullable(),
  surface_poles_hash: Digest.nullable(),
  field_expression_hash: Digest.nullable(),
  quality_status: z
    .strictObject({
      dimensional_status: z.enum(["checks_passed", "failed", "not_evaluated"]),
      topology_status: z.enum(["checks_passed", "failed", "not_evaluated"]),
      manufacturing_status: z.enum([
        "not_certified",
        "rules_sampled",
        "not_evaluated",
      ]),
      source: text,
      check_ids: z.array(text).max(64),
    })
    .nullable(),
  selection_anchor: z
    .strictObject({
      point_mm: point,
      normal: point.nullable(),
      barycentric: z.tuple([z.number(), z.number(), z.number()]).nullable(),
      triangle_index: count.nullable(),
      local_frame: Id,
      geometry_feature_id: Id,
      face_id: Id,
      view_relative: z
        .strictObject({
          facing_camera: z.boolean().nullable(),
          note: text,
        })
        .nullable(),
    })
    .nullable(),
  sections: strings,
});
const summary = z.strictObject({
  ...binding,
  name: text,
  purpose: text,
  head_revision: Id,
  unit: z.literal("mm"),
  quality,
  build_compatibility: z.union([
    z.strictObject({
      status: z.literal("current"),
      target_registry_hash: Digest,
    }),
    z.strictObject({
      status: z.enum(["rebuild_required", "unsupported_construction"]),
      target_registry_hash: Digest,
      diagnostic: ErrorDetail,
    }),
  ]),
  feature_count: count,
  structure: z.strictObject({
    hash: Digest,
    stored_explicitly: z.boolean(),
    ...counts,
  }),
  features: z.array(
    z.strictObject({
      id: Id,
      semantic_name: text,
      kind: text,
      operator: text,
      depends_on: z.array(Id),
      owner_part: Id,
      local_frame: Id,
      representation: z.enum(["brep", "implicit", "mesh"]),
      parameters: z.record(
        text,
        z.strictObject({ value: text, unit: text }),
      ),
    }),
  ),
  next_offset: pageOffset,
  outputs: z.array(Id),
  measurements: facts.nullable(),
  assumptions: strings,
});
/** State of the optional browser viewer after cad_viewer_open / cad_viewer_close. */
const viewer = z.strictObject({
  running: z.boolean(),
  transport: z.enum(["stdio", "http"]),
  url: text.nullable(),
  browser_launched: z.boolean(),
  message: text,
});
const capabilities = z.strictObject({
  application_version: text,
  ir_schema_version: z.literal("1"),
  operators: z.array(
    z.strictObject({
      name: text,
      version: count,
      parameters: strings,
      output: text,
    }),
  ),
  registry_hash: Digest,
  worker_build_hash: Digest,
  implementation_hash: Digest,
  policy_hash: Digest,
  formats: z.strictObject({
    import: strings,
    export: strings,
    step_structure: text,
    vdb_import: text,
  }),
  quality_profiles: z.array(profile),
  manufacturing_candidate: Metadata,
  mesh_validation: Metadata,
  analysis: Metadata,
  field_operators: strings,
  constraint_solver: Metadata,
  typed_expressions: Metadata,
  volumetric_export: Metadata,
  field_cache: Metadata,
  patch_continuity: Metadata,
  nurbs: Metadata,
  transformations: Metadata,
  field_values: Metadata,
  model_hierarchy: Metadata,
  thread_library: Metadata,
  sweep_contract: Metadata,
  field_certificates: Metadata,
  limits: z.record(text, z.number().nonnegative()),
  protocols: strings,
  host_test_status: text,
  face_selection: Metadata,
  worker_isolation: z.enum(["available", "unavailable"]),
  worker_pool: Metadata,
  observability: Metadata,
  pipeline_policy: Metadata,
  unsupported: strings,
});
const structure = z.strictObject({
  model_id: Id,
  revision: Id,
  unit: z.literal("mm"),
  structure_hash: Digest,
  stored_explicitly: z.boolean(),
  kind: z.enum(["project", "assembly", "part", "frame"]),
  total_matches: count,
  entries: z.array(
    z.strictObject({
      entity_id: Id,
      semantic_name: text,
      definition: z.union([
        ModelStructure.shape.project,
        ModelStructure.shape.assemblies.element,
        ModelStructure.shape.parts.element,
        Frame,
      ]),
      unit: z.literal("mm"),
      world_bounds: bounds.nullable(),
      bounds_coverage: text,
      feature_count: count,
      part_count: count,
      revision_quality: quality,
      output_status: z.enum([
        "evaluated_with_revision_profile",
        "not_evaluated",
      ]),
      relationships: z.array(Id),
      provenance: source.nullable(),
      placement,
    }),
  ),
  next_offset: pageOffset,
  counts: z.strictObject(counts),
});
const plan = z.strictObject({
  ...baseBinding,
  changed_features: z.array(Id),
  dependent_features: z.array(Id),
  dirty_features: z.array(Id),
  structure_hash: Digest,
  estimate,
  refinement_reports: z.array(
    z.strictObject({
      feature_id: Id,
      report: z.strictObject({
        method: text,
        coverage: text,
        parameterization_preserved: z.literal(true),
        geometric_error_bound_mm: text,
        native_floating_point_error_bound: z.null(),
        control_points_before: z.tuple([count, count]),
        control_points_after: z.tuple([count, count]),
      }),
    }),
  ),
  resolved_parameters: z.array(
    z.strictObject({ feature_id: Id, parameters, expressions }),
  ),
  protected_constraints: constraints,
  conditioning: z.strictObject({
    max_coordinate_magnitude_mm: z.number().nonnegative(),
    binary64_ulp_at_max_mm: z.number().positive(),
    tolerance_mm: z.number().positive(),
    ulp_to_tolerance_ratio: z.number().nonnegative(),
    rule: text,
  }),
  sensitivity: z.array(
    z.strictObject({
      feature_id: Id,
      quantity: text,
      parameter_feature: Id,
      parameter: text,
      derivative: z.number(),
      unit: text,
      method: z.enum([
        "registered_analytic_dimension",
        "expression_finite_difference",
      ]),
    }),
  ),
  committed: z.literal(false),
});
export const ToolPayloadSchemas = {
  cad_access: AccessPayload,
  cad_capabilities: capabilities,
  cad_list_models: z.strictObject({
    models: z.array(
      z.strictObject({
        model_id: Id,
        name: text,
        purpose_excerpt: text,
        revision: Id,
        created: z.iso.datetime(),
      }),
    ),
    total: count,
    next_offset: pageOffset,
  }),
  cad_create_model: z.strictObject({
    status: z.literal("created"),
    ...binding,
    quality: z.literal("preview_only"),
  }),
  cad_get_model: summary,
  cad_structure: structure,
  cad_find: z.strictObject({
    ...binding,
    ambiguity: z.boolean(),
    spatial_filter: text.nullable(),
    total_matches: count,
    matches: z.array(
      z.strictObject({
        feature_id: Id,
        semantic_name: text,
        selection_handle: Id,
        reason: text,
      }),
    ),
  }),
  cad_inspect: inspect,
  cad_measure: z.union([
    queued,
    z.strictObject({
      ...binding,
      measurements: z.union([z.number(), bounds, facts]),
      metric: text,
      measurement_frame: Id,
      unit: z.enum(["mm", "mm2", "mm3"]),
      coverage: text,
    }),
  ]),
  cad_solve_constraints: queued,
  cad_plan_edit: plan,
  cad_apply_patch: candidate,
  cad_validate: queued,
  cad_compare: z.strictObject({
    model_id: Id,
    from_revision: Id,
    to_revision: Id,
    structure_changed: z.boolean(),
    changed_features: z.array(Id),
    removed_features: z.array(Id),
    dimensions: z.array(
      z.strictObject({
        feature_id: Id,
        metric: text,
        before: z.number().nullable(),
        after: z.number(),
      }),
    ),
    volume_delta: z.number().nullable(),
    coverage: text,
  }),
  cad_commit: z.strictObject({
    status: z.literal("committed"),
    ...binding,
    transaction_id: Id,
    validation_digest: Digest,
    committed: z.literal(true),
    quality: z.literal("checks_passed_within_profile"),
  }),
  cad_discard: z.strictObject({
    status: z.literal("discarded"),
    transaction_id: Id,
    committed: z.literal(false),
  }),
  cad_rebuild: z.union([planned, candidate]),
  cad_revert: candidate,
  cad_import: z.union([candidate, queued]),
  cad_render: queued,
  cad_export: z.union([
    queued,
    z.strictObject({ status: z.literal("succeeded"), ...exported }),
  ]),
  cad_job_get: JobView,
  cad_job_cancel: JobView,
  cad_viewer_open: viewer,
  cad_viewer_close: viewer,
} satisfies Record<ToolName, z.ZodType>;
const envelope = {
  result_schema_version: z.literal("1"),
  status: z.literal("ok"),
  trace_id: Id,
  model_id: z.null(),
  revision: z.null(),
  candidate_revision: z.null(),
  transaction_id: z.null(),
  job_id: z.null(),
  measurements: z.null(),
  checks: z.tuple([]),
  warnings: strings,
  errors: z.array(ErrorDetail),
  assumptions: strings,
  artifacts: z.array(Artifact),
  ...actions,
};
export const FailureResponse = z.strictObject({
  ...envelope,
  status: z.literal("failed"),
  errors: z.array(ErrorDetail).min(1),
  committed: z.literal(false),
});
function enveloped(schema: z.ZodType): z.ZodType {
  if (schema instanceof z.ZodUnion)
    return z.union(
      (schema.options as z.ZodType[]).map(enveloped) as [
        z.ZodType,
        z.ZodType,
        ...z.ZodType[],
      ],
    );
  if (!(schema instanceof z.ZodObject))
    throw new Error("Tool payload must be an object or object union");
  return z.strictObject({ ...envelope, ...schema.shape });
}
export const ToolOutputSchemas = Object.fromEntries(
  Object.entries(ToolPayloadSchemas).map(([name, schema]) => [
    name,
    z.union([enveloped(schema), FailureResponse]),
  ]),
) as unknown as Record<ToolName, z.ZodType>;
/**
 * Rewrite tuple schemas into a form that draft-07 and draft 2020-12 validators
 * read the same way. Zod emits `prefixItems` with `items: false` (2020-12) or
 * `items` as a list with `additionalItems` (draft-07). A draft-07 validator
 * ignores `prefixItems` and would then reject every element; a 2020-12
 * validator such as Ajv 2020 rejects `items` as a list as an invalid schema.
 * The portable form keeps `prefixItems` for positional checks, validates every
 * element against the union of the tuple members and pins the length with
 * `minItems`/`maxItems`. An empty tuple becomes `items: false` with
 * `maxItems: 0`. Nodes already in this form are left unchanged.
 */
export function portableTuples<T>(schema: T): T {
  const same = (a: unknown, b: unknown) =>
    JSON.stringify(a) === JSON.stringify(b);
  const walk = (node: unknown): void => {
    if (Array.isArray(node)) {
      node.forEach(walk);
      return;
    }
    if (!node || typeof node !== "object") return;
    const s = node as Record<string, unknown>;
    const legacyList = Array.isArray(s.items) ? (s.items as unknown[]) : null;
    const prefix = Array.isArray(s.prefixItems)
      ? (s.prefixItems as unknown[])
      : legacyList;
    if (prefix) {
      const rest = legacyList ? s.additionalItems : s.items;
      const portable =
        !legacyList &&
        rest !== false &&
        typeof rest === "object" &&
        s.maxItems === prefix.length;
      delete s.additionalItems;
      if (prefix.length === 0) {
        delete s.prefixItems;
        s.items = false;
        s.maxItems = 0;
      } else if (!portable) {
        const open = rest !== false && rest && typeof rest === "object";
        const members = [...prefix, ...(open ? [rest] : [])];
        const unique = members.filter(
          (m, i) => members.findIndex((o) => same(o, m)) === i,
        );
        s.prefixItems = prefix;
        s.items = unique.length === 1 ? unique[0] : { anyOf: unique };
        if (s.minItems === undefined) s.minItems = prefix.length;
        if (!open) s.maxItems = prefix.length;
      }
    }
    for (const value of Object.values(s)) walk(value);
  };
  walk(schema);
  return schema;
}
export function outputJSONSchema(name: ToolName) {
  return {
    ...portableTuples(
      z.toJSONSchema(ToolOutputSchemas[name], { reused: "ref" }),
    ),
    type: "object" as const,
  };
}
