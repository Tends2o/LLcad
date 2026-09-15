import { ModelIR, Feature, Patch, Quantity } from "../semantic-ir/schema.js";
import { parse, quantity, equalQuantity } from "../semantic-ir/units.js";
import { hash } from "../semantic-ir/hash.js";
import { requireThat } from "../semantic-ir/errors.js";
import { BUILD_HASH } from "./build.js";
import { evaluate } from "./expressions.js";
import { patchContinuity } from "./patches.js";
import { equationValues, checkEquation } from "./constraints.js";
import { validateBasis, validateSurface, refineSurface } from "./nurbs.js";
import * as Rational from "./bernstein.js";
import { affineContract } from "./affine.js";
import { compileStructure, contextHash } from "./structure.js";
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
const one = (
  params: Record<string, Param>,
  refs: [number, number],
  output = "brep",
) => ({
  version: 1,
  params,
  refs,
  output,
  required_validators: ["structure", "finite", "geometry", "constraints"],
  engine: "OCCT-7.9.3.1.1",
});
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
      root_radius: length,
      pitch: length,
      height: length,
      tooth_depth: length,
      tooth_width: length,
    },
    [0, 1],
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
  circle: one({ radius: length, ...position }, [0, 0]),
  bezier: one({}, [0, 0]),
  bspline: one({}, [0, 0]),
  nurbs_curve: one({}, [0, 0]),
  nurbs_surface: one({}, [0, 0]),
  extrude: one({ height: length }, [1, 1]),
  revolve: one({ angle }, [1, 1]),
  loft: one({}, [2, 16]),
  sweep: one({}, [2, 2]),
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
  fillet: one({ radius: length }, [1, 1]),
  chamfer: one({ distance: length }, [1, 1]),
  shell: one({ thickness: length }, [1, 1]),
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
} as const;
export const LIMITS = {
  features: 256,
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
    "Feld-Einheitenbaum ist zu tief.",
  );
  if (node.op === "gyroid") return "dimensionless";
  if (node.a) {
    const a = fieldValueUnit(node.a, depth + 1),
      b = fieldValueUnit(node.b, depth + 1);
    requireThat(
      a === b,
      "UNIT_MISMATCH",
      "CSG-Feldwerte benötigen gleiche Einheiten; zuerst ausdrücklich convert_field_unit verwenden.",
    );
    return a;
  }
  if (node.source) {
    const unit = fieldValueUnit(node.source, depth + 1);
    if (["shell", "offset", "local_field_delta"].includes(node.op))
      requireThat(
        unit === "length",
        "UNIT_MISMATCH",
        "Schalen, Offsets und lokale Feldamplituden benötigen längenwertige Felder.",
      );
    if (node.op === "convert_field_unit") {
      requireThat(
        node.to !== unit,
        "UNIT_MISMATCH",
        "Feld-Einheitenkonvertierung benötigt eine andere Zieleinheit.",
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
      "Datenstruktur ist zu groß oder zu tief.",
    );
    if (value && typeof value === "object")
      for (const child of Object.values(value)) stack.push([child, depth + 1]);
  }
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
    "Feldkomposition überschreitet den numerischen Lipschitz-Bereich [1e-12,1e12].",
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
    "Feld-AST überschreitet das Budget.",
  );
  const num = (v: string) => {
    const n = Number(v);
    requireThat(
      Number.isFinite(n) && Math.abs(n) <= 1e6,
      "INVALID_SCHEMA",
      "Ungültiger Feldparameter.",
    );
    return n;
  };
  const pos = (v: string) => {
    const n = num(v);
    requireThat(
      n >= 1e-5,
      "GEOMETRY_INVALID",
      "Positive Feldgröße erforderlich.",
    );
    return n;
  };
  if (node.center) node.center.forEach(num);
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
      "Ebenennormale darf nicht null sein.",
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
      "Ringradius muss größer als Rohradius sein.",
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
      "Referenzlänge für Feldwerte muss positiv und im unterstützten Bereich liegen.",
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
      "Felddrehung benötigt eine gültige Achse und einen Winkel zwischen -360 und 360 Grad.",
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
      "Variable Schalendicke muss im gesamten Definitionsgebiet positiv bleiben.",
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
      "Lokale Deformation verletzt die nachgewiesene Invertierbarkeitsgrenze.",
    );
    return { semantics: "general_implicit", lipschitz: a.lipschitz / (1 - k) };
  }
  num(node.distance);
  return { semantics: "general_implicit", lipschitz: a.lipschitz };
}
export function compile(input: unknown) {
  checkDepth(input);
  const ir = parse<ModelIR>(ModelIR, input);
  const structure = compileStructure(ir);
  const map = new Map<string, Feature>();
  let instances = 0;
  const expanded = new Map<string, number>();
  const compiled: any[] = [];
  const hashes: Record<string, string> = {};
  for (const f of ir.features) {
    requireThat(!map.has(f.id), "INVALID_SCHEMA", "Doppelte Feature-ID.");
    map.set(f.id, f);
  }
  const visited = new Set<string>(),
    visiting = new Set<string>();
  function visit(fid: string) {
    if (visited.has(fid)) return;
    requireThat(
      !visiting.has(fid),
      "CONSTRAINT_CONFLICT",
      "Zyklischer Feature-Graph.",
    );
    const f = map.get(fid);
    requireThat(f, "INVALID_SCHEMA", "Unbekannte Feature-Referenz.");
    visiting.add(fid);
    f.depends_on.forEach(visit);
    const name = f.construction.operator,
      contract = OPERATORS[name];
    requireThat(
      f.depends_on.length >= contract.refs[0] &&
        f.depends_on.length <= contract.refs[1],
      "INVALID_SCHEMA",
      "Falsche Anzahl von Operatoreingängen.",
    );
    requireThat(
      new Set(f.depends_on).size === f.depends_on.length ||
        ["union", "intersection", "difference"].includes(name),
      "INVALID_SCHEMA",
      "Doppelte Abhängigkeit.",
    );
    requireThat(
      contract.output === "source" ||
        f.authoritative_representation === contract.output,
      "OUT_OF_SCOPE",
      "Repräsentation passt nicht zum Operator.",
    );
    if (f.construction.operator === "imported")
      requireThat(
        f.authoritative_representation ===
          (f.construction.format === "stl" ? "mesh" : "brep"),
        "OUT_OF_SCOPE",
        "Importformat und geometrische Hoheit müssen übereinstimmen.",
      );
    const specs: Record<string, Param> = contract.params;
    const values: Record<string, number> = {};
    const solved = new Set<string>(),
      solving = new Set<string>();
    const solveExpression = (name: string) => {
      if (solved.has(name) || !Object.hasOwn(f.expressions, name)) return;
      requireThat(
        Object.hasOwn(specs, name) && Object.hasOwn(f.parameters, name),
        "INVALID_SCHEMA",
        "Formelziel ist kein registrierter Parameter.",
      );
      requireThat(
        !solving.has(name),
        "CONSTRAINT_CONFLICT",
        "Zyklische Parameterformel.",
      );
      solving.add(name);
      const references = (e: any) => {
        if ("parameter" in e) solveExpression(e.parameter);
        if (e.args) e.args.forEach(references);
      };
      references(f.expressions[name]);
      const value = evaluate(f.expressions[name], f.parameters),
        dimension = specs[name].dimension;
      requireThat(
        value.length === (dimension === "length" ? 1 : 0) &&
          value.angle === (dimension === "angle" ? 1 : 0),
        "UNIT_MISMATCH",
        "Formelergebnis passt nicht zur Parameterdimension.",
      );
      f.parameters[name] = {
        value: value.value.toFixed(12),
        unit:
          dimension === "length" ? "mm" : dimension === "angle" ? "rad" : "1",
      };
      solving.delete(name);
      solved.add(name);
    };
    Object.keys(f.expressions).forEach(solveExpression);
    for (const k of Object.keys(f.parameters))
      requireThat(
        Object.hasOwn(specs, k),
        "INVALID_SCHEMA",
        `Parameter ${k} ist für ${name} nicht registriert.`,
      );
    for (const dep of f.depends_on)
      requireThat(
        map.get(dep)!.authoritative_representation === "brep",
        "OUT_OF_SCOPE",
        "Native Operatoren benötigen B-Rep-Eingänge. Feldoperationen gehören in den Feld-AST.",
      );
    for (const [k, s] of Object.entries(specs)) {
      const q = f.parameters[k];
      requireThat(q || s.optional, "INVALID_SCHEMA", `Parameter ${k} fehlt.`);
      if (!q) continue;
      const value = quantity(q, s.dimension);
      requireThat(
        value >= (s.min ?? -Infinity) &&
          value <= (s.max ?? Infinity) &&
          (!s.integer || Number.isInteger(value)),
        "GEOMETRY_INVALID",
        `Parameter ${k} außerhalb seines Vertrags.`,
      );
      values[k] = value;
    }
    if (name === "torus")
      requireThat(
        values.major_radius > values.minor_radius,
        "GEOMETRY_INVALID",
        "Ungültiger Torus.",
      );
    if (name === "groove")
      requireThat(
        values.radius > values.width / 2,
        "GEOMETRY_INVALID",
        "Nutbreite überschreitet den Innenradius.",
      );
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
        "Mustervarianten benötigen eindeutige gültige Indizes und eine ausdrückliche Änderung.",
      );
      const sources = new Set([
        f.depends_on[0],
        ...overrides.flatMap((o) => (o.source ? [o.source] : [])),
      ]);
      requireThat(
        sources.size === f.depends_on.length &&
          f.depends_on.every((dep) => sources.has(dep)),
        "INVALID_SCHEMA",
        "Mustereingänge müssen Grundform und ausdrücklich referenzierten Varianten entsprechen.",
      );
      requireThat(
        overrides.every(
          (o) =>
            !o.translation ||
            o.translation.every((x) => Math.abs(Number(x)) <= 1e6),
        ),
        "GEOMETRY_INVALID",
        "Variantenverschiebung überschreitet den Koordinatenbereich.",
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
        "Linie benötigt verschiedene, endliche Endpunkte im Koordinatenbereich.",
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
        "Drehung benötigt eine von null verschiedene Achse und endliche Ursprungskoordinaten.",
      );
      if (f.construction.operator === "circular_pattern")
        requireThat(
          values.count === 1 || Math.abs(values.angle) >= 1e-12,
          "GEOMETRY_INVALID",
          "Ein Kreismuster mit mehreren Vorkommen benötigt einen von null verschiedenen Winkelbereich.",
        );
    }
    if (f.construction.operator === "trim_surface")
      requireThat(
        map.get(f.depends_on[0])!.construction.operator === "nurbs_surface" &&
          values.u_min < values.u_max &&
          values.v_min < values.v_max,
        "GEOMETRY_INVALID",
        "Trimmung benötigt einen NURBS-Patch und ein gültiges UV-Rechteck in [0,1].",
      );
    if (f.construction.operator === "thread") {
      requireThat(
        f.depends_on.length === (f.construction.mode === "internal" ? 1 : 0) &&
          values.tooth_width < values.pitch &&
          values.tooth_depth < values.root_radius,
        "GEOMETRY_INVALID",
        "Gewinde benötigt passenden Eingang, getrennte Windungen und ein gültiges Dreiecksprofil.",
      );
      requireThat(
        values.height / values.pitch <= 32,
        "BUDGET_EXCEEDED",
        "Maximal 32 explizite Gewindewindungen pro Feature.",
      );
    }
    requireThat(
      instances <= LIMITS.instances,
      "BUDGET_EXCEEDED",
      "Instanzbudget überschritten.",
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
        "Ungültige Felddomäne oder Zellweite.",
      );
      requireThat(
        Math.max(...max.map((v, i) => (v - min[i]) / cell)) <= 1024,
        "BUDGET_EXCEEDED",
        "Die lokale Feldauflösung überschreitet das Budget.",
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
          "Profilpunkt außerhalb des Bereichs.",
        );
    if (f.construction.operator === "nurbs_curve") {
      const c = f.construction;
      validateBasis(c.basis, c.poles.length);
      requireThat(
        c.poles.every((p) => p.every((v) => Math.abs(Number(v)) <= 1e6)) &&
          c.weights.length === c.poles.length &&
          c.weights.every((w) => Number(w) >= 1e-12 && Number(w) <= 1e6),
        "INVALID_SCHEMA",
        "NURBS-Kurvenpole und Gewichte passen nicht zusammen.",
      );
    }
    if (f.construction.operator === "nurbs_surface")
      validateSurface(f.construction);
    hashes[f.id] = hash({
      registry: REGISTRY_HASH,
      feature: f,
      frame: structure.placements[f.local_frame].hash,
      inputs: f.depends_on.map((d) => hashes[d]),
      tolerance: ir.tolerance,
    });
    compiled.push({
      ...f,
      values,
      cache_key: hashes[f.id],
      local_cache_key:
        f.local_frame === "world"
          ? hashes[f.id]
          : hash([hashes[f.id], "local"]),
      placement: structure.placements[f.local_frame],
      field,
    });
    visiting.delete(fid);
    visited.add(fid);
  }
  ir.features.forEach((f) => visit(f.id));
  ir.outputs.forEach((fid) =>
    requireThat(map.has(fid), "INVALID_SCHEMA", "Ausgabe-Feature fehlt."),
  );
  if (ir.profile === "watertight_solid")
    requireThat(
      ir.outputs.every(
        (fid) => map.get(fid)!.authoritative_representation === "mesh",
      ),
      "OUT_OF_SCOPE",
      "Das Meshkörperprofil benötigt ausdrücklich maßgebliche Mesh-Ausgaben; B-Rep und Feld behalten ihre eigenen Profile.",
    );
  requireThat(
    ir.constraints.filter((c) => c.kind === "patch_continuity").length <=
      LIMITS.patch_join_constraints,
    "BUDGET_EXCEEDED",
    "Zu viele exakte Patchanschlussprüfungen.",
  );
  for (const c of ir.constraints) {
    if (c.kind === "protected_region")
      requireThat(
        Object.hasOwn(structure.placements, c.local_frame ?? "world"),
        "INVALID_SCHEMA",
        "Unbekannter Rahmen der Schutzregion.",
      );
    if (c.kind === "patch_continuity")
      requireThat(
        map.get(c.feature_id)?.local_frame ===
          map.get(c.neighbor_feature_id)?.local_frame,
        "OUT_OF_SCOPE",
        "Patchanschluss benötigt einen gemeinsamen expliziten Bezugsrahmen.",
      );
    if (c.kind === "equation") checkEquation(c, equationValues(ir, c.bindings));
    requireThat(
      map.has(c.feature_id),
      "INVALID_SCHEMA",
      "Constraint-Referenz fehlt.",
    );
    if (c.kind === "patch_continuity") {
      requireThat(
        map.has(c.neighbor_feature_id),
        "INVALID_SCHEMA",
        "Nachbarpatch fehlt.",
      );
      quantity(c.tolerance, "length");
      patchContinuity(map.get(c.feature_id)!, map.get(c.neighbor_feature_id)!);
    }
    if ("parameter" in c)
      requireThat(
        Object.hasOwn(map.get(c.feature_id)!.parameters, c.parameter),
        "INVALID_SCHEMA",
        "Constraint-Parameter fehlt.",
      );
    if ("tolerance" in c && c.kind !== "volume" && c.kind !== "equation")
      requireThat(
        quantity(c.tolerance) >= 0,
        "INVALID_SCHEMA",
        "Negative Constraint-Toleranz.",
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
        "Dimension ist für Constraints nicht registriert.",
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
        "Ungültiger Volumenvertrag.",
      );
    if (c.kind === "protected_bounds") quantity(c.tolerance, "length");
    if (c.kind === "protected_region")
      requireThat(
        c.max.every((v, i) => Number(v) > Number(c.min[i])),
        "INVALID_SCHEMA",
        "Ungültige Schutzregion.",
      );
  }
  requireThat(
    new Set(ir.constraints.map((c) => c.id)).size === ir.constraints.length,
    "INVALID_SCHEMA",
    "Doppelte Constraint-ID.",
  );
  const tolerance = quantity(ir.tolerance, "length");
  requireThat(
    tolerance >= 1e-5 && tolerance <= 0.1,
    "PRECISION_UNSUPPORTED",
    "Unterstützte Modell-Toleranz: 0.00001 bis 0.1 mm.",
  );
  return {
    ir,
    features: compiled,
    outputs: ir.outputs,
    hashes,
    tolerance,
    profile: ir.profile,
    registry_hash: REGISTRY_HASH,
    structure_hash: structure.structure_hash,
    structure: structure.structure,
    estimate: {
      features: compiled.length,
      instances,
      maximum_seconds: LIMITS.job_seconds,
    },
  };
}
export function applyPatch(base: ModelIR, patch: Patch) {
  const ir = structuredClone(base);
  const changed = new Set<string>();
  const refinementReports: {
    feature_id: string;
    report: ReturnType<typeof refineSurface>["report"];
  }[] = [];
  for (const op of patch.operations) {
    if (op.op === "set_structure") {
      requireThat(
        hash(ir.structure ?? null) === op.expected_hash,
        "STALE_REVISION",
        "Die Projektstruktur wurde bereits geändert.",
      );
      ir.structure = op.structure;
      continue;
    }
    if (op.op === "add_feature") {
      requireThat(
        !ir.features.some((f) => f.id === op.feature.id),
        "CONSTRAINT_CONFLICT",
        "Feature existiert bereits.",
      );
      ir.features.push({
        ...op.feature,
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
    requireThat(f, "AMBIGUOUS_SELECTION", "Feature nicht eindeutig auflösbar.");
    if (op.op === "set_feature_context") {
      requireThat(
        contextHash(f) === op.expected_hash,
        "STALE_REVISION",
        "Der Featurekontext wurde bereits geändert.",
      );
      f.owner_part = op.owner_part;
      f.local_frame = op.local_frame;
    } else if (op.op === "set_pattern_occurrence") {
      requireThat(
        f.construction.operator === "pattern" ||
          f.construction.operator === "circular_pattern",
        "OUT_OF_SCOPE",
        "Einzelvorkommen benötigt ein lineares oder zyklisches Muster.",
      );
      requireThat(
        hash(f.construction) === op.expected_hash,
        "STALE_REVISION",
        "Das Muster wurde bereits geändert.",
      );
      requireThat(
        op.index < quantity(f.parameters.count, "scalar"),
        "INVALID_SCHEMA",
        "Vorkommen liegt außerhalb des Musters.",
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
        "Die Konstruktion wurde bereits geändert.",
      );
      f.construction = op.construction;
    } else if (op.op === "insert_surface_knots") {
      requireThat(
        f.construction.operator === "nurbs_surface",
        "OUT_OF_SCOPE",
        "Knotenverfeinerung benötigt eine NURBS-Fläche.",
      );
      requireThat(
        hash(f.construction) === op.expected_hash,
        "STALE_REVISION",
        "Die NURBS-Konstruktion wurde geändert.",
      );
      const tolerance = quantity(op.maximum_deviation, "length");
      requireThat(
        tolerance > 0 && tolerance <= quantity(base.tolerance, "length"),
        "PRECISION_UNSUPPORTED",
        "Verfeinerung muss innerhalb der Modellgenauigkeit liegen.",
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
        "Dezimalrundung überschreitet die erlaubte Formabweichung.",
      );
      f.construction = refined.construction;
      refinementReports.push({ feature_id: f.id, report: refined.report });
    } else if (op.op === "set_surface_poles") {
      requireThat(
        f.construction.operator === "nurbs_surface",
        "OUT_OF_SCOPE",
        "Feature ist kein NURBS-Patch.",
      );
      requireThat(
        hash(f.construction.poles) === op.expected_hash,
        "STALE_REVISION",
        "Kontrollpunkte wurden geändert.",
      );
      f.construction.poles = op.poles;
    } else if (op.op === "set_field") {
      requireThat(
        f.construction.operator === "field",
        "OUT_OF_SCOPE",
        "Feature ist kein Feld.",
      );
      requireThat(
        hash(f.construction.expression) === op.expected_hash,
        "STALE_REVISION",
        "Feldausdruck wurde geändert.",
      );
      f.construction.expression = op.expression;
    } else {
      requireThat(
        f.parameters[op.parameter],
        "INVALID_SCHEMA",
        "Unbekannter Parameter.",
      );
      requireThat(
        equalQuantity(f.parameters[op.parameter], op.expected),
        "STALE_REVISION",
        "Erwarteter Parameterwert stimmt nicht.",
      );
      if (op.op === "set_expression") {
        f.expressions[op.parameter] = op.expression;
      } else if (op.op === "solve_volume") {
        requireThat(
          !Object.hasOwn(f.expressions, op.parameter),
          "CONSTRAINT_CONFLICT",
          "Formelgesteuerter Parameter kann nicht unabhängig gelöst werden.",
        );
        const target = Number(op.target.value),
          params = Object.fromEntries(
            Object.entries(f.parameters).map(([k, q]) => [k, quantity(q)]),
          );
        let value: number;
        requireThat(
          Number.isFinite(target) && target > 0,
          "CONSTRAINT_CONFLICT",
          "Positives Zielvolumen erforderlich.",
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
            "Inverse Volumenkonstruktion ist für dieses Merkmal nicht registriert.",
          );
        }
        requireThat(
          Number.isFinite(value) && value >= 1e-5 && value <= 100000,
          "CONSTRAINT_CONFLICT",
          "Inverse Lösung liegt außerhalb des Parametervertrags.",
        );
        f.parameters[op.parameter] = { value: value.toFixed(12), unit: "mm" };
        const cid = "volume-" + f.id.slice(0, 100);
        const existing = ir.constraints.find((c) => c.id === cid);
        requireThat(
          !existing,
          "CONSTRAINT_CONFLICT",
          "Bestehender Volumenvertrag muss erhalten bleiben.",
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
          "Formelgesteuerter Parameter benötigt set_expression.",
        );
        f.parameters[op.parameter] = op.value;
      }
    }
    changed.add(f.id);
  }
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
        "Geschützter Parameter darf nicht geändert werden.",
      );
    }
    if (c.kind === "protected_feature")
      requireThat(
        !dirty.has(c.feature_id),
        "OUT_OF_SCOPE",
        "Geschütztes Feature wäre betroffen.",
      );
  }
  return {
    ...plan,
    changed_features: [...changed],
    dependent_features: [...dirty].filter((x) => !changed.has(x)),
    dirty_features: [...dirty],
    refinement_reports: refinementReports,
  };
}
