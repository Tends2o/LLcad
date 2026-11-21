import { ModelIR, Feature, Patch, Quantity } from "../semantic-ir/schema.js";
import { parse, quantity, equalQuantity } from "../semantic-ir/units.js";
import { hash } from "../semantic-ir/hash.js";
import { requireThat } from "../semantic-ir/errors.js";
import { BUILD_HASH } from "./build.js";
import { evaluate, resolveExpressions } from "./expressions.js";
import { expandMacro, MACROS } from "./macros.js";
import {
  MODEL_OPERATIONS,
  applyModelOperation,
  checkModelLevel,
  normaliseFeature,
  replacementCandidate,
  type DimensionOf,
  type FragmentSource,
} from "./model-ops.js";
import { patchContinuity } from "./patches.js";
import { equationValues, checkEquation } from "./constraints.js";
import { validateBasis, validateSurface, refineSurface } from "./nurbs.js";
import * as Rational from "./bernstein.js";
import { affineContract } from "./affine.js";
import { compileStructure, contextHash } from "./structure.js";
import { isoBasicProfile } from "./threads.js";
import { resolveFont, readSvgAsset } from "./fonts.js";
type Param = {
  dimension: "length" | "angle" | "scalar";
  min?: number;
  max?: number;
  optional?: boolean;
  integer?: boolean;
};
const length: Param = { dimension: "length", min: 0.00001, max: 100000 };
const coord: Param = {
  dimension: "length",
  min: -1000000,
  max: 1000000,
  optional: true,
};
const angle: Param = {
  dimension: "angle",
  min: -Math.PI * 2,
  max: Math.PI * 2,
};
const position = { x: coord, y: coord, z: coord };
const scalar: Param = { dimension: "scalar", min: 0.000001, max: 10000 };
/** Relief depth: + embosses (adds material), - debosses (cuts). |depth| >= 0.05 mm. */
const relief: Param = { dimension: "length", min: -100, max: 100 };
const overlap: Param = { dimension: "length", min: 0, max: 10, optional: true };
const one = (
  params: Record<string, Param>,
  refs: [number, number],
  output = "brep",
  inputs: "brep" | "mesh" | "implicit" = "brep",
) => ({
  version: 1,
  params,
  refs,
  output,
  inputs,
  required_validators: ["structure", "finite", "geometry", "constraints"],
  engine:
    output === "mesh" && inputs !== "brep"
      ? "mathforge-mesh-1/CGAL-6.0.1"
      : "OCCT-7.9.3.1.1",
});
/** Machine-readable per-operator contract metadata; merged into the registry. */
const LOCAL_EFFECT = new Set([
  "draft",
  "hole",
  "pocket",
  "groove",
  "fillet",
  "chamfer",
  "shell",
  "thread",
  "offset_solid",
  "remesh_region",
  "local_mesh_deform",
  "mesh_repair",
  "trim_surface",
]);
const ANALYTIC_DERIVATIVES = new Set([
  "box",
  "sphere",
  "cylinder",
  "cone",
  "torus",
  "strip",
  "extrude",
  "revolve",
  "point",
  "circle",
  "plane",
]);
export function operatorContract(
  name: string,
  contract: { output: string; inputs: string; refs: [number, number] },
) {
  const mesh = contract.output === "mesh";
  return {
    effect_region: LOCAL_EFFECT.has(name)
      ? "local_to_declared_parameters_within_input_geometry"
      : contract.refs[1] === 0
        ? "entire_new_feature"
        : "entire_dependent_feature",
    derivatives: ANALYTIC_DERIVATIVES.has(name)
      ? "registered_analytic_dimension_derivatives"
      : name === "field"
        ? "interval_forward_mode_and_finite_differences"
        : "expression_finite_difference_only",
    invalidation: "feature_hash_change_invalidates_all_transitive_dependents",
    preconditions:
      "typed_parameter_ranges_dependency_arity_and_representation_match",
    resource_model: {
      time_budget_seconds: LIMITS.job_seconds,
      memory_mib: 1536,
      output_bytes: LIMITS.max_artifact_bytes,
      triangles: LIMITS.triangles,
      active_cells: LIMITS.active_cells,
    },
    determinism: mesh
      ? "deterministic_given_registry_hash_native_mesh_source_and_indexed_input"
      : "deterministic_given_registry_hash_and_kernel_version",
    cancellation:
      "process_group_kill_with_lease_fencing_and_untouched_base_revision",
    error_codes: [
      "INVALID_SCHEMA",
      "UNIT_MISMATCH",
      "GEOMETRY_INVALID",
      "PRECISION_UNSUPPORTED",
      "BUDGET_EXCEEDED",
      "OUT_OF_SCOPE",
      "KERNEL_FAILURE",
      "CANCELLED",
    ],
    tests: mesh
      ? [
          "tests/geometry/mesh-operators.test.ts",
          "tests/geometry/test_mesh_ops.py",
        ]
      : [
          "tests/geometry/test_native.py",
          "tests/geometry/native-operators.test.ts",
          "tests/regression/corpus.test.ts",
        ],
    authorization: "model:edit_scope_of_the_candidate_transaction",
    quality_after_evaluation: "preview_only_until_validated_within_profile",
  };
}
export const OPERATORS = {
  point: one(position, [0, 0]),
  line: one({}, [0, 0]),
  arc: one({}, [0, 0]),
  plane: one({ width: length, height: length, ...position }, [0, 0]),
  trim_surface: one(
    {
      u_min: { dimension: "scalar", min: 0, max: 1 },
      u_max: { dimension: "scalar", min: 0, max: 1 },
      v_min: { dimension: "scalar", min: 0, max: 1 },
      v_max: { dimension: "scalar", min: 0, max: 1 },
    },
    [1, 1],
  ),
  cap: one({}, [1, 16]),
  sew: one(
    { tolerance: { dimension: "length", min: 1e-7, max: 0.001 } },
    [2, 128],
  ),
  regularize: one({}, [1, 1]),
  thread: one(
    {
      root_radius: { ...length, optional: true },
      pitch: { ...length, optional: true },
      height: length,
      tooth_depth: { ...length, optional: true },
      tooth_width: { ...length, optional: true },
      crest_width: { ...length, min: 0, optional: true },
      runout: { ...length, min: 0, optional: true },
    },
    [0, 1],
  ),
  offset_solid: one(
    { distance: { dimension: "length", min: -1000, max: 1000 } },
    [1, 1],
  ),
  box: one(
    { width: length, depth: length, height: length, ...position },
    [0, 0],
  ),
  sphere: one({ radius: length, ...position }, [0, 0]),
  cylinder: one({ radius: length, height: length, ...position }, [0, 0]),
  cone: one(
    {
      radius: length,
      top_radius: { ...length, min: 0 },
      height: length,
      ...position,
    },
    [0, 0],
  ),
  torus: one(
    { major_radius: length, minor_radius: length, ...position },
    [0, 0],
  ),
  profile: one({}, [0, 0]),
  sketch: one({}, [0, 0]),
  text: one(
    {
      cap_height: { dimension: "length", min: 0.1, max: 1000 },
      depth: relief,
      tracking: { dimension: "length", min: -100, max: 100, optional: true },
      line_spacing: { ...length, optional: true },
      overlap,
    },
    [0, 1],
  ),
  svg_emblem: one({ depth: relief, overlap }, [0, 1]),
  strip: one({ width: length, height: length }, [0, 0]),
  circle: one({ radius: length, ...position }, [0, 0]),
  bezier: one({}, [0, 0]),
  bspline: one({}, [0, 0]),
  nurbs_curve: one({}, [0, 0]),
  nurbs_surface: one({}, [0, 0]),
  extrude: one({ height: length }, [1, 1]),
  revolve: one({ angle }, [1, 1]),
  loft: one({}, [2, 16]),
  sweep: one(
    {
      twist: { ...angle, optional: true },
      scale_end: { dimension: "scalar", min: 0.05, max: 20, optional: true },
      sections: {
        dimension: "scalar",
        min: 2,
        max: 256,
        integer: true,
        optional: true,
      },
    },
    [2, 3],
  ),
  union: one({}, [2, 16]),
  intersection: one({}, [2, 16]),
  difference: one({}, [2, 16]),
  hole: one({ radius: length, depth: length, ...position }, [1, 1]),
  pocket: one(
    { width: length, length: length, depth: length, ...position },
    [1, 1],
  ),
  groove: one(
    { radius: length, width: length, depth: length, ...position },
    [1, 1],
  ),
  fillet: one({ radius: length, radius_end: { ...length, optional: true } }, [1, 1]),
  chamfer: one({ distance: length }, [1, 1]),
  shell: one({ thickness: length }, [1, 1]),
  draft: one(
    { angle: { dimension: "angle", min: -(80 * Math.PI) / 180, max: (80 * Math.PI) / 180 } },
    [1, 1],
  ),
  split_by_plane: one({}, [1, 1]),
  knurl: one(
    {
      radius: length,
      height: length,
      count: { dimension: "scalar", min: 3, max: 1000, integer: true },
      depth: length,
      width: { ...length, optional: true },
      inner_radius: { ...length, optional: true },
      ...position,
    },
    [0, 1],
  ),
  cut_pattern: one(
    {
      count: { dimension: "scalar", min: 1, max: 10000, integer: true },
      dx: coord,
      dy: coord,
      dz: coord,
      angle: { ...angle, optional: true },
    },
    [2, 2],
  ),
  transform: one(
    {
      ...position,
      angle: { ...angle, optional: true },
      scale: { ...scalar, optional: true },
    },
    [1, 1],
  ),
  affine_transform: one({}, [1, 1]),
  rotate: one({ angle }, [1, 1]),
  instance: one({ ...position, angle: { ...angle, optional: true } }, [1, 1]),
  mirror: one({}, [1, 1]),
  pattern: one(
    {
      count: { dimension: "scalar", min: 1, max: 10000, integer: true },
      dx: coord,
      dy: coord,
      dz: coord,
    },
    [1, 129],
  ),
  circular_pattern: one(
    {
      count: { dimension: "scalar", min: 1, max: 10000, integer: true },
      angle,
    },
    [1, 129],
  ),
  assembly: one({}, [1, 128]),
  field: one({}, [0, 0], "implicit"),
  imported: one({}, [0, 0], "source"),
  mesh_repair: one(
    {
      weld_tolerance: {
        dimension: "length",
        min: 1e-6,
        max: 1,
        optional: true,
      },
    },
    [1, 1],
    "mesh",
    "mesh",
  ),
  remesh_region: one(
    {
      radius: length,
      target_edge: length,
      iterations: {
        dimension: "scalar",
        min: 1,
        max: 20,
        integer: true,
        optional: true,
      },
      ...position,
    },
    [1, 1],
    "mesh",
    "mesh",
  ),
  local_mesh_deform: one(
    {
      radius: length,
      iterations: {
        dimension: "scalar",
        min: 1,
        max: 50,
        integer: true,
        optional: true,
      },
      ...position,
    },
    [1, 1],
    "mesh",
    "mesh",
  ),
  tessellate: one(
    { deflection: { dimension: "length", min: 1e-5, max: 10 } },
    [1, 1],
    "mesh",
    "brep",
  ),
  extract_isosurface: one({}, [1, 1], "mesh", "implicit"),
} as const;
export const LIMITS = {
  // How complex a model may get. The ceiling exists so that one model still
  // fits in one machine's memory, not to cap the design: a build that needs
  // more time than one job budget now continues in the next job instead of
  // failing, so complexity costs time, never success.
  features: 4096,
  ast_nodes: 4096,
  ast_depth: 32,
  instances: 10000,
  triangles: 500000,
  active_cells: 250000,
  job_seconds: 45,
  worker_memory_mb: 1536,
  request_bytes: 1048576,
  response_bytes: 32768,
  max_artifact_bytes: 33554432,
  max_queued_per_user: 16,
  patch_join_constraints: 8,
  patch_join_max_degree: 5,
  solver_variables: 12,
  vdb_samples: 125000,
  field_cached_samples: 160000,
};
export const REGISTRY_HASH = hash({ operators: OPERATORS, build: BUILD_HASH });
export function fieldValueUnit(
  node: any,
  depth = 0,
): "length" | "dimensionless" {
  requireThat(
    depth <= LIMITS.ast_depth,
    "BUDGET_EXCEEDED",
    "The field unit tree is too deep.",
  );
  if (node.op === "gyroid") return "dimensionless";
  if (node.op === "sampled_grid") return node.value_unit ?? "length";
  if (node.a) {
    const a = fieldValueUnit(node.a, depth + 1),
      b = fieldValueUnit(node.b, depth + 1);
    requireThat(
      a === b,
      "UNIT_MISMATCH",
      "CSG field values need equal units; use convert_field_unit explicitly first.",
    );
    return a;
  }
  if (node.source) {
    const unit = fieldValueUnit(node.source, depth + 1);
    if (["shell", "offset", "local_field_delta"].includes(node.op))
      requireThat(
        unit === "length",
        "UNIT_MISMATCH",
        "Shells, offsets and local field amplitudes need length-valued fields.",
      );
    if (node.op === "convert_field_unit") {
      requireThat(
        node.to !== unit,
        "UNIT_MISMATCH",
        "A field unit conversion needs a different target unit.",
      );
      return node.to;
    }
    return unit;
  }
  return "length";
}
export function checkDepth(input: unknown) {
  let count = 0;
  const stack: [unknown, number][] = [[input, 0]];
  while (stack.length) {
    const [value, depth] = stack.pop()!;
    requireThat(
      ++count <= 50000 && depth <= 48,
      "BUDGET_EXCEEDED",
      "The data structure is too large or too deep.",
    );
    if (value && typeof value === "object")
      for (const child of Object.values(value)) stack.push([child, depth + 1]);
  }
}
/** Every uploaded artifact a plan reads: imported features and sampled grids inside field expressions. */
export function referencedArtifacts(
  features: any[],
): { artifact_id: string; format: string }[] {
  const found = new Map<string, { artifact_id: string; format: string }>();
  const walk = (node: any, depth: number) => {
    if (!node || typeof node !== "object" || depth > LIMITS.ast_depth) return;
    if (node.op === "sampled_grid")
      found.set(node.artifact_id + ":vdb", {
        artifact_id: node.artifact_id,
        format: "vdb",
      });
    for (const key of ["a", "b", "source"])
      if (node[key]) walk(node[key], depth + 1);
  };
  for (const f of features) {
    if (f.construction.operator === "svg_emblem" && f.construction.artifact_id)
      found.set(f.construction.artifact_id + ":svg", {
        artifact_id: f.construction.artifact_id,
        format: "svg",
      });
    if (f.construction.operator === "imported")
      found.set(f.construction.artifact_id + ":" + f.construction.format, {
        artifact_id: f.construction.artifact_id,
        format: f.construction.format,
      });
    if (f.construction.operator === "field") walk(f.construction.expression, 0);
    // A certified edit compares against the previous expression, which may sample a grid too.
    if (f.reference_expression) walk(f.reference_expression, 0);
  }
  return [...found.values()];
}
export function fieldContract(
  node: any,
  depth = 0,
  state = { nodes: 0 },
): { semantics: string; lipschitz: number } {
  const result = fieldContractNode(node, depth, state);
  requireThat(
    Number.isFinite(result.lipschitz) &&
      result.lipschitz >= 1e-12 &&
      result.lipschitz <= 1e12,
    "BUDGET_EXCEEDED",
    "The field composition exceeds the numeric Lipschitz range [1e-12,1e12].",
  );
  return result;
}
function fieldContractNode(
  node: any,
  depth = 0,
  state = { nodes: 0 },
): { semantics: string; lipschitz: number } {
  if (depth === 0) fieldValueUnit(node);
  requireThat(
    depth <= LIMITS.ast_depth && ++state.nodes <= LIMITS.ast_nodes,
    "BUDGET_EXCEEDED",
    "The field AST exceeds the budget.",
  );
  const num = (v: string) => {
    const n = Number(v);
    requireThat(
      Number.isFinite(n) && Math.abs(n) <= 1e6,
      "INVALID_SCHEMA",
      "Invalid field parameter.",
    );
    return n;
  };
  const pos = (v: string) => {
    const n = num(v);
    requireThat(
      n >= 1e-5,
      "GEOMETRY_INVALID",
      "A positive field size is required.",
    );
    return n;
  };
  if (node.center) node.center.forEach(num);
  if (node.op === "sampled_grid") {
    // Claimed bound; the worker refuses the grid when its measured difference bound is larger.
    return {
      semantics: "sampled_implicit_trilinear",
      lipschitz: pos(node.lipschitz),
    };
  }
  if (node.op === "gyroid") {
    node.origin.forEach(num);
    num(node.threshold);
    return { semantics: "general_implicit", lipschitz: 22 / pos(node.period) };
  }
  if (node.op === "sphere") {
    pos(node.radius);
    return { semantics: "exact_sdf", lipschitz: 1 };
  }
  if (node.op === "box") {
    node.half_size.forEach(pos);
    return { semantics: "exact_sdf", lipschitz: 1 };
  }
  if (node.op === "plane") {
    requireThat(
      Math.hypot(...node.normal.map(num)) >= 1e-12,
      "GEOMETRY_INVALID",
      "The plane normal must not be zero.",
    );
    num(node.offset);
    return { semantics: "exact_sdf", lipschitz: 1 };
  }
  if (node.op === "cylinder") {
    pos(node.radius);
    pos(node.half_height);
    return { semantics: "exact_sdf", lipschitz: 1 };
  }
  if (node.op === "capsule") {
    node.start.forEach(num);
    node.end.forEach(num);
    pos(node.radius);
    return { semantics: "exact_sdf", lipschitz: 1 };
  }
  if (node.op === "torus") {
    requireThat(
      pos(node.major) > pos(node.minor),
      "GEOMETRY_INVALID",
      "The ring radius must be larger than the tube radius.",
    );
    return { semantics: "exact_sdf", lipschitz: 1 };
  }
  if (node.a) {
    const a = fieldContract(node.a, depth + 1, state),
      b = fieldContract(node.b, depth + 1, state);
    if (node.op === "smooth_union") pos(node.k);
    return {
      semantics: "general_implicit",
      lipschitz: Math.max(a.lipschitz, b.lipschitz),
    };
  }
  const a = fieldContract(node.source, depth + 1, state);
  if (node.op === "convert_field_unit") {
    const reference = quantity(node.reference_length, "length");
    requireThat(
      reference >= 1e-5 && reference <= 1e6,
      "GEOMETRY_INVALID",
      "The reference length for field values must be positive and within the supported range.",
    );
    return {
      semantics: "general_implicit",
      lipschitz:
        a.lipschitz * (node.to === "length" ? reference : 1 / reference),
    };
  }
  if (node.op === "rotate") {
    node.origin.forEach(num);
    requireThat(
      Math.hypot(...node.axis.map(num)) >= 1e-12 &&
        Math.abs(quantity(node.angle, "angle")) <= 2 * Math.PI,
      "GEOMETRY_INVALID",
      "A field rotation needs a valid axis and an angle between -360 and 360 degrees.",
    );
    return a;
  }
  if (node.op === "affine_transform") {
    affineContract(node.matrix, node.translation);
    return {
      semantics:
        a.semantics === "general_implicit"
          ? "general_implicit"
          : "bounded_distance_estimator",
      lipschitz: a.lipschitz,
    };
  }
  if (node.op === "transform") {
    node.translation.forEach(num);
    const s = node.scale.map(pos);
    return {
      semantics: s.every((x: number) => x === s[0])
        ? a.semantics
        : a.semantics === "general_implicit"
          ? "general_implicit"
          : "bounded_distance_estimator",
      lipschitz: a.lipschitz,
    };
  }
  if (node.op === "shell") {
    const thickness = pos(node.thickness);
    let minimum = thickness,
      derivative = 0;
    for (const term of node.variations ?? []) {
      term.center.forEach(num);
      const amplitude = num(term.amplitude),
        radius = pos(term.radius);
      minimum += Math.min(amplitude, 0);
      derivative += (Math.abs(amplitude) * 2.109375) / radius;
    }
    requireThat(
      minimum >= 1e-5,
      "GEOMETRY_INVALID",
      "A variable shell thickness must stay positive over the whole domain.",
    );
    return {
      semantics: "general_implicit",
      lipschitz: a.lipschitz + derivative / 2,
    };
  }
  if (node.op === "local_field_delta")
    return {
      semantics: "general_implicit",
      lipschitz:
        a.lipschitz +
        (Math.abs(num(node.amplitude)) * 2.109375) / pos(node.radius),
    };
  if (node.op === "local_deform") {
    const k =
      (2.109375 * Math.hypot(...node.displacement.map(num))) / pos(node.radius);
    requireThat(
      k < 0.75,
      "CONSTRAINT_CONFLICT",
      "The local deformation violates the proven invertibility bound.",
    );
    return { semantics: "general_implicit", lipschitz: a.lipschitz / (1 - k) };
  }
  num(node.distance);
  return { semantics: "general_implicit", lipschitz: a.lipschitz };
}
/** Construction axes of the plane presets (build123d convention): x axis and normal. */
const PLANE_AXES: Record<string, { x: number[]; z: number[] }> = {
  XY: { x: [1, 0, 0], z: [0, 0, 1] },
  XZ: { x: [1, 0, 0], z: [0, -1, 0] },
  YZ: { x: [0, 1, 0], z: [1, 0, 0] },
  front: { x: [1, 0, 0], z: [0, -1, 0] },
  side: { x: [0, 1, 0], z: [1, 0, 0] },
  back: { x: [-1, 0, 0], z: [0, 1, 0] },
};
export type OwnPlacement = { rotation: number[][]; translation: number[] };
const finite = (v: number[], limit = 1e6) =>
  v.every((x) => Number.isFinite(x) && Math.abs(x) <= limit);
const normalized = (v: number[], what: string) => {
  const n = Math.hypot(...v);
  requireThat(
    finite(v) && n >= 1e-12,
    "GEOMETRY_INVALID",
    `${what} must not be zero.`,
  );
  return v.map((x) => x / n);
};
/** The feature's own construction frame (placement or plane preset), null when none. */
export function ownPlacement(f: Feature): OwnPlacement | null {
  const constructionPlane =
    f.construction.operator === "sketch" ? f.construction.plane : undefined;
  requireThat(
    [f.placement, f.plane, constructionPlane].filter(Boolean).length <= 1,
    "INVALID_SCHEMA",
    "A feature has at most one construction plane: placement, plane or sketch.plane.",
  );
  const plane = f.plane ?? constructionPlane;
  let x: number[], z: number[], origin: number[];
  if (f.placement) {
    origin = f.placement.origin.map(Number);
    requireThat(
      finite(origin),
      "GEOMETRY_INVALID",
      "placement.origin is outside the coordinate range.",
    );
    x = normalized(f.placement.x_axis.map(Number), "placement.x_axis");
    z = normalized(f.placement.z_axis.map(Number), "placement.z_axis");
    requireThat(
      Math.abs(x[0] * z[0] + x[1] * z[1] + x[2] * z[2]) <= 1e-6,
      "GEOMETRY_INVALID",
      "placement.x_axis and placement.z_axis must be perpendicular to each other.",
    );
  } else if (plane) {
    if (plane === "XY") return null;
    ({ x, z } = PLANE_AXES[plane]);
    origin = [0, 0, 0];
  } else return null;
  // Exact orthonormal frame: z, then x without its z part, y = z cross x.
  const dot = x[0] * z[0] + x[1] * z[1] + x[2] * z[2];
  x = normalized(
    x.map((v, i) => v - dot * z[i]),
    "placement.x_axis",
  );
  const y = [
    z[1] * x[2] - z[2] * x[1],
    z[2] * x[0] - z[0] * x[2],
    z[0] * x[1] - z[1] * x[0],
  ];
  return {
    rotation: [0, 1, 2].map((i) => [x[i], y[i], z[i]]),
    translation: origin,
  };
}
const numbers = (values: string[], what: string, limit = 1e6) => {
  const v = values.map(Number);
  requireThat(finite(v, limit), "GEOMETRY_INVALID", `${what} out of range.`);
  return v;
};
function checkSketchLoop(loop: any, where: string) {
  requireThat(
    !!loop.preset !== !!loop.segments,
    "INVALID_SCHEMA",
    `${where}: give either preset or start + segments.`,
  );
  if (loop.segments) {
    requireThat(loop.start, "INVALID_SCHEMA", `${where}: segments need start.`);
    numbers(loop.start, `${where}.start`);
    for (const seg of loop.segments) {
      for (const key of ["to", "via", "center"])
        if (seg[key]) numbers(seg[key], `${where}.${seg.type}.${key}`);
      for (const p of seg.through ?? []) numbers(p, `${where}.spline.through`);
      if (seg.corner_radius !== undefined)
        requireThat(
          Number(seg.corner_radius) >= 0,
          "GEOMETRY_INVALID",
          "corner_radius must not be negative.",
        );
    }
    if (loop.corner_radius !== undefined)
      requireThat(
        Number(loop.corner_radius) >= 0,
        "GEOMETRY_INVALID",
        "corner_radius must not be negative.",
      );
  } else {
    const p = loop.preset;
    requireThat(
      loop.start === undefined && loop.corner_radius === undefined,
      "INVALID_SCHEMA",
      `${where}: a preset has no start and no corner_radius (the radii are part of the preset).`,
    );
    const positive = ["width", "height", "length", "rx", "ry", "radius", "a", "b", "exponent"]
      .filter((k) => p[k] !== undefined && !(p.kind === "rounded_rect" && k === "radius"));
    for (const k of positive)
      requireThat(
        Number(p[k]) > 0 && Number(p[k]) <= 1e6,
        "GEOMETRY_INVALID",
        `${where}.preset.${k} must be positive.`,
      );
    if (p.kind === "rounded_rect")
      requireThat(
        !(p.radius !== undefined && p.radii) &&
          [p.radius ?? "0", ...(p.radii ?? [])].every((r: string) => Number(r) >= 0),
        "GEOMETRY_INVALID",
        `${where}: rounded_rect takes radius OR radii (not negative).`,
      );
    if (p.center) numbers(p.center, `${where}.preset.center`);
    for (const pt of p.points ?? []) numbers(pt, `${where}.preset.points`);
    if (p.angle_deg !== undefined) numbers([p.angle_deg], `${where}.preset.angle_deg`, 36000);
  }
}
/** Operator-specific checks of the one-shot operators; returns compiled extras. */
function oneShotOperator(
  f: Feature,
  values: Record<string, number>,
  map: Map<string, Feature>,
) {
  const c: any = f.construction;
  const extras: Record<string, any> = {};
  const assets: Record<string, string> = {};
  const known = (id: string, what: string) =>
    requireThat(map.has(id), "INVALID_SCHEMA", `${what}: feature ${id} does not exist.`);
  const reliefDepth = () =>
    requireThat(
      Math.abs(values.depth) >= 0.05,
      "GEOMETRY_INVALID",
      "Relief depth: at least 0.05 mm (+ raised, - recessed). Thinner prints belong in a decal, not in geometry.",
    );
  switch (c.operator) {
    case "sketch":
      checkSketchLoop(c, "sketch");
      (c.holes ?? []).forEach((h: any, i: number) => checkSketchLoop(h, `sketch.holes[${i}]`));
      break;
    case "text": {
      requireThat(
        !/[\u0000-\u0009\u000b-\u001f\u007f]/.test(c.string) && c.string.trim().length > 0,
        "INVALID_SCHEMA",
        "Text must not be empty and must not contain control characters other than line breaks.",
      );
      const lines = c.string.split("\n");
      requireThat(lines.length <= 16, "BUDGET_EXCEEDED", "At most 16 text lines per feature.");
      if (c.arc) {
        requireThat(lines.length === 1, "INVALID_SCHEMA", "Arc text is single-line.");
        const [radius, angle] = numbers([c.arc.radius, c.arc.center_angle_deg], "text.arc", 1e6);
        requireThat(radius > 0 && Math.abs(angle) <= 36000, "GEOMETRY_INVALID", "text.arc: radius > 0 and a finite angle.");
      }
      reliefDepth();
      const font = resolveFont(c.font ?? "DejaVu Sans", c.style ?? "regular");
      extras.font = {
        family: font.family,
        style: font.style,
        files: [font.sandbox, font.host],
        sha256: font.sha256,
      };
      assets.font = font.sha256;
      break;
    }
    case "svg_emblem": {
      requireThat(
        !!c.artifact_id !== !!c.path,
        "INVALID_SCHEMA",
        "svg_emblem needs exactly one source: artifact_id or path.",
      );
      requireThat(
        !(c.width_mm !== undefined && c.height_mm !== undefined),
        "INVALID_SCHEMA",
        "svg_emblem scales by width_mm OR height_mm.",
      );
      for (const k of ["width_mm", "height_mm"])
        if (c[k] !== undefined)
          requireThat(
            Number(c[k]) > 0 && Number(c[k]) <= 100000,
            "GEOMETRY_INVALID",
            `${k} must be positive.`,
          );
      requireThat(!c.sha256 || c.path, "INVALID_SCHEMA", "sha256 pins only a path source.");
      reliefDepth();
      if (c.path) {
        const svg = readSvgAsset(c.path);
        requireThat(
          !c.sha256 || c.sha256 === svg.sha256,
          "STALE_REVISION",
          "The SVG file has changed since it was pinned (sha256).",
          { current_sha256: svg.sha256 },
        );
        extras.svg_text = svg.text;
        extras.svg_sha256 = svg.text_sha256;
        assets.svg = svg.sha256;
      }
      break;
    }
    case "extrude":
      if (c.axis) normalized(c.axis.map(Number), "extrude.axis");
      break;
    case "revolve":
      if (c.axis) normalized(c.axis.map(Number), "revolve.axis");
      if (c.origin) numbers(c.origin, "revolve.origin");
      break;
    case "mirror":
      if (c.plane) {
        normalized(c.plane.normal.map(Number), "mirror.plane.normal");
        if (c.plane.point) numbers(c.plane.point, "mirror.plane.point");
      }
      break;
    case "fillet":
    case "chamfer":
      if (typeof c.edge_selector === "object")
        c.edge_selector.between_features.forEach((id: string) => known(id, "between_features"));
      break;
    case "shell":
      if (typeof c.opening === "object" && "feature" in c.opening) known(c.opening.feature, "shell.opening");
      if (typeof c.opening === "object" && "normal" in c.opening) normalized(c.opening.normal.map(Number), "shell.opening.normal");
      break;
    case "draft":
      requireThat(Math.abs(values.angle) >= 1e-6, "GEOMETRY_INVALID", "The draft angle must not be zero.");
      if (c.pull_direction) normalized(c.pull_direction.map(Number), "draft.pull_direction");
      if (c.neutral_plane?.normal) normalized(c.neutral_plane.normal.map(Number), "draft.neutral_plane.normal");
      if (c.neutral_plane?.point) numbers(c.neutral_plane.point, "draft.neutral_plane.point");
      if (c.faces && typeof c.faces === "object" && "feature" in c.faces) known(c.faces.feature, "draft.faces");
      break;
    case "split_by_plane":
      normalized(c.plane.normal.map(Number), "split_by_plane.plane.normal");
      if (c.plane.point) numbers(c.plane.point, "split_by_plane.plane.point");
      break;
    case "knurl":
      requireThat(values.depth < values.radius, "GEOMETRY_INVALID", "The groove depth must be smaller than the radius.");
      requireThat(
        values.inner_radius === undefined ||
          (f.depends_on.length === 0 && values.inner_radius < values.radius - values.depth),
        "GEOMETRY_INVALID",
        "inner_radius applies only without a target body and must lie inside the groove bottom.",
      );
      requireThat(
        values.width === undefined || values.width < (2 * Math.PI * values.radius) / values.count,
        "GEOMETRY_INVALID",
        "The groove width fills the circumference; use fewer or narrower grooves.",
      );
      break;
    case "cut_pattern":
      if (c.axis) normalized(c.axis.map(Number), "cut_pattern.axis");
      if (c.origin) numbers(c.origin, "cut_pattern.origin");
      requireThat(
        values.count === 1 ||
          ((c.kind ?? "linear") === "circular"
            ? Math.abs(values.angle ?? 2 * Math.PI) >= 1e-12
            : [values.dx, values.dy, values.dz].some((v) => v !== undefined && Math.abs(v) >= 1e-12)),
        "GEOMETRY_INVALID",
        "A pattern cut with several copies needs an offset (dx/dy/dz) or an angle.",
      );
      break;
  }
  return { extras, assets };
}
export function compile(input: unknown) {
  checkDepth(input);
  const ir = parse<ModelIR>(ModelIR, input);
  const structure = compileStructure(ir);
  const variables = ir.variables ?? {};
  // Macro calls expand into primitive sub-features <id>--<n> (the last keeps the macro
  // id); the stored IR (plan.ir) keeps the call itself.
  const features: Feature[] = [];
  const macroExpansion: Record<string, string[]> = {};
  for (const f of ir.features) {
    if (f.construction.operator !== "macro") {
      features.push(f);
      continue;
    }
    const subs = expandMacro(f, ownPlacement(f), variables);
    macroExpansion[f.id] = subs.map((x) => x.id);
    features.push(...subs);
  }
  const map = new Map<string, Feature>();
  let instances = 0;
  const expanded = new Map<string, number>();
  const owned = new Map<string, OwnPlacement>();
  const compiled: any[] = [];
  const hashes: Record<string, string> = {};
  for (const f of features) {
    requireThat(
      !map.has(f.id),
      "INVALID_SCHEMA",
      "Duplicate feature id" + ((f as any).macro_call ? ` (macro sub-feature ${f.id})` : "") + ".",
    );
    map.set(f.id, f);
  }
  const visited = new Set<string>(),
    visiting = new Set<string>();
  function visit(fid: string) {
    if (visited.has(fid)) return;
    requireThat(
      !visiting.has(fid),
      "CONSTRAINT_CONFLICT",
      "Cyclic feature graph.",
    );
    const f = map.get(fid);
    requireThat(f, "INVALID_SCHEMA", "Unknown feature reference.");
    visiting.add(fid);
    f.depends_on.forEach(visit);
    const name = f.construction.operator as keyof typeof OPERATORS,
      contract = OPERATORS[name];
    requireThat(contract, "INVALID_SCHEMA", `Operator ${name} is not registered.`);
    requireThat(
      f.depends_on.length >= contract.refs[0] &&
        f.depends_on.length <= contract.refs[1],
      "INVALID_SCHEMA",
      "Wrong number of operator inputs.",
    );
    requireThat(
      new Set(f.depends_on).size === f.depends_on.length ||
        ["union", "intersection", "difference"].includes(name),
      "INVALID_SCHEMA",
      "Duplicate dependency.",
    );
    requireThat(
      contract.output === "source" ||
        f.authoritative_representation === contract.output,
      "OUT_OF_SCOPE",
      "The representation does not match the operator.",
    );
    if (f.construction.operator === "imported") {
      requireThat(
        f.authoritative_representation ===
          (f.construction.format === "stl"
            ? "mesh"
            : f.construction.format === "vdb"
              ? "implicit"
              : "brep"),
        "OUT_OF_SCOPE",
        "The import format and the geometric authority must match.",
      );
      requireThat(
        (f.construction.component === undefined ||
          f.construction.format === "step") &&
          (f.construction.grid === undefined ||
            f.construction.format === "vdb"),
        "INVALID_SCHEMA",
        "component applies to STEP components, grid to OpenVDB files.",
      );
    }
    const specs: Record<string, Param> = contract.params;
    const values: Record<string, number> = {};
    // Expressions over the feature's own parameters and the model variables ({var});
    // the evaluated value lands in parameters, so a changed variable changes the hash
    // of exactly the features that use it (and with it their dependents).
    resolveExpressions(f, specs, variables);
    for (const k of Object.keys(f.parameters))
      requireThat(
        Object.hasOwn(specs, k),
        "INVALID_SCHEMA",
        `Parameter ${k} is not registered for ${name}.`,
      );
    for (const dep of f.depends_on) {
      requireThat(
        map.get(dep)!.authoritative_representation === contract.inputs,
        "OUT_OF_SCOPE",
        contract.inputs === "brep"
          ? "Native operators need B-Rep inputs. Field operations belong in the field AST."
          : `Operator ${name} needs inputs with representation ${contract.inputs}.`,
      );
      if (contract.output === "mesh")
        requireThat(
          map.get(dep)!.local_frame === f.local_frame,
          "OUT_OF_SCOPE",
          "Mesh operators work in the reference frame of their input.",
        );
    }
    for (const [k, s] of Object.entries(specs)) {
      const q = f.parameters[k];
      requireThat(q || s.optional, "INVALID_SCHEMA", `Parameter ${k} is missing.`);
      if (!q) continue;
      const value = quantity(q, s.dimension);
      requireThat(
        value >= (s.min ?? -Infinity) &&
          value <= (s.max ?? Infinity) &&
          (!s.integer || Number.isInteger(value)),
        "GEOMETRY_INVALID",
        `Parameter ${k} is outside its contract.`,
      );
      values[k] = value;
    }
    if (name === "torus")
      requireThat(
        values.major_radius > values.minor_radius,
        "GEOMETRY_INVALID",
        "Invalid torus.",
      );
    if (name === "groove")
      requireThat(
        values.radius > values.width / 2,
        "GEOMETRY_INVALID",
        "The groove width exceeds the inner radius.",
      );
    if (f.construction.operator === "strip") {
      const c = f.construction;
      const pts = [
        ...c.paths.flat(),
        ...(c.pads ?? []).map((pad) => pad.center),
      ].map((pt) => pt.map(Number));
      requireThat(
        pts.every((pt) => Math.abs(pt[2] - pts[0][2]) <= 1e-9),
        "GEOMETRY_INVALID",
        "Strip paths and pads must lie in a common z plane.",
      );
      requireThat(
        c.paths.every((path) =>
          path.every(
            (pt, i) =>
              i === 0 ||
              Math.hypot(
                Number(pt[0]) - Number(path[i - 1][0]),
                Number(pt[1]) - Number(path[i - 1][1]),
              ) > 1e-9,
          ),
        ),
        "GEOMETRY_INVALID",
        "The strip paths contain a zero-length segment.",
      );
      requireThat(
        (c.pads ?? []).every(
          (pad) => Number(pad.width) > 0 && Number(pad.depth) > 0,
        ),
        "GEOMETRY_INVALID",
        "Strip pads need a positive width and depth.",
      );
    }
    let occurrences = Math.max(
      1,
      f.depends_on.reduce((sum, dep) => sum + expanded.get(dep)!, 0),
    );
    if (
      f.construction.operator === "pattern" ||
      f.construction.operator === "circular_pattern"
    ) {
      const overrides = f.construction.overrides ?? [];
      requireThat(
        new Set(overrides.map((o) => o.index)).size === overrides.length &&
          overrides.every(
            (o) => o.index < values.count && (o.source || o.translation),
          ),
        "INVALID_SCHEMA",
        "Pattern variants need unique valid indices and an explicit change.",
      );
      const sources = new Set([
        f.depends_on[0],
        ...overrides.flatMap((o) => (o.source ? [o.source] : [])),
      ]);
      requireThat(
        sources.size === f.depends_on.length &&
          f.depends_on.every((dep) => sources.has(dep)),
        "INVALID_SCHEMA",
        "Pattern inputs must match the base shape and the explicitly referenced variants.",
      );
      requireThat(
        overrides.every(
          (o) =>
            !o.translation ||
            o.translation.every((x) => Math.abs(Number(x)) <= 1e6),
        ),
        "GEOMETRY_INVALID",
        "The variant offset exceeds the coordinate range.",
      );
      occurrences = values.count * expanded.get(f.depends_on[0])!;
      for (const o of overrides)
        if (o.source)
          occurrences +=
            expanded.get(o.source)! - expanded.get(f.depends_on[0])!;
    }
    expanded.set(f.id, occurrences);
    // Bound nested multiplicative expansion before native construction. Count
    // every dependent result retained by the job, including intermediate ones.
    if (f.depends_on.length) instances += occurrences;
    if (f.construction.operator === "line") {
      const c = f.construction;
      requireThat(
        [...c.start, ...c.end].every((x) => Math.abs(Number(x)) <= 1e6) &&
          c.start.some((x, i) => Number(x) !== Number(c.end[i])),
        "GEOMETRY_INVALID",
        "A line needs distinct, finite end points within the coordinate range.",
      );
    }
    if (f.construction.operator === "affine_transform")
      affineContract(f.construction.matrix, f.construction.translation);
    if (
      f.construction.operator === "rotate" ||
      f.construction.operator === "circular_pattern"
    ) {
      const c = f.construction;
      requireThat(
        [...c.axis, ...c.origin].every((x) => Math.abs(Number(x)) <= 1e6) &&
          Math.hypot(...c.axis.map(Number)) >= 1e-12,
        "GEOMETRY_INVALID",
        "A rotation needs a non-zero axis and finite origin coordinates.",
      );
      if (f.construction.operator === "circular_pattern")
        requireThat(
          values.count === 1 || Math.abs(values.angle) >= 1e-12,
          "GEOMETRY_INVALID",
          "A circular pattern with several occurrences needs a non-zero angular range.",
        );
    }
    if (f.construction.operator === "local_mesh_deform") {
      const c = f.construction;
      requireThat(
        c.handles.every((h) =>
          [...h.point, ...h.displacement].every(
            (x) => Math.abs(Number(x)) <= 1e6,
          ),
        ),
        "GEOMETRY_INVALID",
        "Handle coordinates out of range.",
      );
      requireThat(
        c.handles.every(
          (h) =>
            Math.hypot(...h.displacement.map(Number)) <= values.radius &&
            Math.hypot(...h.displacement.map(Number)) > 0,
        ),
        "GEOMETRY_INVALID",
        "Handle displacements must be non-zero and smaller than the region radius.",
      );
    }
    if (f.construction.operator === "offset_solid")
      requireThat(
        Math.abs(values.distance) >= 1e-5,
        "GEOMETRY_INVALID",
        "The offset distance must not be zero.",
      );
    if (f.construction.operator === "trim_surface")
      requireThat(
        map.get(f.depends_on[0])!.construction.operator === "nurbs_surface" &&
          values.u_min < values.u_max &&
          values.v_min < values.v_max,
        "GEOMETRY_INVALID",
        "A trim needs a NURBS patch and a valid UV rectangle in [0,1].",
      );
    let threadProfile: ReturnType<typeof isoBasicProfile> | null = null;
    if (f.construction.operator === "thread") {
      if (f.construction.standard === "iso_metric_basic") {
        requireThat(
          f.construction.designation &&
            [
              "root_radius",
              "pitch",
              "tooth_depth",
              "tooth_width",
              "crest_width",
            ].every((k) => !Object.hasOwn(f.parameters, k)),
          "INVALID_SCHEMA",
          "The ISO basic profile needs a designation and derives radius, pitch and tooth dimensions itself.",
        );
        threadProfile = isoBasicProfile(f.construction.designation);
        Object.assign(values, threadProfile.values);
      } else
        requireThat(
          !f.construction.designation &&
            ["root_radius", "pitch", "tooth_depth", "tooth_width"].every((k) =>
              Object.hasOwn(values, k),
            ),
          "INVALID_SCHEMA",
          "A custom thread needs core radius, pitch, tooth depth and tooth width.",
        );
      requireThat(
        f.depends_on.length === (f.construction.mode === "internal" ? 1 : 0) &&
          values.tooth_width < values.pitch &&
          values.tooth_depth < values.root_radius &&
          (values.crest_width ?? 0) < values.tooth_width &&
          (values.runout ?? 0) * 2 < values.height,
        "GEOMETRY_INVALID",
        "A thread needs a matching input, separate turns, a valid profile and a run-out shorter than half the height.",
      );
      requireThat(
        values.height / values.pitch <= 32,
        "BUDGET_EXCEEDED",
        "At most 32 explicit thread turns per feature.",
      );
    }
    requireThat(
      instances <= LIMITS.instances,
      "BUDGET_EXCEEDED",
      "Instance budget exceeded.",
      {
        alternatives: [
          {
            action: "reduce_pattern_count",
            maximum_instances: LIMITS.instances,
          },
          {
            action: "split_into_separate_parts",
            note: "each part has its own instance budget and job",
          },
          {
            action: "request_larger_approved_budget",
            note: "operator policy change; no silent relaxation",
          },
        ],
      },
    );
    let field;
    if (f.construction.operator === "field") {
      field = fieldContract(f.construction.expression);
      const cell = quantity(f.construction.cell_size, "length");
      const min = f.construction.domain.min.map(Number),
        max = f.construction.domain.max.map(Number);
      requireThat(
        cell >= 0.00001 &&
          cell <= 1000 &&
          max.every((v, i) => v > min[i] && v - min[i] <= 100000),
        "GEOMETRY_INVALID",
        "Invalid field domain or cell size.",
      );
      const extent = Math.max(...max.map((v, i) => v - min[i]));
      requireThat(
        extent / cell <= 1024,
        "BUDGET_EXCEEDED",
        "The local field resolution exceeds the budget.",
        {
          alternatives: [
            {
              action: "increase_cell_size",
              minimum_cell_size_mm: Number((extent / 1024).toPrecision(6)),
            },
            {
              action: "shrink_domain",
              maximum_extent_mm: Number((cell * 1024).toPrecision(6)),
              note: "edit a smaller region as its own detail feature",
            },
            {
              action: "coarser_preview_then_refine",
              note: "preview quality may be reduced; model tolerance is never relaxed",
            },
          ],
        },
      );
      field = {
        ...field,
        cell_size: cell,
        value_unit: fieldValueUnit(f.construction.expression),
      };
    }
    if ("points" in f.construction)
      for (const p of f.construction.points)
        requireThat(
          p.every((x) => Math.abs(Number(x)) <= 1e6),
          "GEOMETRY_INVALID",
          "Profile point out of range.",
        );
    if (f.construction.operator === "nurbs_curve") {
      const c = f.construction;
      validateBasis(c.basis, c.poles.length);
      requireThat(
        c.poles.every((p) => p.every((v) => Math.abs(Number(v)) <= 1e6)) &&
          c.weights.length === c.poles.length &&
          c.weights.every((w) => Number(w) >= 1e-12 && Number(w) <= 1e6),
        "INVALID_SCHEMA",
        "NURBS curve poles and weights do not fit together.",
      );
    }
    if (f.construction.operator === "nurbs_surface")
      validateSurface(f.construction);
    const { extras, assets } = oneShotOperator(f, values, map);
    const own = ownPlacement(f);
    if (own) {
      requireThat(
        f.authoritative_representation === "brep",
        "OUT_OF_SCOPE",
        "placement/plane applies to B-Rep features; fields and meshes use structure frames.",
      );
      extras.feature_placement = own;
      owned.set(f.id, own);
    }
    if (
      f.construction.operator === "extrude" &&
      !f.construction.axis &&
      !own &&
      owned.has(f.depends_on[0])
    ) {
      // A profile drawn on a plane is extruded along that plane's normal, carried
      // from the profile's frame into this feature's frame.
      const dep = map.get(f.depends_on[0])!;
      const z = owned.get(dep.id)!.rotation.map((row) => row[2]);
      const a = structure.placements[dep.local_frame].rotation,
        b = structure.placements[f.local_frame].rotation;
      const world = a.map((row) => row.reduce((sum, v, i) => sum + v * z[i], 0));
      extras.extrude_axis = [0, 1, 2].map((i) =>
        b.reduce((sum, row, j) => sum + row[i] * world[j], 0),
      );
    }
    hashes[f.id] = hash({
      registry: REGISTRY_HASH,
      feature: f,
      frame: structure.placements[f.local_frame].hash,
      inputs: f.depends_on.map((d) => hashes[d]),
      tolerance: ir.tolerance,
      // File contents a feature reads (font, SVG) belong to its identity.
      ...(Object.keys(assets).length ? { assets } : {}),
    });
    const blendRegions = ir.constraints
      .filter((c) => c.kind === "blend_free_region" && c.feature_id === f.id)
      .map((c: any) => ({
        id: c.id,
        min: c.min.map(Number),
        max: c.max.map(Number),
      }));
    compiled.push({
      ...f,
      ...extras,
      values,
      cache_key: hashes[f.id],
      local_cache_key:
        f.local_frame === "world"
          ? hashes[f.id]
          : hash([hashes[f.id], "local"]),
      placement: structure.placements[f.local_frame],
      field,
      ...(threadProfile ? { thread_profile: threadProfile } : {}),
      ...(blendRegions.length ? { blend_free_regions: blendRegions } : {}),
    });
    visiting.delete(fid);
    visited.add(fid);
  }
  features.forEach((f) => visit(f.id));
  ir.outputs.forEach((fid) =>
    requireThat(map.has(fid), "INVALID_SCHEMA", "The output feature is missing."),
  );
  checkModelLevel(
    ir,
    map,
    ir.structure
      ? new Set(ir.structure.parts.flatMap((p) => p.outputs))
      : null,
  );
  if (ir.profile === "watertight_solid")
    requireThat(
      ir.outputs.every(
        (fid) => map.get(fid)!.authoritative_representation === "mesh",
      ),
      "OUT_OF_SCOPE",
      "The mesh body profile explicitly needs authoritative mesh outputs; B-Rep and field keep their own profiles.",
    );
  if (ir.profile === "manufacturing_candidate") {
    requireThat(
      !!ir.manufacturing,
      "INVALID_SCHEMA",
      "The manufacturing candidate profile needs explicit process rules (manufacturing).",
    );
    requireThat(
      ir.outputs.every(
        (fid) => map.get(fid)!.authoritative_representation === "brep",
      ),
      "OUT_OF_SCOPE",
      "Manufacturing rules are sampled on native B-Rep outputs.",
    );
    const wall = quantity(ir.manufacturing!.minimum_wall, "length");
    const hole = ir.manufacturing!.minimum_hole_diameter
      ? quantity(ir.manufacturing!.minimum_hole_diameter, "length")
      : 0;
    const overhang = ir.manufacturing!.maximum_overhang
      ? quantity(ir.manufacturing!.maximum_overhang, "angle")
      : null;
    requireThat(
      wall > 0 &&
        wall <= 1000 &&
        hole >= 0 &&
        hole <= 1000 &&
        (overhang === null || (overhang >= 0 && overhang <= Math.PI / 2)) &&
        Math.hypot(...ir.manufacturing!.build_direction.map(Number)) >= 1e-9,
      "GEOMETRY_INVALID",
      "Invalid manufacturing rules.",
    );
  } else
    requireThat(
      !ir.manufacturing,
      "INVALID_SCHEMA",
      "Manufacturing rules apply only in the manufacturing candidate profile.",
    );
  requireThat(
    ir.constraints.filter((c) => c.kind === "patch_continuity").length <=
      LIMITS.patch_join_constraints,
    "BUDGET_EXCEEDED",
    "Too many exact patch junction checks.",
  );
  for (const c of ir.constraints) {
    if (c.kind === "protected_region" || c.kind === "change_region")
      requireThat(
        Object.hasOwn(structure.placements, c.local_frame ?? "world"),
        "INVALID_SCHEMA",
        "Unknown frame of the protection region.",
      );
    if (c.kind === "blend_free_region") {
      requireThat(
        map.get(c.feature_id)!.construction.operator === "field",
        "OUT_OF_SCOPE",
        "Fit regions without blend are registered for analytic fields.",
      );
      requireThat(
        c.min.every(
          (x, i) =>
            Number(c.max[i]) > Number(x) &&
            Math.abs(Number(x)) <= 1e6 &&
            Math.abs(Number(c.max[i])) <= 1e6,
        ),
        "GEOMETRY_INVALID",
        "Invalid fit region.",
      );
    }
    if (c.kind === "change_region") {
      requireThat(
        map.get(c.feature_id)!.construction.operator === "field",
        "OUT_OF_SCOPE",
        "Change regions are registered for analytic fields.",
      );
      const radius = Number(c.radius),
        margin = Number(c.compute_margin ?? "0");
      requireThat(
        c.center.every((x) => Math.abs(Number(x)) <= 1e6) &&
          radius >= 1e-5 &&
          radius <= 1e6 &&
          margin >= 0 &&
          margin <= 1e6,
        "GEOMETRY_INVALID",
        "Invalid change region.",
      );
    }
    if (c.kind === "patch_continuity")
      requireThat(
        map.get(c.feature_id)?.local_frame ===
          map.get(c.neighbor_feature_id)?.local_frame,
        "OUT_OF_SCOPE",
        "A patch junction needs a common explicit reference frame.",
      );
    if (c.kind === "equation")
      checkEquation(c, equationValues(ir, c.bindings), variables);
    requireThat(
      map.has(c.feature_id),
      "INVALID_SCHEMA",
      "The constraint reference is missing.",
    );
    if (c.kind === "patch_continuity") {
      requireThat(
        map.has(c.neighbor_feature_id),
        "INVALID_SCHEMA",
        "The neighbouring patch is missing.",
      );
      quantity(c.tolerance, "length");
      patchContinuity(map.get(c.feature_id)!, map.get(c.neighbor_feature_id)!);
    }
    if ("parameter" in c)
      requireThat(
        Object.hasOwn(map.get(c.feature_id)!.parameters, c.parameter),
        "INVALID_SCHEMA",
        "The constraint parameter is missing.",
      );
    if ("tolerance" in c && c.kind !== "volume" && c.kind !== "equation")
      requireThat(
        quantity(c.tolerance) >= 0,
        "INVALID_SCHEMA",
        "Negative constraint tolerance.",
      );
    if (c.kind === "dimension" || c.kind === "minimum") {
      requireThat(
        [
          "radius",
          "depth",
          "width",
          "height",
          "length",
          "remaining_wall",
        ].includes(c.metric),
        "OUT_OF_SCOPE",
        "The dimension is not registered for constraints.",
      );
      quantity(c.target, "length");
      if ("tolerance" in c) quantity(c.tolerance, "length");
    }
    if (c.kind === "parameter") {
      equalQuantity(map.get(c.feature_id)!.parameters[c.parameter], c.target);
      equalQuantity(c.target, c.tolerance);
    }
    if (c.kind === "volume")
      requireThat(
        Number(c.target.value) > 0 &&
          Number(c.target.value) <= 1e18 &&
          Number(c.tolerance.value) > 0 &&
          Number(c.tolerance.value) <= 1e9,
        "INVALID_SCHEMA",
        "Invalid volume contract.",
      );
    if (c.kind === "protected_bounds") quantity(c.tolerance, "length");
    if (c.kind === "surface_deviation") {
      requireThat(
        map.get(c.feature_id)!.construction.operator === "field",
        "OUT_OF_SCOPE",
        "Surface deviation is certified for analytic fields.",
      );
      const maximum = quantity(c.maximum, "length");
      requireThat(
        maximum >= 1e-6 && maximum <= 1000,
        "PRECISION_UNSUPPORTED",
        "Deviation bound: 0.000001 to 1000 mm.",
      );
    }
    if (c.kind === "protected_region")
      requireThat(
        c.max.every((v, i) => Number(v) > Number(c.min[i])),
        "INVALID_SCHEMA",
        "Invalid protection region.",
      );
  }
  requireThat(
    new Set(ir.constraints.map((c) => c.id)).size === ir.constraints.length,
    "INVALID_SCHEMA",
    "Duplicate constraint id.",
  );
  const tolerance = quantity(ir.tolerance, "length");
  requireThat(
    tolerance >= 1e-5 && tolerance <= 0.1,
    "PRECISION_UNSUPPORTED",
    "Supported model tolerance: 0.00001 to 0.1 mm.",
  );
  // Conditioning: world coordinate magnitude versus the requested tolerance.
  let magnitude = 0;
  for (const f of compiled) {
    for (const value of Object.values(f.values as Record<string, number>))
      magnitude = Math.max(magnitude, Math.abs(value));
    for (const x of f.placement?.translation ?? [])
      magnitude = Math.max(magnitude, Math.abs(x));
    for (const x of f.feature_placement?.translation ?? [])
      magnitude = Math.max(magnitude, Math.abs(x));
  }
  const ulp = Math.pow(2, Math.floor(Math.log2(Math.max(magnitude, 1))) - 52);
  requireThat(
    ulp * 1000 <= tolerance,
    "PRECISION_UNSUPPORTED",
    "World coordinates are too large for the requested tolerance; use local reference frames with a small origin.",
    { max_coordinate_magnitude_mm: magnitude, binary64_ulp_mm: ulp },
  );
  return {
    ir,
    features: compiled,
    outputs: ir.outputs,
    hashes,
    tolerance,
    profile: ir.profile,
    manufacturing: ir.manufacturing
      ? {
          process: ir.manufacturing.process,
          minimum_wall_mm: quantity(ir.manufacturing.minimum_wall, "length"),
          minimum_hole_diameter_mm: ir.manufacturing.minimum_hole_diameter
            ? quantity(ir.manufacturing.minimum_hole_diameter, "length")
            : null,
          maximum_overhang_deg: ir.manufacturing.maximum_overhang
            ? (quantity(ir.manufacturing.maximum_overhang, "angle") * 180) /
              Math.PI
            : null,
          build_direction: ir.manufacturing.build_direction.map(Number),
        }
      : null,
    registry_hash: REGISTRY_HASH,
    structure_hash: structure.structure_hash,
    structure: structure.structure,
    macro_expansion: macroExpansion,
    estimate: {
      features: compiled.length,
      instances,
      maximum_seconds: LIMITS.job_seconds,
    },
    conditioning: {
      max_coordinate_magnitude_mm: magnitude,
      binary64_ulp_at_max_mm: ulp,
      tolerance_mm: tolerance,
      ulp_to_tolerance_ratio: ulp / tolerance,
      rule: "1000 ulp must not exceed the model tolerance; frames with local origins keep details conditioned",
    },
  };
}
/** Registered dimension derivatives (mm per mm) with respect to constructor parameters. */
const DIMENSION_SOURCES: Record<string, Record<string, [string, number][]>> = {
  box: {
    width: [["width", 1]],
    depth: [["depth", 1]],
    height: [["height", 1]],
  },
  sphere: { radius: [["radius", 1]] },
  cylinder: { radius: [["radius", 1]], height: [["height", 1]] },
  strip: { height: [["height", 1]] },
  groove: {
    depth: [["depth", 1]],
    width: [["width", 1]],
    remaining_wall: [
      ["depth", -1],
      ["base.height", 1],
    ],
  },
  hole: { radius: [["radius", 1]], depth: [["depth", 1]] },
  pocket: {
    width: [["width", 1]],
    length: [["length", 1]],
    depth: [["depth", 1]],
  },
};
/** Explain which measured quantities a changed parameter drives; local linearization only. */
export function sensitivityReport(
  base: ModelIR,
  ir: ModelIR,
  changed: Set<string>,
) {
  const entries: any[] = [];
  const touched = new Set<string>();
  for (const f of ir.features) {
    const before = base.features.find((x) => x.id === f.id);
    for (const [name, q] of Object.entries(f.parameters))
      if (
        !before ||
        !before.parameters[name] ||
        !equalQuantity(before.parameters[name], q)
      )
        touched.add(f.id + ":" + name);
  }
  if (!touched.size) return entries;
  const dependants = (fid: string) =>
    ir.features.filter((x) => x.depends_on.includes(fid)).map((x) => x.id);
  for (const f of ir.features) {
    const sources = DIMENSION_SOURCES[f.construction.operator] ?? {};
    for (const [quantityName, terms] of Object.entries(sources))
      for (const [source, derivative] of terms) {
        const [ownerId, parameter] = source.startsWith("base.")
          ? [f.depends_on[0], source.slice(5)]
          : [f.id, source];
        if (!ownerId || !touched.has(ownerId + ":" + parameter)) continue;
        entries.push({
          feature_id: f.id,
          quantity: quantityName,
          parameter_feature: ownerId,
          parameter,
          derivative,
          unit: "mm_per_mm",
          method: "registered_analytic_dimension",
        });
      }
    for (const [target, expression] of Object.entries(f.expressions)) {
      const references = new Set<string>();
      const walk = (e: any) => {
        if (e.parameter) references.add(e.parameter);
        e.args?.forEach(walk);
      };
      walk(expression);
      for (const parameter of references) {
        if (!touched.has(f.id + ":" + parameter) || !f.parameters[parameter])
          continue;
        const current = quantity(f.parameters[parameter]);
        const h = Math.max(1e-6, Math.abs(current) * 1e-6);
        const at = (value: number) =>
          evaluate(
            expression,
            {
              ...f.parameters,
              [parameter]: {
                value: value.toFixed(12),
                unit: f.parameters[parameter].unit,
              },
            },
            ir.variables ?? {},
          ).value;
        const derivative = (at(current + h) - at(current - h)) / (2 * h);
        if (!Number.isFinite(derivative)) continue;
        entries.push({
          feature_id: f.id,
          quantity: "parameter:" + target,
          parameter_feature: f.id,
          parameter,
          derivative,
          unit: "per_parameter_unit",
          method: "expression_finite_difference",
        });
      }
    }
    for (const [ownerId] of [...touched].map((t) => t.split(":")))
      if (ownerId === f.id)
        for (const consumer of dependants(f.id))
          if (
            !entries.some(
              (e) =>
                e.feature_id === consumer &&
                e.quantity === "dependent_geometry",
            )
          )
            entries.push({
              feature_id: consumer,
              quantity: "dependent_geometry",
              parameter_feature: f.id,
              parameter: "*",
              derivative: 1,
              unit: "dirty_flag",
              method: "registered_analytic_dimension",
            });
  }
  return entries.slice(0, 64);
}
/** Bind changed field features to their base expression so the worker can certify d_H(Z_old, Z_new). */
export function attachDeviationReferences(base: ModelIR, plan: any) {
  for (const f of plan.features) {
    if (f.construction.operator !== "field") continue;
    const limits = plan.ir.constraints
      .filter(
        (c: any) => c.kind === "surface_deviation" && c.feature_id === f.id,
      )
      .map((c: any) => quantity(c.maximum, "length"));
    const previous = base.features.find((x) => x.id === f.id);
    if (!limits.length || previous?.construction.operator !== "field") continue;
    const before = previous.construction.expression;
    if (hash(before) === hash(f.construction.expression)) continue;
    f.reference_expression = before;
    f.deviation_epsilon = Math.min(...limits);
  }
  return plan;
}
type PatchOp = Patch["operations"][number];
type ModelOp = Extract<
  PatchOp,
  {
    op:
      | "set_model_settings"
      | "set_variable"
      | "prune_unreachable"
      | "add_part"
      | "add_frame"
      | "add_assembly"
      | "set_part_outputs"
      | "instantiate_fragment"
      | "set_configurations"
      | "set_decals";
  }
>;
const isModelOp = (op: PatchOp): op is ModelOp => MODEL_OPERATIONS.has(op.op);
/** Unit dimension of a parameter for compact add_feature (plain numbers). */
const dimensionOf: DimensionOf = (operator, name, construction) =>
  operator === "macro"
    ? (MACROS as any)[construction?.template]?.params[name]?.dimension
    : (OPERATORS as any)[operator]?.params[name]?.dimension;
export type PatchContext = {
  /** Resolves instantiate_fragment sources with the caller's read access. */
  fragmentSource?: (model: string, revision: string) => FragmentSource;
};
/** A compiled plan restricted to one configuration's outputs (exports, previews). */
export function selectConfiguration<T extends { ir: ModelIR; outputs: string[] }>(
  plan: T,
  configuration?: string,
): T {
  if (!configuration) return plan;
  const c = (plan.ir.configurations ?? []).find((x) => x.id === configuration);
  requireThat(c, "INVALID_SCHEMA", `Configuration ${configuration} does not exist.`, {
    configurations: (plan.ir.configurations ?? []).map((x) => x.id),
  });
  return { ...plan, outputs: [...c!.outputs], configuration: c!.id };
}
export function applyPatch(
  base: ModelIR,
  patch: Patch,
  context: PatchContext = {},
) {
  const ir = structuredClone(base);
  const changed = new Set<string>();
  const notes: string[] = [];
  const refinementReports: {
    feature_id: string;
    report: ReturnType<typeof refineSurface>["report"];
  }[] = [];
  for (const op of patch.operations) {
    if (isModelOp(op)) {
      applyModelOperation(ir, op, {
        changed,
        notes,
        operationId: hash(op).slice(0, 32),
        fragmentSource: context.fragmentSource,
      });
      continue;
    }
    if (op.op === "set_structure") {
      requireThat(
        hash(ir.structure ?? null) === op.expected_hash,
        "STALE_REVISION",
        "The project structure has already been changed.",
      );
      ir.structure = op.structure;
      continue;
    }
    if (op.op === "add_feature") {
      requireThat(
        !ir.features.some((f) => f.id === op.feature.id),
        "CONSTRAINT_CONFLICT",
        "The feature already exists.",
      );
      // Compact input (kind, units, plain numbers) becomes the canonical stored form.
      ir.features.push({
        ...normaliseFeature(op.feature, dimensionOf),
        lineage: {
          created_by_operation: hash(op).slice(0, 32),
          source_revision: patch.base_revision,
        },
      });
      changed.add(op.feature.id);
      continue;
    }
    if (op.op === "set_outputs") {
      ir.outputs = op.outputs;
      continue;
    }
    if (op.op === "add_constraint") {
      ir.constraints.push(op.constraint);
      continue;
    }
    const f = ir.features.find((f) => f.id === op.feature_id);
    requireThat(f, "AMBIGUOUS_SELECTION", "The feature cannot be resolved unambiguously.");
    if (op.op === "remove_feature") {
      requireThat(hash(f) === op.expected_hash, "STALE_REVISION", "The feature to remove has already been changed.");
      requireThat(
        !ir.constraints.some((c) => c.feature_id === f.id ||
          ("neighbor_feature_id" in c && c.neighbor_feature_id === f.id) ||
          ("bindings" in c && Object.values(c.bindings).some((b) => b.feature_id === f.id))),
        "OUT_OF_SCOPE", "A feature referenced by a constraint must not be removed.",
      );
      // Only this new revision changes. Final compile rejects dangling graph,
      // part-output and model-output references, including atomic rewires.
      ir.features = ir.features.filter((x) => x.id !== f.id);
      changed.add(f.id);
      continue;
    }
    if (op.op === "set_feature_dependencies") {
      requireThat(
        hash(f.depends_on) === op.expected_hash,
        "STALE_REVISION",
        "The feature dependencies have already been changed.",
      );
      // The ordinary compile below checks references, cycles, input counts,
      // representations and protected downstream features before any commit.
      f.depends_on = [...op.depends_on];
    } else if (op.op === "set_feature_context") {
      requireThat(
        contextHash(f) === op.expected_hash,
        "STALE_REVISION",
        "The feature context has already been changed.",
      );
      f.owner_part = op.owner_part;
      f.local_frame = op.local_frame;
    } else if (op.op === "set_pattern_occurrence") {
      requireThat(
        f.construction.operator === "pattern" ||
          f.construction.operator === "circular_pattern",
        "OUT_OF_SCOPE",
        "A single occurrence needs a linear or circular pattern.",
      );
      requireThat(
        hash(f.construction) === op.expected_hash,
        "STALE_REVISION",
        "The pattern has already been changed.",
      );
      requireThat(
        op.index < quantity(f.parameters.count, "scalar"),
        "INVALID_SCHEMA",
        "The occurrence is outside the pattern.",
      );
      const overrides = (f.construction.overrides ?? []).filter(
        (o) => o.index !== op.index,
      );
      if (op.override) overrides.push({ index: op.index, ...op.override });
      overrides.sort((a, b) => a.index - b.index);
      if (overrides.length) f.construction.overrides = overrides;
      else delete f.construction.overrides;
      f.depends_on = [
        ...new Set([
          f.depends_on[0],
          ...overrides.flatMap((o) => (o.source ? [o.source] : [])),
        ]),
      ];
    } else if (op.op === "set_construction") {
      requireThat(
        hash(f.construction) === op.expected_hash,
        "STALE_REVISION",
        "The construction has already been changed.",
      );
      f.construction = op.construction;
    } else if (op.op === "insert_surface_knots") {
      requireThat(
        f.construction.operator === "nurbs_surface",
        "OUT_OF_SCOPE",
        "Knot refinement needs a NURBS surface.",
      );
      requireThat(
        hash(f.construction) === op.expected_hash,
        "STALE_REVISION",
        "The NURBS construction has been changed.",
      );
      const tolerance = quantity(op.maximum_deviation, "length");
      requireThat(
        tolerance > 0 && tolerance <= quantity(base.tolerance, "length"),
        "PRECISION_UNSUPPORTED",
        "The refinement must be within the model accuracy.",
      );
      const refined = refineSurface(f.construction, op.direction, op.knots);
      const toleranceMM = Rational.mul(
        Rational.decimal(op.maximum_deviation.value),
        Rational.decimal(
          op.maximum_deviation.unit === "m"
            ? "1000"
            : op.maximum_deviation.unit === "um"
              ? "0.001"
              : "1",
        ),
      );
      requireThat(
        Rational.cmp(
          Rational.decimal(refined.report.geometric_error_bound_mm),
          toleranceMM,
        ) <= 0,
        "PRECISION_UNSUPPORTED",
        "Decimal rounding exceeds the permitted shape deviation.",
      );
      f.construction = refined.construction;
      refinementReports.push({ feature_id: f.id, report: refined.report });
    } else if (op.op === "set_surface_poles") {
      requireThat(
        f.construction.operator === "nurbs_surface",
        "OUT_OF_SCOPE",
        "The feature is not a NURBS patch.",
      );
      requireThat(
        hash(f.construction.poles) === op.expected_hash,
        "STALE_REVISION",
        "The control points have been changed.",
      );
      f.construction.poles = op.poles;
    } else if (op.op === "set_field") {
      requireThat(
        f.construction.operator === "field",
        "OUT_OF_SCOPE",
        "The feature is not a field.",
      );
      requireThat(
        hash(f.construction.expression) === op.expected_hash,
        "STALE_REVISION",
        "The field expression has been changed.",
      );
      f.construction.expression = op.expression;
    } else {
      requireThat(
        f.parameters[op.parameter],
        "INVALID_SCHEMA",
        "Unknown parameter.",
      );
      requireThat(
        equalQuantity(f.parameters[op.parameter], op.expected),
        "STALE_REVISION",
        "The expected parameter value does not match.",
      );
      if (op.op === "set_expression") {
        f.expressions[op.parameter] = op.expression;
      } else if (op.op === "solve_volume") {
        requireThat(
          !Object.hasOwn(f.expressions, op.parameter),
          "CONSTRAINT_CONFLICT",
          "A formula-driven parameter cannot be solved independently.",
        );
        const target = Number(op.target.value),
          params = Object.fromEntries(
            Object.entries(f.parameters).map(([k, q]) => [k, quantity(q)]),
          );
        let value: number;
        requireThat(
          Number.isFinite(target) && target > 0,
          "CONSTRAINT_CONFLICT",
          "A positive target volume is required.",
        );
        if (f.construction.operator === "sphere" && op.parameter === "radius")
          value = Math.cbrt((target * 3) / (4 * Math.PI));
        else if (
          f.construction.operator === "cylinder" &&
          op.parameter === "radius"
        )
          value = Math.sqrt(target / (Math.PI * params.height));
        else if (
          f.construction.operator === "cylinder" &&
          op.parameter === "height"
        )
          value = target / (Math.PI * params.radius ** 2);
        else if (
          f.construction.operator === "box" &&
          ["width", "depth", "height"].includes(op.parameter)
        )
          value =
            target /
            ["width", "depth", "height"]
              .filter((k) => k !== op.parameter)
              .reduce((v, k) => v * params[k], 1);
        else {
          requireThat(
            false,
            "OUT_OF_SCOPE",
            "Inverse volume construction is not registered for this feature.",
          );
        }
        requireThat(
          Number.isFinite(value) && value >= 1e-5 && value <= 100000,
          "CONSTRAINT_CONFLICT",
          "The inverse solution is outside the parameter contract.",
        );
        f.parameters[op.parameter] = { value: value.toFixed(12), unit: "mm" };
        const cid = "volume-" + f.id.slice(0, 100);
        const existing = ir.constraints.find((c) => c.id === cid);
        requireThat(
          !existing,
          "CONSTRAINT_CONFLICT",
          "The existing volume contract must be preserved.",
        );
        ir.constraints.push({
          id: cid,
          kind: "volume",
          feature_id: f.id,
          target: op.target,
          tolerance: op.tolerance,
        });
      } else {
        requireThat(
          !Object.hasOwn(f.expressions, op.parameter),
          "CONSTRAINT_CONFLICT",
          "A formula-driven parameter needs set_expression.",
        );
        f.parameters[op.parameter] = op.value;
      }
    }
    changed.add(f.id);
  }
  return finishPlan(base, ir, changed, refinementReports, notes);
}
/** cad_import format ir mode replace: the imported IR as a diff candidate of the base. */
export function planReplacement(base: ModelIR, input: unknown, baseRevision: string) {
  checkDepth(input);
  const incoming = parse<ModelIR>(ModelIR, input);
  const { ir, changed, summary } = replacementCandidate(base, incoming, baseRevision);
  return {
    ...finishPlan(base, ir, changed, [], summary.notes),
    import_diff: summary,
  };
}
/** Compile a candidate IR against its base: dirty features, protected constraints,
 *  sensitivity. Shared by patches and IR replacement imports. */
export function finishPlan(
  base: ModelIR,
  ir: ModelIR,
  changed: Set<string>,
  refinementReports: {
    feature_id: string;
    report: ReturnType<typeof refineSurface>["report"];
  }[] = [],
  notes: string[] = [],
) {
  const plan = compile(ir);
  const previous = compile(base);
  const dirty = new Set(changed);
  for (const f of plan.features)
    if (
      previous.hashes[f.id] !== plan.hashes[f.id] ||
      f.depends_on.some((d: string) => dirty.has(d))
    )
      dirty.add(f.id);
  for (const c of base.constraints) {
    if (c.kind === "protected_parameter") {
      const before = base.features.find((f) => f.id === c.feature_id)!,
        after = plan.ir.features.find((f) => f.id === c.feature_id)!;
      requireThat(
        equalQuantity(
          before.parameters[c.parameter],
          after.parameters[c.parameter],
        ),
        "OUT_OF_SCOPE",
        "A protected parameter must not be changed.",
      );
    }
    if (c.kind === "protected_feature")
      requireThat(
        !dirty.has(c.feature_id),
        "OUT_OF_SCOPE",
        "A protected feature would be affected.",
      );
  }
  return {
    ...plan,
    changed_features: [...changed],
    dependent_features: [...dirty].filter((x) => !changed.has(x)),
    dirty_features: [...dirty],
    refinement_reports: refinementReports,
    sensitivity: sensitivityReport(base, plan.ir, changed),
    notes,
  };
}
