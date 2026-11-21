import { z } from "zod";
import { Decimal } from "decimal.js";
import { Id } from "./identifiers.js";
import { Setup as ReferenceSetup, Start as ReferenceStart, ReportQuery as ReferenceReport } from "../reference-review/schema.js";
import { BatchStart, BatchReport } from "../feature-work/schema.js";
import { FitParameters } from "../reference-review/fit-schema.js";
import { AccessTool } from "./access.js";
import { ViewInput, GatesInput, RegisterInput } from "../reference-review/gates-schema.js";
import { DrawingAnalyzeInput, DrawingRenderInput, DrawingPublishInput } from "../drawing/schema.js";
import { toolInputJSONSchema } from "./json-schema.js";
export { Id } from "./identifiers.js";
export const DecimalString = z
  .string()
  .max(48)
  .regex(/^-?(0|[1-9][0-9]*)(\.[0-9]+)?$/);
export const QuantityUnit = z.enum(["m", "mm", "um", "rad", "deg", "1"]);
export const Quantity = z.strictObject({
  value: DecimalString,
  unit: QuantityUnit,
});
/** Plain JSON number -> canonical decimal string (no exponent); anything else unchanged. */
export function canonicalDecimal(value: unknown): unknown {
  if (typeof value !== "number" || !Number.isFinite(value)) return value;
  const text = new Decimal(value).toFixed();
  return text === "-0" ? "0" : text;
}
/** Keys whose JSON numbers are genuine integers in the IR (never decimal strings). */
const INTEGER_KEYS = new Set([
  "degree",
  "multiplicities",
  "samples",
  "fill_holes_max_edges",
  "index",
  "version",
  "steps",
]);
/** Compact input: every JSON number where the IR expects a decimal string becomes one. */
export function decimalize(value: unknown, integers = false, key = ""): unknown {
  if (typeof value === "number")
    return integers && INTEGER_KEYS.has(key) ? value : canonicalDecimal(value);
  if (Array.isArray(value))
    return value.map((item) => decimalize(item, integers, key));
  if (value && typeof value === "object")
    return Object.fromEntries(
      Object.entries(value).map(([k, v]) => [k, decimalize(v, integers, k)]),
    );
  return value;
}
export const VariableName = z.string().regex(/^[a-z][a-z0-9_]{0,63}$/);
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
    z
      .strictObject({ var: VariableName })
      .describe("Model variable from ModelIR.variables (set_variable changes it; every feature using it rebuilds)."),
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
    z.strictObject({
      op: z.literal("sampled_grid"),
      artifact_id: Id,
      grid: z.string().min(1).max(200).optional(),
      lipschitz: DecimalString,
      value_unit: z.enum(["length", "dimensionless"]).default("length"),
      source_unit: z.enum(["mm", "m", "um"]).default("mm"),
      interpolation: z.literal("trilinear").default("trilinear"),
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
const Point2 = z.tuple([DecimalString, DecimalString]);
/** Named sketch/placement planes: construction XY mapped onto the local frame. */
export const PlanePreset = z.enum(["XY", "XZ", "YZ", "front", "side", "back"]);
/** A feature's own construction frame: origin plus x and z axes (orthogonal), in its local_frame. */
export const FeaturePlacement = z.strictObject({
  origin: Point,
  x_axis: Point,
  z_axis: Point,
});
const CornerRadius = DecimalString.optional();
export const SketchSegment = z.discriminatedUnion("type", [
  z.strictObject({ type: z.literal("line"), to: Point2, corner_radius: CornerRadius }),
  z.strictObject({
    type: z.literal("arc3"),
    via: Point2,
    to: Point2,
    corner_radius: CornerRadius,
  }),
  z.strictObject({
    type: z.literal("tangent_arc"),
    to: Point2,
    corner_radius: CornerRadius,
  }),
  z.strictObject({
    type: z.literal("center_arc"),
    center: Point2,
    to: Point2,
    direction: z.enum(["ccw", "cw"]).optional(),
    corner_radius: CornerRadius,
  }),
  z.strictObject({
    type: z.literal("spline"),
    through: z.array(Point2).max(64).optional(),
    to: Point2,
    corner_radius: CornerRadius,
  }),
]);
const PresetPlacement = {
  center: Point2.optional(),
  angle_deg: DecimalString.optional(),
};
export const SketchPreset = z.discriminatedUnion("kind", [
  z.strictObject({
    kind: z.literal("rounded_rect"),
    width: DecimalString,
    height: DecimalString,
    radius: DecimalString.optional(),
    /** Per-corner radii [bottom_left, bottom_right, top_right, top_left]. */
    radii: z.tuple([DecimalString, DecimalString, DecimalString, DecimalString]).optional(),
    ...PresetPlacement,
  }),
  z.strictObject({
    kind: z.literal("slot"),
    length: DecimalString,
    width: DecimalString,
    ...PresetPlacement,
  }),
  z.strictObject({
    kind: z.literal("ellipse"),
    rx: DecimalString,
    ry: DecimalString,
    ...PresetPlacement,
  }),
  z.strictObject({ kind: z.literal("circle"), radius: DecimalString, center: Point2.optional() }),
  z.strictObject({
    kind: z.literal("spline_through"),
    points: z.array(Point2).min(3).max(256),
    periodic: z.boolean().optional(),
  }),
  z.strictObject({
    kind: z.literal("superellipse"),
    a: DecimalString,
    b: DecimalString,
    exponent: DecimalString.optional(),
    samples: z.int().min(8).max(256).optional(),
    ...PresetPlacement,
  }),
]);
const SketchLoopFields = {
  start: Point2.optional(),
  segments: z.array(SketchSegment).min(1).max(256).optional(),
  /** Default corner radius for every vertex without its own corner_radius (0 = sharp). */
  corner_radius: DecimalString.optional(),
  preset: SketchPreset.optional(),
  /** Join the loop into ONE exact B-spline edge (loft sections of different presets). */
  single_edge: z.boolean().optional(),
};
export const SketchLoop = z.strictObject(SketchLoopFields);
/** Faces by provenance (the feature that made them, optional role) or by outward normal. */
export const FaceSelector = z.union([
  z.strictObject({ feature: Id, role: z.string().min(1).max(64).optional() }),
  z.strictObject({ normal: Point, angle_tolerance_deg: DecimalString.optional() }),
]);
export const TEXT_FONTS = [
  "DejaVu Sans",
  "DejaVu Sans Condensed",
  "Liberation Sans",
  "Liberation Sans Narrow",
  "Inter",
] as const;
/** Compile-time macro templates (packages/compiler/macros.ts). */
export const MACRO_TEMPLATES = [
  "ring",
  "knurl_ring",
  "lens",
  "indicator_row",
  "control_key",
  "usb_c_receptacle",
] as const;
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
    standard: z.enum(["custom", "iso_metric_basic"]).default("custom"),
    designation: z
      .string()
      .regex(/^M\d+(\.\d+)?(x\d+(\.\d+)?)?$/)
      .optional(),
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
  z.strictObject({
    operator: z.literal("sketch").describe(
      "Planar region with exact edges: start + segments (line | arc3 | tangent_arc | center_arc | spline, corner_radius per vertex) or one preset (rounded_rect, slot, ellipse, circle, spline_through, superellipse); holes subtract loops; single_edge joins a loop into one B-spline edge for lofts. 2D points [x, y].",
    ),
    /** Same as the feature's `plane`; give one or the other. */
    plane: PlanePreset.optional(),
    ...SketchLoopFields,
    holes: z.array(SketchLoop).max(32).optional(),
  }),
  z.strictObject({
    operator: z.literal("text").describe(
      "Relief lettering in ONE feature (fonts DejaVu Sans, Liberation Sans, Inter; + variants). Parameters cap_height, depth (+ emboss / - deboss, >= 0.05 mm), tracking, line_spacing, overlap. arc puts it on a circle (surface plane) or round a cylinder about Z. Optional target input: fused or cut in one Boolean.",
    ),
    string: z.string().min(1).max(512),
    font: z.enum(TEXT_FONTS).optional(),
    style: z
      .enum(["regular", "bold", "italic", "bold_italic", "medium", "semibold"])
      .optional(),
    align: z.enum(["left", "center", "right"]).optional(),
    valign: z.enum(["baseline", "center", "top"]).optional(),
    arc: z
      .strictObject({
        radius: DecimalString,
        center_angle_deg: DecimalString,
        side: z.enum(["outside", "inside"]).optional(),
        direction: z.enum(["cw", "ccw"]).optional(),
        surface: z.enum(["plane", "cylinder"]).optional(),
      })
      .optional(),
  }),
  z.strictObject({
    operator: z.literal("svg_emblem").describe(
      "Logo from the ORIGINAL SVG vectors (artifact_id or allowed path), element_filter by ids/classes/fills, scaled by width_mm or height_mm of the viewBox (fit_to), anchor, parameter depth +/-; optional target input.",
    ),
    artifact_id: Id.optional(),
    path: z.string().min(1).max(1024).optional(),
    /** Pins a path source: the file must still have this sha256. */
    sha256: z
      .string()
      .regex(/^[a-f0-9]{64}$/)
      .optional(),
    element_filter: z
      .strictObject({
        ids: z.array(z.string().min(1).max(200)).max(64).optional(),
        classes: z.array(z.string().min(1).max(200)).max(64).optional(),
        fills: z.array(z.string().min(1).max(64)).max(32).optional(),
        exclude_ids: z.array(z.string().min(1).max(200)).max(64).optional(),
      })
      .optional(),
    width_mm: DecimalString.optional(),
    height_mm: DecimalString.optional(),
    fit_to: z.enum(["viewbox", "selection"]).optional(),
    anchor: z
      .enum(["center", "bottom_left", "bottom_center", "top_left", "svg_origin"])
      .optional(),
  }),
  z.strictObject({
    operator: z.literal("strip"),
    paths: z.array(z.array(Point).min(2).max(256)).min(1).max(64),
    pads: z
      .array(
        z.strictObject({
          center: Point,
          width: DecimalString,
          depth: DecimalString,
        }),
      )
      .max(256)
      .optional(),
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
  z.strictObject({
    operator: z.literal("extrude"),
    /** '+' along the axis (default), '-' against it, 'both' symmetric (total height). */
    direction: z
      .enum(["+", "-", "both"])
      .optional()
      .describe("'+' along the axis (default), '-' against it, 'both' symmetric (total = height)."),
    /** Extrusion direction in construction coordinates (default +Z, or the normal of a placed profile). */
    axis: Point.optional().describe(
      "Extrusion direction (default construction +Z, or the plane normal of a placed profile/sketch input).",
    ),
  }),
  z.strictObject({
    operator: z.literal("revolve"),
    axis: Point.optional(),
    origin: Point.optional(),
  }),
  z.strictObject({
    operator: z.literal("loft"),
    ruled: z.boolean().optional(),
    check_compatibility: z.boolean().optional(),
  }),
  z.strictObject({
    operator: z.literal("sweep"),
    frame: z.enum(["corrected_frenet", "rotation_minimizing"]).optional(),
    self_contact_check: z.boolean().optional(),
    /** Guided sweep (third input = auxiliary spine): contact of the section with the guide. */
    guide_contact: z.enum(["none", "contact", "contact_on_border"]).optional(),
    curvilinear_equivalence: z.boolean().optional(),
  }),
  z.strictObject({ operator: z.literal("offset_solid") }),
  z.strictObject({
    operator: z.literal("mesh_repair"),
    remove_degenerate: z.boolean().optional(),
    remove_duplicates: z.boolean().optional(),
    orient: z.boolean().optional(),
    fill_holes_max_edges: z.int().min(0).max(256).optional(),
  }),
  z.strictObject({ operator: z.literal("remesh_region") }),
  z.strictObject({
    operator: z.literal("local_mesh_deform"),
    handles: z
      .array(z.strictObject({ point: Point, displacement: Point }))
      .min(1)
      .max(32),
  }),
  z.strictObject({ operator: z.literal("tessellate") }),
  z.strictObject({ operator: z.literal("extract_isosurface") }),
  z.strictObject({ operator: z.enum(["hole", "pocket", "groove"]) }),
  z.strictObject({
    operator: z.enum(["fillet", "chamfer"]),
    edge_selector: z.union([
      z.enum(["all", "vertical", "horizontal"]),
      z.strictObject({
        /** Edges where a face made by feature A meets a face made by feature B. */
        between_features: z
          .tuple([Id, Id])
          .describe("Edges where a face made by feature A meets a face made by feature B (face provenance of the input)."),
        roles: z
          .tuple([z.string().max(64).nullable(), z.string().max(64).nullable()])
          .optional(),
      }),
    ]),
  }),
  z.strictObject({
    operator: z.literal("shell"),
    opening: z
      .union([z.enum(["top", "bottom"]), FaceSelector])
      .describe("top/bottom planar face, or faces by provenance {feature, role?} or outward normal {normal}."),
  }),
  z.strictObject({
    operator: z.literal("draft").describe(
      "Taper faces (default walls parallel to pull_direction) by parameter angle about neutral_plane; positive angle tapers inward along the pull direction.",
    ),
    /** 'walls' (default): every face parallel to the pull direction. */
    faces: z.union([z.literal("walls"), FaceSelector]).optional(),
    pull_direction: Point.optional(),
    neutral_plane: z
      .strictObject({ point: Point.optional(), normal: Point.optional() })
      .optional(),
  }),
  z.strictObject({
    operator: z.literal("split_by_plane").describe(
      "Split a solid by a plane {point, normal}; keep both, above (normal side) or below. Colour zones / parting lines.",
    ),
    plane: z.strictObject({ point: Point.optional(), normal: Point }),
    keep: z.enum(["both", "above", "below"]).optional(),
  }),
  z.strictObject({
    operator: z.literal("knurl").describe(
      "Straight knurl (radius, height, count, depth, width?, inner_radius?) about Z at (x, y, z): without input one notched profile extruded once, with a target one multi-tool cut.",
    ),
    profile: z.enum(["v", "square"]).optional(),
  }),
  z.strictObject({
    operator: z.literal("cut_pattern").describe(
      "Copies of the tool (input 2) on a linear (dx/dy/dz) or circular (axis, origin, angle) pattern cut from (or united with) the target (input 1) in ONE Boolean.",
    ),
    kind: z.enum(["linear", "circular"]).optional(),
    axis: Point.optional(),
    origin: Point.optional(),
    operation: z.enum(["difference", "union"]).optional(),
  }),
  z.strictObject({
    operator: z.enum(["transform", "instance"]),
  }),
  z.strictObject({
    operator: z.literal("mirror"),
    /** Mirror plane (default: the YZ plane through the origin, x -> -x). */
    plane: z.strictObject({ normal: Point, point: Point.optional() }).optional(),
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
    extraction: z
      .strictObject({
        method: z
          .enum(["marching_tetrahedra", "dual_contouring"])
          .default("marching_tetrahedra"),
        pruning: z.enum(["lipschitz", "interval"]).default("lipschitz"),
      })
      .optional(),
  }),
  z.strictObject({
    operator: z.literal("imported"),
    artifact_id: Id,
    format: z.enum(["step", "stl", "vdb"]),
    source_unit: z.enum(["mm", "m", "um"]),
    /** XCAF label entry of a STEP prototype shape (structure-preserving assembly import). */
    component: z
      .string()
      .max(64)
      .regex(/^0(:[0-9]{1,6}){1,16}$/)
      .optional(),
    /** Named grid inside an OpenVDB file with several grids. */
    grid: z.string().min(1).max(200).optional(),
  }),
  z.strictObject({
    operator: z.literal("macro").describe(
      "Compile-time macro: the compiler expands it deterministically into primitive sub-features '<id>--<n>' (the last one keeps the macro's own id, so dependents and outputs reference the macro id). The stored IR keeps this call: numeric template parameters live in the feature's `parameters` (set_parameter, expressions and {var} work), string options in construction.params. Templates (version 1): ring, knurl_ring, lens, indicator_row, control_key, usb_c_receptacle; parameters and examples in llcad://operators (macros).",
    ),
    template: z.enum(MACRO_TEMPLATES),
    version: z.int().min(1).max(16),
    /** String options of the template (e.g. mode, shape, profile). Numbers belong in parameters. */
    params: z
      .record(z.string().regex(/^[a-z][a-z0-9_]{0,63}$/), z.string().min(1).max(64))
      .optional(),
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
  /** Own construction frame in the local_frame: build in its XY, extrude along its +Z. */
  placement: FeaturePlacement.optional().describe(
    "Own construction frame {origin, x_axis, z_axis} (orthogonal) inside local_frame: the feature is built in its XY plane/+Z direction, sees its inputs in that frame and is placed back. Replaces transform chains. B-Rep features only.",
  ),
  /** Named plane preset for the construction frame (alternative to placement). */
  plane: PlanePreset.optional().describe(
    "Plane preset instead of placement: XY; XZ = front (x->X, y->Z, normal -Y); YZ = side (x->Y, y->Z, normal +X); back (x->-X, y->Z, normal +Y). A profile/sketch on a plane is extruded along its normal.",
  ),
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
  objectives: z
    .array(
      z.strictObject({
        id: Id,
        expression: ExpressionSchema,
        weight: DecimalString.default("1"),
        scale: DecimalString.default("1"),
        loss: z.enum(["none", "huber", "cauchy"]).default("none"),
      }),
    )
    .max(32)
    .default([]),
  regularization: DecimalString.default("1"),
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
  z.strictObject({
    id: Id,
    kind: z.literal("surface_deviation"),
    feature_id: Id,
    maximum: Quantity,
  }),
  z.strictObject({
    id: Id,
    kind: z.literal("change_region"),
    feature_id: Id,
    local_frame: Id.optional(),
    center: Point,
    radius: DecimalString,
    compute_margin: DecimalString.optional(),
  }),
  /** Mating region of a field with smooth unions: every blend must stay inactive inside the box. */
  z.strictObject({
    id: Id,
    kind: z.literal("blend_free_region"),
    feature_id: Id,
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
export const Assembly = z.strictObject({
  ...SemanticEntity,
  parent_assembly: Id.optional(),
  local_frame: Id.default("world"),
});
export const Part = z.strictObject({
  ...SemanticEntity,
  assembly: Id.optional(),
  local_frame: Id.default("world"),
  authoritative_representation: z.enum(["brep", "implicit", "mesh"]),
  outputs: z.array(Id).max(128),
});
export const ModelStructure = z.strictObject({
  project: z.strictObject(SemanticEntity),
  frames: z.array(Frame).max(128),
  assemblies: z.array(Assembly).max(64),
  parts: z.array(Part).max(128),
});
/** Tolerance presets: precision_cad 0.001 mm (the default), visual_reconstruction 0.01 mm. */
export const TOLERANCE_PRESETS = {
  precision_cad: "0.001",
  visual_reconstruction: "0.01",
} as const;
export const ModelSettings = z.strictObject({
  tolerance_mm: DecimalString,
  profile_preset: z.enum(["precision_cad", "visual_reconstruction"]).optional(),
});
/** Explicit model settings as requested: a preset, an explicit tolerance, or both (must agree). */
export const ModelSettingsInput = z.preprocess(
  (v) => decimalize(v),
  z
    .strictObject({
      tolerance_mm: DecimalString.optional(),
      profile_preset: z.enum(["precision_cad", "visual_reconstruction"]).optional(),
    })
    .describe(
      "Model tolerance, explicit opt-in: profile_preset visual_reconstruction = 0.01 mm (photo-based reconstructions), precision_cad = 0.001 mm (the default without settings); or tolerance_mm (0.00001..0.1). Recorded in the IR and every export manifest. Changing it rebuilds every feature.",
    ),
);
export const Configuration = z.strictObject({
  id: Id,
  name: z.string().min(1).max(200),
  /** Features shown/exported in this configuration (registered part outputs). */
  outputs: z.array(Id).min(1).max(512),
  /** The configuration a viewer selects first; exports without a configuration still use all model outputs. */
  default: z.boolean().optional(),
});
const Sha256 = z.string().regex(/^[a-f0-9]{64}$/);
export const Decal = z.strictObject({
  id: Id,
  /** Image or SVG: an uploaded artifact or a file in the allowed asset folders (sha256 pins it). */
  source: z.union([
    z.strictObject({ artifact_id: Id }),
    z.strictObject({ path: z.string().min(1).max(1024), sha256: Sha256.optional() }),
  ]),
  /** Centre of the decal on the surface of feature_id, and the outward surface normal there (world mm). */
  anchor: z.strictObject({ feature_id: Id, point_mm: Point, normal: Point }),
  size_mm: z.tuple([DecimalString, DecimalString]),
  rotation_deg: DecimalString.optional(),
  colour_hex: z
    .string()
    .regex(/^#[0-9a-fA-F]{6}$/)
    .optional(),
});
/** Lineage of an instantiate_fragment copy. */
export const FragmentRecord = z.strictObject({
  id_prefix: z.string().regex(/^[a-zA-Z0-9][a-zA-Z0-9_-]{0,47}$/),
  source_model: Id,
  source_revision: Id,
  source_ir_hash: Sha256,
  root_ids: z.array(Id).max(64),
  feature_count: z.int().min(0).max(4096),
  created_by_operation: Id,
});
export const ModelIR = z.strictObject({
  schema_version: z.literal("1"),
  unit: z.literal("mm"),
  structure: ModelStructure.optional(),
  features: z.array(Feature).max(4096),
  outputs: z.array(Id).max(512),
  constraints: z.array(Constraint).max(1024).default([]),
  assumptions: z.array(z.string().max(1000)).max(64).default([]),
  tolerance: Quantity.default({ value: "0.001", unit: "mm" }),
  profile: z
    .enum([
      "precision_cad",
      "render_surface",
      "watertight_solid",
      "manufacturing_candidate",
    ])
    .default("precision_cad"),
  /** Explicit process rules for manufacturing_candidate; sampled checks only, never a certification. */
  manufacturing: z
    .strictObject({
      process: z.enum(["fdm", "sla", "cnc_3axis", "sheet_metal", "generic"]),
      minimum_wall: Quantity,
      minimum_hole_diameter: Quantity.optional(),
      maximum_overhang: Quantity.optional(),
      build_direction: Point.default(["0", "0", "1"]),
    })
    .optional(),
  /** Explicit tolerance preset; when present tolerance equals settings.tolerance_mm. */
  settings: ModelSettings.optional(),
  /** Model variables, referenced by expressions {var: name} in any feature parameter. */
  variables: z.record(VariableName, Quantity).optional(),
  /** Output sets (variants, states, accessories); exports and previews may select one. */
  configurations: z.array(Configuration).max(32).optional(),
  /** Visual-only prints (viewer and GLB extras); never geometry, never in STEP. */
  decals: z.array(Decal).max(256).optional(),
  /** Lineage of features copied from other models with instantiate_fragment. */
  fragments: z.array(FragmentRecord).max(64).optional(),
});
export type ModelIR = z.infer<typeof ModelIR>;
export const SetParameter = z.strictObject({
  op: z.literal("set_parameter"),
  feature_id: Id,
  parameter: z.string().regex(/^[a-z][a-z0-9_]{0,63}$/),
  expected: Quantity,
  value: Quantity,
});
const ExpectedHash = z.string().regex(/^[a-f0-9]{64}$/);
/** One parameter in compact form: a Quantity, a plain number/decimal string (unit from the
 *  parameter's dimension: mm, deg or 1), {value, unit?} or an expression ({var}, {fn,...}). */
export const CompactParameter = z.union([
  Quantity,
  z.strictObject({ value: DecimalString, unit: QuantityUnit.optional() }),
  DecimalString,
  ExpressionSchema,
]);
/** add_feature input: the canonical Feature, or the compact form normalised server-side. */
export const CompactFeature = z.preprocess(
  (value: any) =>
    value && typeof value === "object" && !Array.isArray(value)
      ? {
          ...value,
          ...(value.parameters !== undefined
            ? { parameters: decimalize(value.parameters) }
            : {}),
          ...(value.expressions !== undefined
            ? { expressions: decimalize(value.expressions) }
            : {}),
          ...(value.construction !== undefined
            ? { construction: decimalize(value.construction, true) }
            : {}),
          ...(value.placement !== undefined
            ? { placement: decimalize(value.placement) }
            : {}),
        }
      : value,
  Feature.extend({
    kind: z.string().max(64).optional(),
    parameters: z.record(z.string().regex(/^[a-z][a-z0-9_]{0,63}$/), CompactParameter).default({}),
  }).describe(
    "Feature to add. Compact form accepted: kind defaults to the operator, local_frame to world, authoritative_representation to brep, owner_part to part-main; plain JSON numbers are accepted wherever a decimal string is expected; a parameter may be a plain number (unit mm for lengths, deg for angles, 1 for scalars), {value, unit?} or an expression such as {var: name}. The stored IR is always canonical.",
  ),
);
const PartInput = Part.extend({
  authoritative_representation: z.enum(["brep", "implicit", "mesh"]).default("brep"),
  outputs: z.array(Id).max(128).default([]),
});
export const PatchOperation = z.discriminatedUnion("op", [
  z.strictObject({
    op: z.literal("set_feature_dependencies"),
    feature_id: Id,
    expected_hash: z.string().regex(/^[a-f0-9]{64}$/),
    depends_on: z.array(Id).max(512),
  }),
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
  z.strictObject({ op: z.literal("add_feature"), feature: CompactFeature }),
  z.strictObject({
    op: z.literal("remove_feature"),
    feature_id: Id,
    expected_hash: z.string().regex(/^[a-f0-9]{64}$/),
  }),
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
  z
    .strictObject({
      op: z.literal("set_model_settings"),
      /** settings_hash from cad_get_model (model_level). */
      expected_hash: ExpectedHash,
      settings: ModelSettingsInput,
    })
    .describe("Change the model tolerance explicitly (preset visual_reconstruction = 0.01 mm). Every feature rebuilds."),
  z
    .strictObject({
      op: z.literal("set_variable"),
      name: VariableName,
      /** Current value; omit or null when the variable is new. */
      expected: Quantity.nullable().optional(),
      /** New value; a plain number keeps the current unit; null removes an unused variable. */
      value: z.union([Quantity, DecimalString, z.number()]).nullable(),
    })
    .describe("Create, change or remove a model variable; every feature whose expressions use {var: name} is re-evaluated and rebuilt (dependency invalidation)."),
  z
    .strictObject({
      op: z.literal("prune_unreachable"),
      /** Features to keep although unreachable (intentional construction references). */
      keep: z.array(Id).max(256).optional(),
      /** Optional exact list of the features expected to be removed (fails when it differs). */
      expected_removed: z.array(Id).max(4096).optional(),
    })
    .describe("Remove every feature that no output, part output, configuration, constraint or decal reaches (commit warnings list them)."),
  z
    .strictObject({ op: z.literal("add_part"), part: PartInput })
    .describe("Add one structure part (delta instead of set_structure; fails if the id exists). An implicit structure is made explicit first."),
  z
    .strictObject({
      op: z.literal("add_frame"),
      frame: z.preprocess((v) => decimalize(v), Frame),
    })
    .describe("Add one structure frame (delta instead of set_structure; fails if the id exists)."),
  z
    .strictObject({ op: z.literal("add_assembly"), assembly: Assembly })
    .describe("Add one structure assembly (delta instead of set_structure; fails if the id exists)."),
  z
    .strictObject({
      op: z.literal("set_part_outputs"),
      part_id: Id,
      /** hash of the part's current outputs array (cad_structure definition.outputs). */
      expected_hash: ExpectedHash,
      outputs: z.array(Id).max(128),
    })
    .describe("Replace one part's outputs, checked against the hash of that part's current outputs only (parallel authors of other parts do not conflict)."),
  z
    .strictObject({
      op: z.literal("instantiate_fragment"),
      source_model: Id,
      /** Exact immutable source revision. */
      source_revision: Id,
      /** Feature ids or part ids (a part = its outputs) whose dependency closure is copied. */
      root_ids: z.array(Id).min(1).max(64),
      /** Prefix of every copied id (features, parts, frames); variables get a namespaced name. */
      id_prefix: z.string().regex(/^[a-zA-Z0-9][a-zA-Z0-9_-]{0,47}$/),
      /** Target frame the fragment's world is placed in (default world). */
      frame: Id.optional(),
      /** Register copied source outputs as outputs here (default true). */
      outputs: z.boolean().optional(),
    })
    .describe("Copy features from another model's exact revision (dependency closure, their parts and frames) with prefixed ids and a lineage record. Needs read access to the source (owner, or a grant that may export); artifacts are never shared silently."),
  z
    .strictObject({
      op: z.literal("set_configurations"),
      /** hash of the current configurations array ([] when none), cad_get_model model_level. */
      expected_hash: ExpectedHash,
      configurations: z.array(Configuration).max(32),
    })
    .describe("Replace the model configurations (variants/states as output sets). cad_render/cad_export take configuration to select one; without it all model outputs are used."),
  z
    .strictObject({
      op: z.literal("set_decals"),
      /** hash of the current decals array ([] when none), cad_get_model model_level. */
      expected_hash: ExpectedHash,
      decals: z.preprocess((v) => decimalize(v), z.array(Decal).max(256)),
    })
    .describe("Replace the visual-only decals (viewer and GLB extras, never STEP geometry). Prints that must exist as geometry use text/svg_emblem relief."),
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
  repair: z
    .strictObject({
      of_transaction: Id,
      cause: z.string().min(1).max(200),
    })
    .optional(),
});
export type Patch = z.infer<typeof Patch>;
/** Server-side wait of an asynchronous tool call (turn economy: no polling turns). */
export const WaitMs = z.int().min(0).max(110000).optional();
/** brief: compact rows/omitted nulls for reading; full: every field of the contract. */
export const Detail = z.enum(["brief", "full"]).optional();
/** A patch document the caller already has on disk or uploaded: never re-typed as JSON. */
export const PatchRef = z
  .union([
    z.strictObject({ artifact_id: Id }),
    z.strictObject({ path: z.string().min(1).max(1024) }),
  ])
  .describe(
    "Instead of operations: an uploaded artifact or an absolute *.json path inside LLCAD_PATCH_PATHS (default /root/projects, <data>/uploads, <data>/annotations) holding an operations array or {operations}.",
  );
/** cad_apply_patch: Patch plus the one-call transaction controls. */
export const ApplyPatch = Patch.extend({
  operations: z.array(PatchOperation).min(1).max(64).optional(),
  patch_ref: PatchRef.optional(),
  dry_run: z
    .boolean()
    .optional()
    .describe("Plan only (compile, dependencies, estimate) like cad_plan_edit; nothing is built."),
  validate: z
    .boolean()
    .optional()
    .describe("After a successful evaluate run the profile checks in the same call (~0.2 s)."),
  commit: z
    .enum(["never", "if_valid"])
    .optional()
    .describe("if_valid: validate and commit in the same call when every check passes (default never)."),
  gates: z
    .boolean()
    .optional()
    .describe(
      "With commit if_valid: run cad_gates (brief) on the committed revision and attach {floor_passed, summary, residuals} (a floor, never 'done').",
    ),
  wait_ms: WaitMs.describe(
    "Wait up to this many ms (<= 110000) and answer with the finished job; default 100000 with validate/commit.",
  ),
  detail: Detail,
});
export type ApplyPatch = z.infer<typeof ApplyPatch>;
export const ReadBinding = { model_id: Id, revision: Id.optional() };

export const ToolSchemas = {
  cad_feature_batch: BatchStart,
  cad_feature_batch_report: BatchReport,
  cad_reference_setup: ReferenceSetup,
  cad_reference_check: ReferenceStart,
  cad_reference_report: ReferenceReport,
  cad_reference_register: RegisterInput,
  cad_view: ViewInput,
  cad_gates: GatesInput,
  cad_drawing_analyze: DrawingAnalyzeInput,
  cad_drawing_render: DrawingRenderInput,
  cad_drawing_publish: DrawingPublishInput,
  cad_fit_parameters: FitParameters,
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
      .enum([
        "precision_cad",
        "render_surface",
        "watertight_solid",
        "manufacturing_candidate",
      ])
      .default("precision_cad"),
    /** Explicit tolerance preset, e.g. {profile_preset: "visual_reconstruction"} = 0.01 mm. */
    settings: ModelSettingsInput.optional(),
    idempotency_key: z.string().min(16).max(128),
  }),
  cad_get_model: z.strictObject({
    ...ReadBinding,
    offset: z.int().min(0).default(0),
    limit: z.int().min(1).max(64).default(32),
    detail: Detail.describe(
      "brief: compact feature rows; full (default).",
    ),
  }),
  cad_structure: z.strictObject({
    ...ReadBinding,
    kind: z.enum(["project", "assembly", "part", "frame"]).default("project"),
    entity_id: Id.optional(),
    query: z.string().max(200).default(""),
    offset: z.int().min(0).default(0),
    limit: z.int().min(1).max(16).default(8),
    detail: Detail.describe(
      "brief: compact entries; full (default).",
    ),
  }),
  /** One brief table for a local edit: features by id or by world region, with everything
   *  set_parameter/set_construction/remove_feature/set_part_outputs expect. */
  cad_context: z.strictObject({
    ...ReadBinding,
    feature_ids: z.array(Id).min(1).max(64).optional(),
    region_mm: z
      .strictObject({ min: Point, max: Point })
      .optional()
      .describe("World box: features whose evaluated bounds overlap it (candidate filter, not containment)."),
    depth: z
      .int()
      .min(0)
      .max(3)
      .default(1)
      .describe("Also list direct dependencies/dependents up to this graph distance (default 1)."),
    limit: z.int().min(1).max(64).default(48),
  }),
  cad_find: z.strictObject({
    ...ReadBinding,
    query: z.string().max(200).default(""),
    kind: z.string().max(64).optional(),
    owner_part: Id.optional(),
    point: Point.optional(),
    box: z.strictObject({ min: Point, max: Point }).optional(),
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
    sections: z
      .array(
        z.enum([
          "faces",
          "constraints",
          "facts",
          "contracts",
          "quality",
          "adjacency",
        ]),
      )
      .max(6)
      .optional(),
    anchor: z
      .strictObject({
        point: Point,
        normal: Point.optional(),
        barycentric: z
          .tuple([DecimalString, DecimalString, DecimalString])
          .optional(),
        triangle_index: z.int().min(0).max(10000000).optional(),
        view_direction: Point.optional(),
      })
      .optional(),
    detail: Detail.describe(
      "brief: parameters, hashes, facts summary, dependents (faces only with sections [faces]); full (default).",
    ),
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
        "surface_deviation",
        "thread_fit",
        "fit_primitives",
        "surface_distance",
        "wall_thickness",
        "blend_activity",
      ])
      .default("all"),
    other_feature_id: Id.optional(),
    other_revision: Id.optional(),
    maximum_deviation: Quantity.optional(),
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
    point: Point.optional(),
    step: Quantity.optional(),
    /** clearance only: sample the clearance along a linear motion of the other feature. */
    motion: z
      .strictObject({
        translation: Point,
        steps: z.int().min(1).max(64).default(8),
      })
      .optional(),
    /** blend_activity only: local-frame box in which every smooth union must stay inactive. */
    region: z.strictObject({ min: Point, max: Point }).optional(),
    /** Contact graph for listed pairs in ONE job: each name is a B-Rep feature id or a structure part id (= its outputs). */
    pairs: z
      .array(z.tuple([Id, Id]))
      .min(1)
      .max(256)
      .optional()
      .describe(
        "Contact graph of these pairs (feature or part ids) in one job: per pair distance_mm, contact (<= 0.05 mm), coincident_faces; floating_parts; coincident faces. Needs idempotency_key.",
      ),
    /** Contact graph over all parts (structure parts, else outputs) or the listed ones: AABB prefilter, exact distance, coincident faces. */
    all_pairs: z
      .strictObject({
        parts: z.array(Id).min(2).max(128).optional(),
        tolerance_mm: z.number().positive().max(10).optional(),
      })
      .optional()
      .describe(
        "Contact graph over all parts (or the listed ones) in one job: exact distances, contact <= tolerance_mm (0.05), floating_parts, coincident faces of different parts (z-fighting). Needs idempotency_key.",
      ),
    idempotency_key: z.string().min(16).max(128).optional(),
    wait_ms: WaitMs,
  }),
  cad_plan_edit: Patch,
  cad_solve_constraints: z.strictObject({
    ...WriteBinding,
    problem: SolverProblem,
    wait_ms: WaitMs,
  }),
  cad_apply_patch: ApplyPatch,
  cad_validate: z.strictObject({
    ...WriteBinding,
    transaction_id: Id,
    wait_ms: WaitMs,
  }),
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
  cad_revert: z.strictObject({
    ...WriteBinding,
    target_revision: Id,
    wait_ms: WaitMs,
  }),
  cad_rebuild: z.strictObject({
    ...WriteBinding,
    target_registry_hash: z.string().regex(/^[a-f0-9]{64}$/),
    mode: z.enum(["plan", "candidate"]).default("plan"),
    wait_ms: WaitMs,
  }),
  cad_render: z.strictObject({
    ...ReadBinding,
    idempotency_key: z.string().min(16).max(128),
    feature_id: Id.optional(),
    deflection: Quantity.default({ value: "0.05", unit: "mm" }),
    adaptive: z
      .strictObject({
        target_error: Quantity,
        feature_factor: DecimalString.default("0.5"),
        max_deflection: Quantity.optional(),
      })
      .optional(),
    region: z.strictObject({ center: Point, radius: DecimalString }).optional(),
    /** Render only this configuration's outputs (default: all model outputs). */
    configuration: Id.optional(),
    view: z
      .strictObject({
        kind: z.enum(["section", "orthographic"]),
        origin: Point.optional(),
        normal: Point.optional(),
        direction: Point.optional(),
        hidden_lines: z.boolean().optional(),
        discretization: Quantity.optional(),
      })
      .optional(),
    wait_ms: WaitMs,
  }),
  cad_import: z.strictObject({
    ...WriteBinding,
    artifact_id: Id,
    format: z.enum(["ir", "step", "stl", "vdb"]),
    validation_profile: z
      .enum(["render_surface", "watertight_solid"])
      .optional(),
    source_unit: z.enum(["mm", "m", "um"]),
    /** STEP only: preserve the product structure as frames, assemblies and parts via a probe job. */
    structure: z.enum(["flatten", "preserve"]).default("flatten"),
    grid: z.string().min(1).max(200).optional(),
    /** IR only: replace = import into a non-empty model as a diff candidate (protected features and all current constraints are kept). Default: the model must be empty. */
    mode: z.enum(["empty", "replace"]).optional(),
    wait_ms: WaitMs,
  }),
  cad_export: z.strictObject({
    reference_policy: z.enum(["require_pass", "draft"]).default("require_pass"),
    model_id: Id,
    revision: Id,
    format: z.enum(["ir", "step", "stl", "glb", "brep", "vdb"]),
    deflection: Quantity.default({ value: "0.01", unit: "mm" }),
    /** Geometry formats: export only this configuration's outputs (default: all model outputs). */
    configuration: Id.optional(),
    idempotency_key: z.string().min(16).max(128),
    wait_ms: WaitMs,
  }),
  cad_job_get: z.strictObject({
    job_id: Id,
    wait_ms: WaitMs.describe(
      "Wait up to this many ms (<= 110000, default 10000) for the job to finish; progress notifications every ~5 s.",
    ),
    detail: Detail.describe(
      "brief (default) or full job view.",
    ),
  }),
  cad_job_cancel: z.strictObject({
    job_id: Id,
    idempotency_key: z.string().min(16).max(128),
  }),
  cad_viewer_open: z.strictObject({
    launch_browser: z.boolean().default(true),
    model_id: Id.optional(),
  }),
  cad_viewer_close: z.strictObject({}),
  cad_model_admin: z.strictObject({
    action: z.enum(["rename", "fork"]),
    model_id: Id,
    /** rename: the new name (required); fork: name of the new model (default "<name> (Fork)"). */
    name: z.string().min(1).max(200).optional(),
    /** fork: source revision (default head). */
    revision: Id.optional(),
    /** fork: purpose of the new model (default: the source purpose). */
    purpose: z.string().max(1000).optional(),
    idempotency_key: z.string().min(16).max(128),
  }),
};
export type ToolName = keyof typeof ToolSchemas;
/** MCP requires an explicit object root even for unions of object modes. The model-facing
 *  schema is generated for size (input mode, shared $defs, numbers for decimals); the
 *  server validates every call against the strict zod schema regardless. cad_plan_edit
 *  advertises its operations by reference to cad_apply_patch instead of a second copy
 *  of the whole patch grammar. */
export function inputJSONSchema(tool: ToolName) {
  if (tool === "cad_plan_edit") {
    const json = toolInputJSONSchema(Patch.omit({ operations: true }));
    json.properties.operations = {
      type: "array",
      minItems: 1,
      maxItems: 64,
      description:
        "Exactly the operations of cad_apply_patch (same grammar, validated identically). Prefer cad_apply_patch {dry_run: true}.",
      items: {
        type: "object",
        properties: {
          op: {
            type: "string",
            enum: PatchOperation.options.map(
              (o: any) => o.shape.op.value as string,
            ),
          },
        },
        required: ["op"],
      },
    };
    json.required = [...(json.required ?? []), "operations"];
    return { ...json, type: "object" as const };
  }
  return { ...toolInputJSONSchema(ToolSchemas[tool]), type: "object" as const };
}
export const READ_TOOLS = new Set<ToolName>([
  "cad_feature_batch_report",
  "cad_reference_report",
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
  "cad_context",
  "cad_view",
  "cad_gates",
  "cad_drawing_analyze",
  "cad_drawing_render",
]);
