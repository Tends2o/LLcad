import { z } from "zod";
import { Id } from "./identifiers.js";
import { AccessTool } from "./access.js";
export { Id } from "./identifiers.js";
export const DecimalString = z
  .string()
  .max(48)
  .regex(/^-?(0|[1-9][0-9]*)(\.[0-9]+)?$/);
export const Quantity = z.strictObject({
  value: DecimalString,
  unit: z.enum(["m", "mm", "um", "rad", "deg", "1"]),
});
export type Quantity = z.infer<typeof Quantity>;
export const Vec3 = z.tuple([Quantity, Quantity, Quantity]);
export const Point = z.tuple([DecimalString, DecimalString, DecimalString]);
export const Matrix3 = z.tuple([Point, Point, Point]);
/** Explicit nonperiodic, clamped basis on the normalized parameter domain. */
export const SplineBasis = z.strictObject({
  degree: z.number().int().min(1).max(15),
  knots: z.array(DecimalString).min(2).max(256),
  multiplicities: z.array(z.number().int().min(1).max(16)).min(2).max(256),
});
const Source = z.strictObject({
  value: z.string().max(1000),
  status: z.enum(["user_declared", "imported", "hypothesis", "measured"]),
  evidence_id: Id.optional(),
});
export const ExpressionSchema: z.ZodType<any> = z.lazy(() =>
  z.union([
    z.strictObject({ constant: Quantity }),
    z.strictObject({ parameter: z.string().regex(/^[a-z][a-z0-9_]{0,63}$/) }),
    z.strictObject({
      fn: z.enum([
        "+",
        "-",
        "*",
        "/",
        "sqrt",
        "sin",
        "cos",
        "atan2",
        "min",
        "max",
        "abs",
        "clamp",
        "vec3",
        "dot",
        "norm",
      ]),
      args: z.array(ExpressionSchema).min(1).max(3),
    }),
  ]),
);
export const FieldNode: z.ZodType<any> = z.lazy(() =>
  z.discriminatedUnion("op", [
    z.strictObject({
      op: z.literal("gyroid"),
      period: DecimalString,
      origin: Point,
      threshold: DecimalString,
    }),
    z.strictObject({
      op: z.literal("convert_field_unit"),
      source: FieldNode,
      to: z.enum(["length", "dimensionless"]),
      reference_length: Quantity,
    }),
    z.strictObject({
      op: z.literal("sphere"),
      center: Point,
      radius: DecimalString,
    }),
    z.strictObject({ op: z.literal("box"), center: Point, half_size: Point }),
    z.strictObject({
      op: z.literal("plane"),
      normal: Point,
      offset: DecimalString,
    }),
    z.strictObject({
      op: z.literal("cylinder"),
      center: Point,
      radius: DecimalString,
      half_height: DecimalString,
    }),
    z.strictObject({
      op: z.literal("capsule"),
      start: Point,
      end: Point,
      radius: DecimalString,
    }),
    z.strictObject({
      op: z.literal("torus"),
      center: Point,
      major: DecimalString,
      minor: DecimalString,
    }),
    z.strictObject({
      op: z.enum(["union", "intersection", "difference"]),
      a: FieldNode,
      b: FieldNode,
    }),
    z.strictObject({
      op: z.literal("smooth_union"),
      a: FieldNode,
      b: FieldNode,
      k: DecimalString,
    }),
    z.strictObject({
      op: z.literal("offset"),
      source: FieldNode,
      distance: DecimalString,
    }),
    z.strictObject({
      op: z.literal("shell"),
      source: FieldNode,
      thickness: DecimalString,
      variations: z
        .array(
          z.strictObject({
            center: Point,
            radius: DecimalString,
            amplitude: DecimalString,
          }),
        )
        .max(32)
        .default([]),
    }),
    z.strictObject({
      op: z.literal("transform"),
      source: FieldNode,
      translation: Point,
      scale: Point,
    }),
    z.strictObject({
      op: z.literal("affine_transform"),
      source: FieldNode,
      matrix: Matrix3,
      translation: Point,
    }),
    z.strictObject({
      op: z.literal("rotate"),
      source: FieldNode,
      axis: Point,
      origin: Point,
      angle: Quantity,
    }),
    z.strictObject({
      op: z.literal("local_field_delta"),
      source: FieldNode,
      center: Point,
      radius: DecimalString,
      amplitude: DecimalString,
    }),
    z.strictObject({
      op: z.literal("local_deform"),
      source: FieldNode,
      center: Point,
      radius: DecimalString,
      displacement: Point,
    }),
  ]),
);
const PatternOverride = z.strictObject({
  source: Id.optional(),
  translation: Point.optional(),
});
const PatternOverrides = z
  .array(
    z.strictObject({
      index: z.int().min(0).max(9999),
      ...PatternOverride.shape,
    }),
  )
  .max(128)
  .optional();
export const Construction = z.discriminatedUnion("operator", [
  z.strictObject({ operator: z.enum(["point", "plane"]) }),
  z.strictObject({ operator: z.literal("line"), start: Point, end: Point }),
  z.strictObject({
    operator: z.literal("arc"),
    points: z.tuple([Point, Point, Point]),
  }),
  z.strictObject({ operator: z.enum(["trim_surface", "cap", "regularize"]) }),
  z.strictObject({
    operator: z.literal("sew"),
    make_solid: z.boolean().default(true),
  }),
  z.strictObject({
    operator: z.literal("thread"),
    mode: z.enum(["external", "internal"]).default("external"),
    handedness: z.enum(["right", "left"]).default("right"),
    standard: z.literal("custom").default("custom"),
  }),
  z.strictObject({
    operator: z.enum(["box", "sphere", "cylinder", "cone", "torus"]),
  }),
  z.strictObject({
    operator: z.enum(["union", "intersection", "difference", "assembly"]),
  }),
  z.strictObject({
    operator: z.literal("profile"),
    points: z.array(Point).min(3).max(256),
  }),
  z.strictObject({ operator: z.literal("circle") }),
  z.strictObject({
    operator: z.enum(["bezier", "bspline"]),
    points: z.array(Point).min(2).max(256),
  }),
  z.strictObject({
    operator: z.literal("nurbs_curve"),
    poles: z.array(Point).min(2).max(256),
    weights: z.array(DecimalString).min(2).max(256),
    basis: SplineBasis,
  }),
  z.strictObject({ operator: z.enum(["extrude", "revolve", "loft", "sweep"]) }),
  z.strictObject({ operator: z.enum(["hole", "pocket", "groove"]) }),
  z.strictObject({
    operator: z.enum(["fillet", "chamfer"]),
    edge_selector: z.enum(["all", "vertical", "horizontal"]),
  }),
  z.strictObject({
    operator: z.literal("shell"),
    opening: z.enum(["top", "bottom"]),
  }),
  z.strictObject({
    operator: z.enum(["transform", "instance", "mirror"]),
  }),
  z.strictObject({
    operator: z.literal("pattern"),
    overrides: PatternOverrides,
  }),
  z.strictObject({
    operator: z.literal("circular_pattern"),
    axis: Point,
    origin: Point,
    overrides: PatternOverrides,
  }),
  z.strictObject({
    operator: z.literal("affine_transform"),
    matrix: Matrix3,
    translation: Point,
  }),
  z.strictObject({ operator: z.literal("rotate"), axis: Point, origin: Point }),
  z.strictObject({
    operator: z.literal("nurbs_surface"),
    poles: z.array(z.array(Point).min(2).max(16)).min(2).max(16),
    weights: z.array(z.array(DecimalString).min(2).max(16)).min(2).max(16),
    u_basis: SplineBasis.optional(),
    v_basis: SplineBasis.optional(),
  }),
  z.strictObject({
    operator: z.literal("field"),
    expression: FieldNode,
    domain: z.strictObject({ min: Point, max: Point }),
    cell_size: Quantity,
  }),
  z.strictObject({
    operator: z.literal("imported"),
    artifact_id: Id,
    format: z.enum(["step", "stl"]),
    source_unit: z.enum(["mm", "m", "um"]),
  }),
]);
export const Feature = z.strictObject({
  id: Id,
  semantic_name: z.string().min(1).max(200),
  kind: z.string().max(64),
  owner_part: Id.default("part-main"),
  local_frame: Id.default("world"),
  purpose: Source.optional(),
  authoritative_representation: z
    .enum(["brep", "implicit", "mesh"])
    .default("brep"),
  parameters: z.record(z.string().regex(/^[a-z][a-z0-9_]{0,63}$/), Quantity),
  expressions: z
    .record(z.string().regex(/^[a-z][a-z0-9_]{0,63}$/), ExpressionSchema)
    .default({}),
  parameter_sources: z.record(z.string(), Source).default({}),
  construction: Construction,
  depends_on: z.array(Id).max(256).default([]),
  protected_relations: z.array(Id).max(64).default([]),
  lineage: z
    .strictObject({ created_by_operation: Id, source_revision: Id })
    .optional(),
});
export type Feature = z.infer<typeof Feature>;
export const Equation = z.strictObject({
  id: Id,
  expression: ExpressionSchema,
  relation: z.enum(["eq", "ge", "le"]),
  tolerance: DecimalString,
});
export const ParameterBinding = z.strictObject({
  feature_id: Id,
  parameter: z.string().regex(/^[a-z][a-z0-9_]{0,63}$/),
});
export const SolverProblem = z.strictObject({
  variables: z
    .array(
      ParameterBinding.extend({
        name: z.string().regex(/^[a-z][a-z0-9_]{0,63}$/),
        expected: Quantity,
        lower: Quantity,
        upper: Quantity,
      }),
    )
    .min(1)
    .max(12),
  equations: z.array(Equation).min(1).max(32),
  max_iterations: z.int().min(1).max(200).default(100),
});
export const Constraint = z.discriminatedUnion("kind", [
  Equation.extend({
    kind: z.literal("equation"),
    feature_id: Id,
    bindings: z.record(
      z.string().regex(/^[a-z][a-z0-9_]{0,63}$/),
      ParameterBinding,
    ),
  }),
  z.strictObject({
    id: Id,
    kind: z.literal("patch_continuity"),
    feature_id: Id,
    neighbor_feature_id: Id,
    continuity: z.enum(["C0", "C1", "C2"]),
    tolerance: Quantity,
  }),
  z.strictObject({
    id: Id,
    kind: z.literal("volume"),
    feature_id: Id,
    target: z.strictObject({ value: DecimalString, unit: z.literal("mm3") }),
    tolerance: z.strictObject({ value: DecimalString, unit: z.literal("mm3") }),
  }),
  z.strictObject({
    id: Id,
    kind: z.literal("dimension"),
    feature_id: Id,
    metric: z.string().max(64),
    target: Quantity,
    tolerance: Quantity,
  }),
  z.strictObject({
    id: Id,
    kind: z.literal("parameter"),
    feature_id: Id,
    parameter: z.string().max(64),
    target: Quantity,
    tolerance: Quantity,
  }),
  z.strictObject({
    id: Id,
    kind: z.literal("minimum"),
    feature_id: Id,
    metric: z.literal("remaining_wall"),
    target: Quantity,
  }),
  z.strictObject({
    id: Id,
    kind: z.literal("protected_parameter"),
    feature_id: Id,
    parameter: z.string().max(64),
  }),
  z.strictObject({
    id: Id,
    kind: z.literal("protected_feature"),
    feature_id: Id,
  }),
  z.strictObject({
    id: Id,
    kind: z.literal("protected_bounds"),
    feature_id: Id,
    tolerance: Quantity,
  }),
  z.strictObject({
    id: Id,
    kind: z.literal("protected_region"),
    feature_id: Id,
    local_frame: Id.optional(),
    min: Point,
    max: Point,
  }),
]);
const SemanticEntity = {
  id: Id,
  semantic_name: z.string().min(1).max(200),
  purpose: Source.optional(),
};
export const Frame = z.strictObject({
  ...SemanticEntity,
  parent: Id.default("world"),
  translation: Point,
  axis: Point,
  angle: Quantity,
});
export const ModelStructure = z.strictObject({
  project: z.strictObject(SemanticEntity),
  frames: z.array(Frame).max(128),
  assemblies: z
    .array(
      z.strictObject({
        ...SemanticEntity,
        parent_assembly: Id.optional(),
        local_frame: Id.default("world"),
      }),
    )
    .max(64),
  parts: z
    .array(
      z.strictObject({
        ...SemanticEntity,
        assembly: Id.optional(),
        local_frame: Id.default("world"),
        authoritative_representation: z.enum(["brep", "implicit", "mesh"]),
        outputs: z.array(Id).max(128),
      }),
    )
    .max(128),
});
export const ModelIR = z.strictObject({
  schema_version: z.literal("1"),
  unit: z.literal("mm"),
  structure: ModelStructure.optional(),
  features: z.array(Feature).max(256),
  outputs: z.array(Id).max(128),
  constraints: z.array(Constraint).max(256).default([]),
  assumptions: z.array(z.string().max(1000)).max(64).default([]),
  tolerance: Quantity.default({ value: "0.001", unit: "mm" }),
  profile: z.enum(["precision_cad", "render_surface"]).default("precision_cad"),
});
export type ModelIR = z.infer<typeof ModelIR>;
export const SetParameter = z.strictObject({
  op: z.literal("set_parameter"),
  feature_id: Id,
  parameter: z.string().regex(/^[a-z][a-z0-9_]{0,63}$/),
  expected: Quantity,
  value: Quantity,
});
export const PatchOperation = z.discriminatedUnion("op", [
  z.strictObject({
    op: z.literal("set_structure"),
    expected_hash: z.string().regex(/^[a-f0-9]{64}$/),
    structure: ModelStructure,
  }),
  z.strictObject({
    op: z.literal("set_feature_context"),
    feature_id: Id,
    expected_hash: z.string().regex(/^[a-f0-9]{64}$/),
    owner_part: Id,
    local_frame: Id,
  }),
  z.strictObject({
    op: z.literal("set_pattern_occurrence"),
    feature_id: Id,
    expected_hash: z.string().regex(/^[a-f0-9]{64}$/),
    index: z.int().min(0).max(9999),
    override: PatternOverride.nullable(),
  }),
  z.strictObject({
    op: z.literal("set_construction"),
    feature_id: Id,
    expected_hash: z.string().regex(/^[a-f0-9]{64}$/),
    construction: Construction,
  }),
  z.strictObject({
    op: z.literal("insert_surface_knots"),
    feature_id: Id,
    expected_hash: z.string().regex(/^[a-f0-9]{64}$/),
    direction: z.enum(["u", "v"]),
    knots: z.array(DecimalString).min(1).max(14),
    maximum_deviation: Quantity,
  }),
  z.strictObject({
    op: z.literal("set_surface_poles"),
    feature_id: Id,
    expected_hash: z.string().regex(/^[a-f0-9]{64}$/),
    poles: z.array(z.array(Point).min(2).max(16)).min(2).max(16),
  }),
  SetParameter,
  z.strictObject({
    op: z.literal("set_expression"),
    feature_id: Id,
    parameter: z.string().regex(/^[a-z][a-z0-9_]{0,63}$/),
    expected: Quantity,
    expression: ExpressionSchema,
  }),
  z.strictObject({
    op: z.literal("solve_volume"),
    feature_id: Id,
    parameter: z.enum(["radius", "width", "depth", "height"]),
    expected: Quantity,
    target: z.strictObject({ value: DecimalString, unit: z.literal("mm3") }),
    tolerance: z.strictObject({ value: DecimalString, unit: z.literal("mm3") }),
  }),
  z.strictObject({ op: z.literal("add_feature"), feature: Feature }),
  z.strictObject({
    op: z.literal("set_outputs"),
    outputs: z.array(Id).min(1).max(128),
  }),
  z.strictObject({ op: z.literal("add_constraint"), constraint: Constraint }),
  z.strictObject({
    op: z.literal("set_field"),
    feature_id: Id,
    expected_hash: z.string().regex(/^[a-f0-9]{64}$/),
    expression: FieldNode,
  }),
]);
export const WriteBinding = {
  model_id: Id,
  base_revision: Id,
  idempotency_key: z.string().min(16).max(128),
};
export const Patch = z.strictObject({
  ...WriteBinding,
  operations: z.array(PatchOperation).min(1).max(64),
  selection_handle: Id.optional(),
});
export type Patch = z.infer<typeof Patch>;
export const ReadBinding = { model_id: Id, revision: Id.optional() };
export const ToolSchemas = {
  cad_access: AccessTool,
  cad_capabilities: z.strictObject({}),
  cad_list_models: z.strictObject({
    query: z.string().max(200).default(""),
    offset: z.int().min(0).default(0),
    limit: z.int().min(1).max(32).default(16),
  }),
  cad_create_model: z.strictObject({
    name: z.string().min(1).max(200),
    purpose: z.string().max(1000).default(""),
    unit: z.literal("mm").default("mm"),
    profile: z
      .enum(["precision_cad", "render_surface"])
      .default("precision_cad"),
    idempotency_key: z.string().min(16).max(128),
  }),
  cad_get_model: z.strictObject({
    ...ReadBinding,
    offset: z.int().min(0).default(0),
    limit: z.int().min(1).max(64).default(32),
  }),
  cad_structure: z.strictObject({
    ...ReadBinding,
    kind: z.enum(["project", "assembly", "part", "frame"]).default("project"),
    entity_id: Id.optional(),
    query: z.string().max(200).default(""),
    offset: z.int().min(0).default(0),
    limit: z.int().min(1).max(16).default(8),
  }),
  cad_find: z.strictObject({
    ...ReadBinding,
    query: z.string().max(200).default(""),
    kind: z.string().max(64).optional(),
    owner_part: Id.optional(),
    point: Point.optional(),
    limit: z.int().min(1).max(32).default(16),
  }),
  cad_inspect: z.strictObject({
    ...ReadBinding,
    feature_id: Id.optional(),
    selection_handle: Id.optional(),
    face_id: z
      .string()
      .regex(/^face_[a-f0-9]{32}$/)
      .optional(),
    rebind: z.boolean().default(false),
    face_offset: z.int().min(0).default(0),
    face_limit: z.int().min(1).max(16).default(8),
  }),
  cad_measure: z.strictObject({
    ...ReadBinding,
    feature_id: Id.optional(),
    metric: z
      .enum([
        "all",
        "volume",
        "area",
        "bounds",
        "radius",
        "depth",
        "width",
        "remaining_wall",
        "distance",
        "angle",
        "curvature",
        "clearance",
      ])
      .default("all"),
    other_feature_id: Id.optional(),
    face_id: z
      .string()
      .regex(/^face_[a-f0-9]{32}$/)
      .optional(),
    other_face_id: z
      .string()
      .regex(/^face_[a-f0-9]{32}$/)
      .optional(),
    uv: z.tuple([DecimalString, DecimalString]).optional(),
    other_uv: z.tuple([DecimalString, DecimalString]).optional(),
    curve_parameter: DecimalString.optional(),
    other_curve_parameter: DecimalString.optional(),
    minimum_clearance: Quantity.optional(),
    idempotency_key: z.string().min(16).max(128).optional(),
  }),
  cad_plan_edit: Patch,
  cad_solve_constraints: z.strictObject({
    ...WriteBinding,
    problem: SolverProblem,
  }),
  cad_apply_patch: Patch,
  cad_validate: z.strictObject({ ...WriteBinding, transaction_id: Id }),
  cad_compare: z.strictObject({
    model_id: Id,
    from_revision: Id,
    to_revision: Id,
  }),
  cad_commit: z.strictObject({
    ...WriteBinding,
    transaction_id: Id,
    validation_digest: z.string().regex(/^[a-f0-9]{64}$/),
  }),
  cad_discard: z.strictObject({ ...WriteBinding, transaction_id: Id }),
  cad_revert: z.strictObject({ ...WriteBinding, target_revision: Id }),
  cad_rebuild: z.strictObject({
    ...WriteBinding,
    target_registry_hash: z.string().regex(/^[a-f0-9]{64}$/),
    mode: z.enum(["plan", "candidate"]).default("plan"),
  }),
  cad_render: z.strictObject({
    ...ReadBinding,
    idempotency_key: z.string().min(16).max(128),
    feature_id: Id.optional(),
    deflection: Quantity.default({ value: "0.05", unit: "mm" }),
  }),
  cad_import: z.strictObject({
    ...WriteBinding,
    artifact_id: Id,
    format: z.enum(["ir", "step", "stl"]),
    source_unit: z.enum(["mm", "m", "um"]),
  }),
  cad_export: z.strictObject({
    model_id: Id,
    revision: Id,
    format: z.enum(["ir", "step", "stl", "glb", "brep", "vdb"]),
    deflection: Quantity.default({ value: "0.01", unit: "mm" }),
    idempotency_key: z.string().min(16).max(128),
  }),
  cad_job_get: z.strictObject({ job_id: Id }),
  cad_job_cancel: z.strictObject({
    job_id: Id,
    idempotency_key: z.string().min(16).max(128),
  }),
};
export type ToolName = keyof typeof ToolSchemas;
/** MCP requires an explicit object root even for unions of object modes. */
export function inputJSONSchema(tool: ToolName) {
  return { ...z.toJSONSchema(ToolSchemas[tool]), type: "object" as const };
}
export const READ_TOOLS = new Set<ToolName>([
  "cad_capabilities",
  "cad_list_models",
  "cad_get_model",
  "cad_structure",
  "cad_find",
  "cad_inspect",
  "cad_measure",
  "cad_plan_edit",
  "cad_compare",
  "cad_job_get",
]);
