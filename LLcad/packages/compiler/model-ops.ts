/**
 * Model-level IR operations (Ob): tolerance settings, model variables, reachability and
 * pruning, delta structure edits, configurations, decals, compact add_feature
 * normalisation, fragment instantiation across models and the IR import diff.
 * Everything here edits a candidate IR; applyPatch/compile in index.ts keep the
 * ordinary checks (dirty features, protected constraints, final strict compile).
 */
import { existsSync, realpathSync, statSync, readFileSync } from "node:fs";
import { createHash } from "node:crypto";
import { resolve, sep } from "node:path";
import {
  ModelIR,
  Feature,
  Quantity,
  TOLERANCE_PRESETS,
  VariableName,
  canonicalDecimal,
} from "../semantic-ir/schema.js";
import { hash } from "../semantic-ir/hash.js";
import { quantity, equalQuantity, parse } from "../semantic-ir/units.js";
import { requireThat } from "../semantic-ir/errors.js";
import { structureOf } from "./structure.js";
import { variablesOf } from "./expressions.js";
import { MACROS } from "./macros.js";
import { assetRoots } from "./fonts.js";

type Dimension = "length" | "angle" | "scalar";
const DECIMAL = /^-?(0|[1-9][0-9]*)(\.[0-9]+)?$/;

/* ------------------------------------------------------------------ settings */
/** What set_model_settings.expected_hash names (cad_get_model model_level.settings_hash). */
export const settingsHash = (ir: ModelIR) =>
  hash({ settings: ir.settings ?? null, tolerance: ir.tolerance });
/** Explicit settings request -> stored settings and the model tolerance. */
export function resolveSettings(input: {
  tolerance_mm?: string;
  profile_preset?: "precision_cad" | "visual_reconstruction";
}) {
  const preset = input.profile_preset;
  const presetValue = preset ? TOLERANCE_PRESETS[preset] : undefined;
  requireThat(
    presetValue !== undefined || input.tolerance_mm !== undefined,
    "INVALID_SCHEMA",
    "settings needs profile_preset or tolerance_mm.",
  );
  const tolerance = String(canonicalDecimal(Number(input.tolerance_mm ?? presetValue)));
  requireThat(
    !presetValue || Number(tolerance) === Number(presetValue),
    "INVALID_SCHEMA",
    `profile_preset ${preset} means ${presetValue} mm; tolerance_mm contradicts it.`,
  );
  requireThat(
    Number(tolerance) >= 1e-5 && Number(tolerance) <= 0.1,
    "PRECISION_UNSUPPORTED",
    "Supported model tolerance: 0.00001 to 0.1 mm.",
  );
  return {
    settings: { tolerance_mm: tolerance, ...(preset ? { profile_preset: preset } : {}) },
    tolerance: { value: tolerance, unit: "mm" as const },
  };
}
/** Stored settings must agree with the tolerance the kernel uses. */
export function checkSettings(ir: ModelIR) {
  if (!ir.settings) return;
  requireThat(
    equalQuantity(ir.tolerance, { value: ir.settings.tolerance_mm, unit: "mm" }),
    "INVALID_SCHEMA",
    "tolerance and settings.tolerance_mm contradict each other; use set_model_settings.",
  );
  const preset = ir.settings.profile_preset;
  if (preset)
    requireThat(
      Number(ir.settings.tolerance_mm) === Number(TOLERANCE_PRESETS[preset]),
      "INVALID_SCHEMA",
      `profile_preset ${preset} means ${TOLERANCE_PRESETS[preset]} mm.`,
    );
}

/* ------------------------------------------------------------ references */
/** Feature ids a construction names besides depends_on (face provenance selectors). */
export function mapConstructionRefs(c: any, fn: (id: string) => string) {
  const out = structuredClone(c);
  if (Array.isArray(out.overrides))
    out.overrides = out.overrides.map((o: any) => (o.source ? { ...o, source: fn(o.source) } : o));
  if (out.edge_selector && typeof out.edge_selector === "object")
    out.edge_selector.between_features = out.edge_selector.between_features.map(fn);
  for (const key of ["opening", "faces"])
    if (out[key] && typeof out[key] === "object" && typeof out[key].feature === "string")
      out[key].feature = fn(out[key].feature);
  return out;
}
export function featureRefs(f: Feature): string[] {
  const found: string[] = [...f.depends_on];
  mapConstructionRefs(f.construction, (id) => {
    found.push(id);
    return id;
  });
  return found;
}
/** A stored feature id for a reference: itself, or the macro a sub-feature id belongs to. */
function storedRef(ids: Set<string>, macros: Set<string>, id: string) {
  if (ids.has(id)) return id;
  const m = /^(.*)--[0-9]+$/.exec(id);
  return m && macros.has(m[1]) ? m[1] : null;
}
/** Features no output, part output, configuration, constraint or decal reaches. */
export function unreachableFeatures(ir: ModelIR, keep: string[] = []) {
  const ids = new Set(ir.features.map((f) => f.id));
  const macros = new Set(
    ir.features.filter((f) => f.construction.operator === "macro").map((f) => f.id),
  );
  const byId = new Map(ir.features.map((f) => [f.id, f]));
  const roots = [
    ...ir.outputs,
    ...(ir.structure?.parts ?? []).flatMap((p) => p.outputs),
    ...(ir.configurations ?? []).flatMap((c) => c.outputs),
    ...(ir.decals ?? []).map((d) => d.anchor.feature_id),
    ...ir.constraints.flatMap((c: any) => [
      c.feature_id,
      ...(c.neighbor_feature_id ? [c.neighbor_feature_id] : []),
      ...Object.values<any>(c.bindings ?? {}).map((b) => b.feature_id),
    ]),
    ...keep,
  ];
  const reached = new Set<string>();
  const stack = [...roots];
  while (stack.length) {
    const id = storedRef(ids, macros, stack.pop()!);
    if (!id || reached.has(id)) continue;
    reached.add(id);
    stack.push(...featureRefs(byId.get(id)!));
  }
  return ir.features.filter((f) => !reached.has(f.id)).map((f) => f.id);
}

/* ------------------------------------------------------------ compact form */
const isQuantity = (v: any) =>
  v && typeof v === "object" && typeof v.value === "string" && typeof v.unit === "string";
/** Dimension of a parameter of an operator or macro template; undefined when unknown. */
export type DimensionOf = (operator: string, name: string, construction: any) => Dimension | undefined;
/** Compact add_feature input -> canonical Feature (units from the registry, kind from the
 *  operator, numeric macro options moved into parameters). */
export function normaliseFeature(input: any, dimensionOf: DimensionOf): Feature {
  const construction = structuredClone(input.construction);
  const operator: string = construction.operator;
  const raw: Record<string, any> = { ...(input.parameters ?? {}) };
  if (operator === "macro" && construction.params) {
    const template = (MACROS as any)[construction.template];
    const options: Record<string, string> = {};
    for (const [k, v] of Object.entries<string>(construction.params))
      if (template?.params[k] && DECIMAL.test(v)) {
        requireThat(!(k in raw), "INVALID_SCHEMA", `Macro parameter ${k} given twice (params and parameters).`);
        raw[k] = v;
      } else options[k] = v;
    if (Object.keys(options).length) construction.params = options;
    else delete construction.params;
  }
  const parameters: Record<string, Quantity> = {};
  const expressions: Record<string, any> = { ...(input.expressions ?? {}) };
  for (const [name, value] of Object.entries<any>(raw)) {
    if (isQuantity(value)) parameters[name] = value;
    else if (typeof value === "string" || (value && typeof value === "object" && "value" in value)) {
      const number = typeof value === "string" ? value : value.value;
      let unit = typeof value === "object" ? value.unit : undefined;
      if (!unit) {
        const dimension = dimensionOf(operator, name, construction);
        requireThat(
          dimension,
          "INVALID_SCHEMA",
          `Parameter ${name} is not registered for ${operator === "macro" ? "macro " + construction.template : operator}; the unit cannot be derived.`,
        );
        unit = dimension === "length" ? "mm" : dimension === "angle" ? "deg" : "1";
      }
      parameters[name] = { value: number, unit };
    } else {
      requireThat(
        !Object.hasOwn(expressions, name),
        "INVALID_SCHEMA",
        `Parameter ${name} is an expression in both parameters and expressions.`,
      );
      expressions[name] = value;
    }
  }
  return parse<Feature>(Feature, {
    ...input,
    kind: input.kind ?? operator,
    parameters,
    expressions,
    construction,
  });
}

/* ------------------------------------------------------------- structure */
type Structure = NonNullable<ModelIR["structure"]>;
const structureIds = (s: Structure) =>
  new Set([s.project.id, ...s.frames.map((f) => f.id), ...s.assemblies.map((a) => a.id), ...s.parts.map((p) => p.id)]);
/** Delta structure edits start from an explicit structure (an implicit one is materialised). */
function explicitStructure(ir: ModelIR) {
  if (!ir.structure) ir.structure = structuredClone(structureOf(ir));
  return ir.structure!;
}
function freshStructureId(ir: ModelIR, id: string) {
  const s = explicitStructure(ir);
  requireThat(
    id !== "world" && !structureIds(s).has(id) && !ir.features.some((f) => f.id === id),
    "CONSTRAINT_CONFLICT",
    `Structure id ${id} is already taken.`,
  );
  return s;
}

/* ---------------------------------------------------------------- decals */
/** Path decals: inside the asset folders, an image/SVG file, optionally pinned by sha256. */
function checkDecalFile(path: string, sha256?: string) {
  const roots = assetRoots();
  const inside = (candidate: string) =>
    roots.some((root) => candidate === root || candidate.startsWith(root + sep));
  requireThat(
    inside(resolve(path)),
    "ACCESS_DENIED",
    "The decal path is outside the permitted directories (LLCAD_ASSET_PATHS).",
    { allowed_roots: roots },
  );
  requireThat(existsSync(path), "INVALID_SCHEMA", `Decal file ${path} does not exist.`);
  const real = realpathSync(path);
  requireThat(inside(real), "ACCESS_DENIED", "The decal path is outside the permitted directories.");
  requireThat(/\.(png|jpe?g|webp|svg)$/i.test(real), "INVALID_SCHEMA", "Decals are PNG, JPEG, WebP or SVG.");
  const info = statSync(real);
  requireThat(info.isFile() && info.size <= 16 * 1024 * 1024, "BUDGET_EXCEEDED", "The decal file is too large (at most 16 MiB).");
  if (sha256)
    requireThat(
      createHash("sha256").update(readFileSync(real)).digest("hex") === sha256,
      "STALE_REVISION",
      "The decal file has changed since it was pinned (sha256).",
    );
}

/* ------------------------------------------------------------- fragments */
export type FragmentSource = { ir: ModelIR; ir_hash: string };
function namespace(prefix: string) {
  let ns = prefix.toLowerCase().replace(/[^a-z0-9_]/g, "_").replace(/_+$/, "");
  if (!/^[a-z]/.test(ns)) ns = "f_" + ns;
  return ns;
}
function renameVariables(expression: any, fn: (name: string) => string): any {
  if (!expression || typeof expression !== "object") return expression;
  if (typeof expression.var === "string") return { var: fn(expression.var) };
  if (Array.isArray(expression.args))
    return { ...expression, args: expression.args.map((a: any) => renameVariables(a, fn)) };
  return expression;
}
/** Copy the dependency closure of root_ids from another model's revision with prefixed ids. */
export function instantiateFragment(
  ir: ModelIR,
  op: {
    source_model: string;
    source_revision: string;
    root_ids: string[];
    id_prefix: string;
    frame?: string;
    outputs?: boolean;
  },
  source: FragmentSource,
  operationId: string,
) {
  const src = source.ir;
  const byId = new Map(src.features.map((f) => [f.id, f]));
  const ids = new Set(byId.keys());
  const macros = new Set(src.features.filter((f) => f.construction.operator === "macro").map((f) => f.id));
  const srcStructure = structureOf(src);
  const parts = new Map(srcStructure.parts.map((p) => [p.id, p]));
  const roots: string[] = [];
  for (const rid of op.root_ids) {
    if (byId.has(rid)) roots.push(rid);
    else {
      const part = parts.get(rid);
      requireThat(
        part && part.outputs.length > 0,
        "INVALID_SCHEMA",
        `Root ${rid} is neither a feature nor a part with outputs in the source model.`,
      );
      roots.push(...part!.outputs);
    }
  }
  const closure = new Set<string>();
  const stack = [...roots];
  while (stack.length) {
    const id = storedRef(ids, macros, stack.pop()!);
    requireThat(id, "INVALID_SCHEMA", "The source model contains an open reference.");
    if (closure.has(id!)) continue;
    closure.add(id!);
    stack.push(...featureRefs(byId.get(id!)!));
  }
  requireThat(closure.size <= 1024, "BUDGET_EXCEEDED", "A fragment comprises at most 1024 features.");
  const prefixed = (id: string) => {
    const name = op.id_prefix + id;
    requireThat(
      name.length <= 128 && /^[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}$/.test(name),
      "INVALID_SCHEMA",
      `Prefixed id ${name} is invalid or too long.`,
    );
    return name;
  };
  const target = op.frame ?? "world";
  const frameId = (fid: string) => (fid === "world" ? target : prefixed(fid));
  const copiedSource = src.features.filter((f) => closure.has(f.id));
  const partIds = new Set(copiedSource.map((f) => f.owner_part));
  const frames = new Map(srcStructure.frames.map((f) => [f.id, f]));
  const neededFrames = new Set<string>();
  const needFrame = (fid: string) => {
    while (fid !== "world" && !neededFrames.has(fid)) {
      neededFrames.add(fid);
      fid = frames.get(fid)!.parent;
    }
  };
  copiedSource.forEach((f) => needFrame(f.local_frame));
  partIds.forEach((p) => needFrame(parts.get(p)!.local_frame));
  // Variables are namespaced by the prefix: the copy never rebinds the target's own.
  const ns = namespace(op.id_prefix);
  const variableName = (name: string) => {
    const renamed = `${ns}_${name}`;
    requireThat(VariableName.safeParse(renamed).success, "INVALID_SCHEMA", `Variable name ${renamed} is too long.`);
    return renamed;
  };
  const used = new Set<string>();
  copiedSource.forEach((f) => Object.values(f.expressions).forEach((e) => variablesOf(e, used)));
  const variables = { ...(ir.variables ?? {}) };
  for (const name of used) {
    const value = src.variables?.[name];
    requireThat(value, "INVALID_SCHEMA", `Source variable ${name} is missing.`);
    const renamed = variableName(name);
    if (variables[renamed])
      requireThat(
        equalQuantity(variables[renamed], value!),
        "CONSTRAINT_CONFLICT",
        `Variable ${renamed} exists in the target with a different value; choose another id_prefix.`,
      );
    else variables[renamed] = value!;
  }
  if (Object.keys(variables).length) ir.variables = variables;
  const s = explicitStructure(ir);
  const taken = new Set([...structureIds(s), ...ir.features.map((f) => f.id)]);
  const fresh = (id: string) => {
    requireThat(!taken.has(id), "CONSTRAINT_CONFLICT", `Id ${id} already exists in the target model; choose another id_prefix.`);
    taken.add(id);
    return id;
  };
  requireThat(target === "world" || s.frames.some((f) => f.id === target), "INVALID_SCHEMA", `Target frame ${target} does not exist.`);
  for (const f of srcStructure.frames)
    if (neededFrames.has(f.id)) s.frames.push({ ...f, id: fresh(prefixed(f.id)), parent: frameId(f.parent) });
  const exportOutputs = op.outputs !== false;
  for (const pid of partIds) {
    const part = parts.get(pid)!;
    s.parts.push({
      id: fresh(prefixed(part.id)),
      semantic_name: part.semantic_name,
      ...(part.purpose ? { purpose: part.purpose } : {}),
      local_frame: frameId(part.local_frame),
      authoritative_representation: part.authoritative_representation,
      outputs: exportOutputs ? part.outputs.filter((o) => closure.has(o)).map(prefixed) : [],
    });
  }
  const copied = copiedSource.map((f) => {
    const g = structuredClone(f);
    g.id = fresh(prefixed(f.id));
    g.owner_part = prefixed(f.owner_part);
    g.local_frame = frameId(f.local_frame);
    g.depends_on = f.depends_on.map(prefixed);
    g.construction = mapConstructionRefs(g.construction, prefixed);
    g.expressions = Object.fromEntries(
      Object.entries(g.expressions).map(([k, e]) => [k, renameVariables(e, variableName)]),
    );
    g.protected_relations = [];
    g.lineage = { created_by_operation: operationId, source_revision: op.source_revision };
    return g;
  });
  ir.features.push(...copied);
  if (exportOutputs)
    for (const o of src.outputs)
      if (closure.has(o) && !ir.outputs.includes(prefixed(o))) ir.outputs.push(prefixed(o));
  ir.fragments = [
    ...(ir.fragments ?? []),
    {
      id_prefix: op.id_prefix,
      source_model: op.source_model,
      source_revision: op.source_revision,
      source_ir_hash: source.ir_hash,
      root_ids: op.root_ids,
      feature_count: copied.length,
      created_by_operation: operationId,
    },
  ];
  return copied.map((f) => f.id);
}

/* ------------------------------------------------------ patch dispatch */
export const MODEL_OPERATIONS = new Set([
  "set_model_settings",
  "set_variable",
  "prune_unreachable",
  "add_part",
  "add_frame",
  "add_assembly",
  "set_part_outputs",
  "instantiate_fragment",
  "set_configurations",
  "set_decals",
]);
export type ModelOpContext = {
  changed: Set<string>;
  notes: string[];
  operationId: string;
  fragmentSource?: (model: string, revision: string) => FragmentSource;
};
/** Apply one model-level patch operation to the candidate IR (in place). */
export function applyModelOperation(ir: ModelIR, op: any, ctx: ModelOpContext) {
  switch (op.op) {
    case "set_model_settings": {
      requireThat(settingsHash(ir) === op.expected_hash, "STALE_REVISION", "The model settings have already been changed.", {
        current_hash: settingsHash(ir),
      });
      const { settings, tolerance } = resolveSettings(op.settings);
      ir.settings = settings;
      ir.tolerance = tolerance;
      ctx.notes.push(`Model tolerance ${tolerance.value} mm${settings.profile_preset ? ` (${settings.profile_preset})` : ""}: all features will be rebuilt.`);
      return;
    }
    case "set_variable": {
      const variables = { ...(ir.variables ?? {}) };
      const current = variables[op.name];
      if (current)
        requireThat(op.expected && equalQuantity(current, op.expected), "STALE_REVISION", "The expected variable value does not match.", {
          current,
        });
      else requireThat(!op.expected, "STALE_REVISION", `Variable ${op.name} does not exist; omit expected.`);
      const users = ir.features
        .filter((f) => Object.values(f.expressions).some((e) => variablesOf(e).has(op.name)))
        .map((f) => f.id);
      if (op.value === null) {
        requireThat(current, "INVALID_SCHEMA", `Variable ${op.name} does not exist.`);
        requireThat(users.length === 0, "CONSTRAINT_CONFLICT", `Variable ${op.name} is still in use.`, {
          used_by: users.slice(0, 32),
        });
        delete variables[op.name];
      } else {
        let value: Quantity;
        if (isQuantity(op.value)) value = op.value;
        else {
          requireThat(current, "INVALID_SCHEMA", "A new variable needs a quantity {value, unit}.");
          value = { value: String(canonicalDecimal(op.value)), unit: current!.unit };
        }
        requireThat(DECIMAL.test(value.value), "INVALID_SCHEMA", "The variable value is not a decimal number.");
        quantity(value);
        if (current) equalQuantity(current, value);
        variables[op.name] = value;
      }
      if (Object.keys(variables).length) ir.variables = variables;
      else delete ir.variables;
      users.forEach((id) => ctx.changed.add(id));
      return;
    }
    case "prune_unreachable": {
      const removed = unreachableFeatures(ir, op.keep ?? []);
      if (op.expected_removed)
        requireThat(
          hash([...op.expected_removed].sort()) === hash([...removed].sort()),
          "STALE_REVISION",
          "The set of unreachable features differs from expected_removed.",
          { unreachable: removed.slice(0, 64), count: removed.length },
        );
      const gone = new Set(removed);
      ir.features = ir.features.filter((f) => !gone.has(f.id));
      removed.forEach((id) => ctx.changed.add(id));
      ctx.notes.push(`prune_unreachable: ${removed.length} features removed${removed.length ? ": " + removed.slice(0, 24).join(", ") + (removed.length > 24 ? ", …" : "") : ""}.`);
      return;
    }
    case "add_part":
      freshStructureId(ir, op.part.id).parts.push(op.part);
      return;
    case "add_frame":
      freshStructureId(ir, op.frame.id).frames.push(op.frame);
      return;
    case "add_assembly":
      freshStructureId(ir, op.assembly.id).assemblies.push(op.assembly);
      return;
    case "set_part_outputs": {
      const part = explicitStructure(ir).parts.find((p) => p.id === op.part_id);
      requireThat(part, "AMBIGUOUS_SELECTION", `Part ${op.part_id} does not exist.`);
      requireThat(hash(part!.outputs) === op.expected_hash, "STALE_REVISION", "The part outputs have already been changed.", {
        current_hash: hash(part!.outputs),
      });
      part!.outputs = [...op.outputs];
      return;
    }
    case "instantiate_fragment": {
      requireThat(ctx.fragmentSource, "OUT_OF_SCOPE", "Fragment sources are resolved only through the model service.");
      const source = ctx.fragmentSource!(op.source_model, op.source_revision);
      const added = instantiateFragment(ir, op, source, ctx.operationId);
      added.forEach((id) => ctx.changed.add(id));
      ctx.notes.push(`instantiate_fragment: ${added.length} features from ${op.source_model}@${op.source_revision} with prefix ${op.id_prefix}.`);
      return;
    }
    case "set_configurations":
      requireThat(hash(ir.configurations ?? []) === op.expected_hash, "STALE_REVISION", "The configurations have already been changed.", {
        current_hash: hash(ir.configurations ?? []),
      });
      if (op.configurations.length) ir.configurations = op.configurations;
      else delete ir.configurations;
      return;
    case "set_decals":
      requireThat(hash(ir.decals ?? []) === op.expected_hash, "STALE_REVISION", "The decals have already been changed.", {
        current_hash: hash(ir.decals ?? []),
      });
      for (const decal of op.decals)
        if ("path" in decal.source) checkDecalFile(decal.source.path, decal.source.sha256);
      if (op.decals.length) ir.decals = op.decals;
      else delete ir.decals;
      return;
  }
  requireThat(false, "INVALID_SCHEMA", `Unknown model operation ${op.op}.`);
}

/** Model-level fields a compile checks (the feature graph is checked by compile itself). */
export function checkModelLevel(
  ir: ModelIR,
  features: Map<string, Feature>,
  partOutputs: Set<string> | null,
) {
  checkSettings(ir);
  const variables = Object.entries(ir.variables ?? {});
  requireThat(variables.length <= 256, "BUDGET_EXCEEDED", "At most 256 model variables.");
  for (const [, q] of variables) quantity(q);
  const configurations = ir.configurations ?? [];
  requireThat(
    new Set(configurations.map((c) => c.id)).size === configurations.length &&
      configurations.filter((c) => c.default).length <= 1,
    "INVALID_SCHEMA",
    "Configurations need unique ids and at most one default configuration.",
  );
  for (const c of configurations) {
    requireThat(new Set(c.outputs).size === c.outputs.length, "INVALID_SCHEMA", `Configuration ${c.id}: duplicate output.`);
    for (const o of c.outputs)
      requireThat(
        features.has(o) && (!partOutputs || partOutputs.has(o)),
        "INVALID_SCHEMA",
        `Configuration ${c.id}: output ${o} is missing or is not a registered part output.`,
      );
  }
  const decals = ir.decals ?? [];
  requireThat(new Set(decals.map((d) => d.id)).size === decals.length, "INVALID_SCHEMA", "Decals need unique ids.");
  for (const d of decals) {
    requireThat(features.has(d.anchor.feature_id), "INVALID_SCHEMA", `Decal ${d.id}: anchor feature is missing.`);
    const [w, h] = d.size_mm.map(Number);
    const point = d.anchor.point_mm.map(Number),
      normal = d.anchor.normal.map(Number);
    requireThat(
      w > 0 && h > 0 && w <= 10000 && h <= 10000 &&
        point.every((v) => Math.abs(v) <= 1e6) &&
        Math.hypot(...normal) >= 1e-9 &&
        Math.abs(Number(d.rotation_deg ?? "0")) <= 3600,
      "GEOMETRY_INVALID",
      `Decal ${d.id}: invalid size, position or normal.`,
    );
  }
}

/* ---------------------------------------------------------- IR import diff */
const FEATURE_FIELDS = [
  "semantic_name",
  "kind",
  "owner_part",
  "local_frame",
  "purpose",
  "authoritative_representation",
  "parameters",
  "expressions",
  "parameter_sources",
  "construction",
  "placement",
  "plane",
  "depends_on",
  "protected_relations",
] as const;
/** cad_import format ir mode replace: the imported IR as a candidate against the base, with
 *  every current constraint kept (removing requirements is never implicit). Returns the
 *  candidate IR, the changed feature ids and a diff in patch-operation terms. */
export function replacementCandidate(base: ModelIR, incoming: ModelIR, baseRevision: string) {
  requireThat(
    incoming.profile === base.profile,
    "OUT_OF_SCOPE",
    "An IR import (replace) keeps the quality profile of the model; the imported IR names a different one.",
    { model_profile: base.profile, import_profile: incoming.profile },
  );
  requireThat(
    equalQuantity(incoming.tolerance, base.tolerance) || !!incoming.settings,
    "OUT_OF_SCOPE",
    "A different model tolerance needs explicit settings in the imported IR (no silent relaxation).",
    { model_tolerance: base.tolerance, import_tolerance: incoming.tolerance },
  );
  const operationId = "import-" + hash(incoming).slice(0, 24);
  const counts: Record<string, number> = {};
  const count = (op: string, n = 1) => (counts[op] = (counts[op] ?? 0) + n);
  const changed = new Set<string>();
  const old = new Map(base.features.map((f) => [f.id, f]));
  const incomingIds = new Set(incoming.features.map((f) => f.id));
  const ir = structuredClone(incoming);
  ir.features = incoming.features.map((f) => {
    const before = old.get(f.id);
    const lineage = { created_by_operation: operationId, source_revision: baseRevision };
    if (!before) {
      count("add_feature");
      changed.add(f.id);
      return { ...f, lineage };
    }
    const differs = FEATURE_FIELDS.filter((k) => hash((before as any)[k] ?? null) !== hash((f as any)[k] ?? null));
    if (!differs.length) return before;
    changed.add(f.id);
    const rest = differs.filter((k) => !["parameters", "expressions", "construction", "depends_on", "owner_part", "local_frame"].includes(k));
    if (rest.length) count("replace_feature");
    else {
      if (differs.includes("parameters"))
        count(
          "set_parameter",
          Object.keys({ ...before.parameters, ...f.parameters }).filter(
            (k) => hash(before.parameters[k] ?? null) !== hash(f.parameters[k] ?? null),
          ).length,
        );
      if (differs.includes("expressions")) count("set_expression");
      if (differs.includes("construction")) count("set_construction");
      if (differs.includes("depends_on")) count("set_feature_dependencies");
      if (differs.includes("owner_part") || differs.includes("local_frame")) count("set_feature_context");
    }
    return { ...f, lineage };
  });
  for (const f of base.features)
    if (!incomingIds.has(f.id)) {
      count("remove_feature");
      changed.add(f.id);
    }
  const baseConstraints = new Map(base.constraints.map((c) => [c.id, c]));
  const incomingConstraints = new Set(incoming.constraints.map((c) => c.id));
  const added = incoming.constraints.filter((c) => !baseConstraints.has(c.id));
  const conflicting = incoming.constraints
    .filter((c) => baseConstraints.has(c.id) && hash(c) !== hash(baseConstraints.get(c.id)))
    .map((c) => c.id);
  const kept = base.constraints.filter((c) => !incomingConstraints.has(c.id)).map((c) => c.id);
  ir.constraints = [...base.constraints, ...added];
  if (added.length) count("add_constraint", added.length);
  ir.assumptions = [...new Set([...incoming.assumptions, ...base.assumptions])].slice(0, 64);
  if (hash(base.structure ?? null) !== hash(incoming.structure ?? null)) count("set_structure");
  if (hash(base.outputs) !== hash(incoming.outputs)) count("set_outputs");
  if (settingsHash(base) !== settingsHash(incoming)) count("set_model_settings");
  if (hash(base.variables ?? {}) !== hash(incoming.variables ?? {})) count("set_variable");
  if (hash(base.configurations ?? []) !== hash(incoming.configurations ?? [])) count("set_configurations");
  if (hash(base.decals ?? []) !== hash(incoming.decals ?? [])) count("set_decals");
  const notes = [
    ...(kept.length ? [`${kept.length} existing constraints are missing from the import and are kept: ${kept.slice(0, 16).join(", ")}.`] : []),
    ...(conflicting.length ? [`${conflicting.length} constraints with the same id differ; the existing version is kept: ${conflicting.slice(0, 16).join(", ")}.`] : []),
  ];
  return {
    ir,
    changed,
    summary: {
      operations: counts,
      changed_features: changed.size,
      unchanged_features: incoming.features.length - [...changed].filter((id) => incomingIds.has(id)).length,
      kept_constraints: kept.length,
      notes,
    },
  };
}
