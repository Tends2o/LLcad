/**
 * Model-facing JSON Schemas of the tool inputs (tools/list). They are what an agent reads
 * before every call, so they are generated for size and clarity, while the server still
 * validates every call with the strict zod schema:
 * - `io: "input"`: fields with a server default are optional, not required;
 * - `reused: "ref"` plus a structural pass: every sub-schema that occurs more than once is
 *   one named `$defs` entry instead of hundreds of inline copies;
 * - decimal strings also accept plain JSON numbers (the server normalises them to the
 *   canonical decimal string before validation, see units.normaliseDecimals).
 */
import { z } from "zod";

const DECIMAL_PATTERN = "^-?(0|[1-9][0-9]*)(\\.[0-9]+)?$";
type Json = any;

/** Readable names for the structural building blocks, recognised by content. */
function nameFor(def: Json): string | null {
  if (!def || typeof def !== "object") return null;
  if (def.type === "string" && def.pattern === DECIMAL_PATTERN) return "Decimal";
  if (def.type === "string" && def.pattern === "^[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}$")
    return "Id";
  if (def.type === "string" && def.pattern === "^[a-f0-9]{64}$") return "Hash";
  if (def.type === "string" && def.pattern === "^[a-z][a-z0-9_]{0,63}$") return "Name";
  const props = def.properties ?? {};
  const keys = Object.keys(props);
  if (keys.length === 2 && props.value && props.unit && def.type === "object")
    return "Quantity";
  if (def.type === "array" && def.minItems === 3 && def.maxItems === 3) {
    const items = JSON.stringify(def.prefixItems ?? def.items ?? []);
    if (items.includes("Decimal")) return "Point";
  }
  const variants: Json[] = def.oneOf ?? def.anyOf ?? [];
  if (variants.length > 3) {
    const key = ["operator", "op", "kind", "type"].find((k) =>
      variants.every(
        (v) =>
          v?.properties?.[k]?.const !== undefined ||
          v?.properties?.[k]?.enum !== undefined,
      ),
    );
    if (key) {
      const values = variants.flatMap(
        (v) => v.properties[key].const ?? v.properties[key].enum ?? [],
      );
      if (key === "operator" && values.includes("box")) return "Construction";
      if (key === "op" && values.includes("add_feature")) return "PatchOperation";
      if (key === "op" && values.includes("gyroid")) return "FieldNode";
      if (key === "kind" && values.includes("protected_parameter")) return "Constraint";
      if (key === "type" && values.includes("tangent_arc")) return "SketchSegment";
      if (key === "kind" && values.includes("rounded_rect")) return "SketchPreset";
    }
    if (variants.some((v) => v?.properties?.var) && variants.some((v) => v?.properties?.fn))
      return "Expression";
  }
  return null;
}

function walk(node: Json, visit: (n: Json) => void) {
  if (Array.isArray(node)) node.forEach((n) => walk(n, visit));
  else if (node && typeof node === "object") {
    visit(node);
    for (const value of Object.values(node)) walk(value, visit);
  }
}

/** Replace `$ref: "#/$defs/<from>"` by `<to>` everywhere. */
function renameRefs(root: Json, renames: Map<string, string>) {
  walk(root, (n) => {
    if (typeof n.$ref === "string" && n.$ref.startsWith("#/$defs/")) {
      const target = renames.get(n.$ref.slice(8));
      if (target) n.$ref = "#/$defs/" + target;
    }
  });
}

/**
 * Hoist identical sub-schemas (by serialisation) into $defs. Greedy by saved characters,
 * repeated until nothing worth hoisting remains. Nodes that are already a bare $ref are
 * left alone.
 */
function hoist(root: Json, minimum = 48) {
  const defs: Record<string, Json> = (root.$defs ??= {});
  let counter = 0;
  for (let round = 0; round < 64; round++) {
    const counts = new Map<string, number>();
    const scan = (node: Json, top: boolean) => {
      if (Array.isArray(node)) return node.forEach((n) => scan(n, false));
      if (!node || typeof node !== "object") return;
      if (!top && !(Object.keys(node).length === 1 && node.$ref)) {
        const text = JSON.stringify(node);
        if (text.length >= minimum) counts.set(text, (counts.get(text) ?? 0) + 1);
      }
      for (const [key, value] of Object.entries(node)) {
        // property maps are not schemas; their values are
        if (key === "properties" || key === "$defs" || key === "patternProperties") {
          for (const v of Object.values(value as object)) scan(v, key === "$defs");
        } else scan(value, false);
      }
    };
    scan(root, true);
    let best: string | null = null,
      bestSaving = 0;
    for (const [text, n] of counts) {
      const saving = (text.length - 24) * (n - 1) - 24;
      if (n >= 2 && saving > bestSaving) {
        best = text;
        bestSaving = saving;
      }
    }
    if (!best || bestSaving < minimum) break;
    const parsed = JSON.parse(best);
    let name = nameFor(parsed) ?? `S${++counter}`;
    while (defs[name] && JSON.stringify(defs[name]) !== best) name = `${name}_${++counter}`;
    defs[name] = parsed;
    const replace = (node: Json, top: boolean): Json => {
      if (Array.isArray(node)) return node.map((n) => replace(n, false));
      if (!node || typeof node !== "object") return node;
      if (!top && JSON.stringify(node) === best) return { $ref: "#/$defs/" + name };
      for (const [key, value] of Object.entries(node)) {
        if (key === "properties" || key === "patternProperties") {
          for (const [k, v] of Object.entries(value as object))
            (value as any)[k] = replace(v, false);
        } else if (key === "$defs") {
          for (const [k, v] of Object.entries(value as object))
            (value as any)[k] = k === name ? v : replace(v, true);
        } else node[key] = replace(value, false);
      }
      return node;
    };
    replace(root, true);
  }
}

/**
 * zod also extracts sub-schemas that carry metadata (a description) into $defs even when
 * they are used once, which leaves many one-line indirections. Inline every definition
 * that is referenced once (and not recursive) or is smaller than its reference.
 */
function inlineSingles(root: Json) {
  for (let round = 0; round < 16; round++) {
    const defs: Record<string, Json> = root.$defs ?? {};
    const counts = new Map<string, number>();
    walk(root, (n) => {
      if (typeof n.$ref === "string" && n.$ref.startsWith("#/$defs/"))
        counts.set(n.$ref.slice(8), (counts.get(n.$ref.slice(8)) ?? 0) + 1);
    });
    const inline = new Set(
      Object.keys(defs).filter((name) => {
        const text = JSON.stringify(defs[name]);
        if (text.includes(`"#/$defs/${name}"`)) return false;
        return (counts.get(name) ?? 0) <= 1 || text.length <= 32;
      }),
    );
    if (!inline.size) return;
    const replace = (node: Json): Json => {
      if (Array.isArray(node)) return node.map(replace);
      if (!node || typeof node !== "object") return node;
      if (typeof node.$ref === "string" && inline.has(node.$ref.slice(8))) {
        const { $ref, ...siblings } = node;
        return replace({ ...structuredClone(defs[$ref.slice(8)]), ...siblings });
      }
      for (const [key, value] of Object.entries(node))
        if (key !== "$defs") node[key] = replace(value);
      return node;
    };
    for (const name of Object.keys(defs))
      if (!inline.has(name)) defs[name] = replace(defs[name]);
    for (const [key, value] of Object.entries(root))
      if (key !== "$defs") root[key] = replace(value);
    for (const name of inline) delete defs[name];
  }
}

/**
 * Rarely used, large grammars are advertised by their discriminator only; the full
 * grammar is in llcad://operators and the server validates strictly (with the allowed
 * keys and options in the error, see units.parse).
 */
const LOOSE: Record<string, { key: string; text: string }> = {
  FieldNode: {
    key: "op",
    text: "Implicit field expression tree (node per op). Fields per op: llcad://operators, section 'Field nodes'; validated strictly by the server.",
  },
  Constraint: {
    key: "kind",
    text: "Registered constraint {id, kind, feature_id, ...}. Fields per kind: llcad://operators, section 'Constraints'; validated strictly by the server.",
  },
};
function loosen(root: Json) {
  const replace = (node: Json): Json => {
    if (Array.isArray(node)) return node.map(replace);
    if (!node || typeof node !== "object") return node;
    const name = nameFor(node);
    if (name && LOOSE[name]) {
      const { key, text } = LOOSE[name];
      const values = (node.oneOf ?? node.anyOf).flatMap(
        (v: Json) => v.properties?.[key]?.const ?? v.properties?.[key]?.enum ?? [],
      );
      return {
        type: "object",
        description: text,
        properties: { [key]: { type: "string", enum: [...new Set(values)] } },
        required: [key],
      };
    }
    for (const [k, v] of Object.entries(node)) node[k] = replace(v);
    return node;
  };
  for (const [k, v] of Object.entries(root)) root[k] = replace(v);
}
/** Only the root keeps additionalProperties:false: nested objects list their fields and the
 *  server rejects unknown keys with the allowed ones named. */
function openNested(root: Json) {
  walk(root, (n) => {
    if (n !== root && n.additionalProperties === false) delete n.additionalProperties;
  });
}

/** Decimal strings accept numbers; the pattern only constrains strings. */
function acceptNumbers(root: Json) {
  walk(root, (n) => {
    if (n.type === "string" && n.pattern === DECIMAL_PATTERN) {
      n.type = ["string", "number"];
      delete n.maxLength;
    }
  });
}

export function toolInputJSONSchema(schema: z.ZodType) {
  const json: Json = z.toJSONSchema(schema, {
    io: "input",
    reused: "ref",
    unrepresentable: "any",
  });
  delete json.$schema;
  // Give zod's generated names readable ones where the content is recognisable.
  const defs: Record<string, Json> = json.$defs ?? {};
  const renames = new Map<string, string>();
  const taken = new Set(Object.keys(defs));
  for (const [key, def] of Object.entries(defs)) {
    if (!key.startsWith("__schema")) continue;
    const name = nameFor(def);
    if (name && !taken.has(name)) {
      renames.set(key, name);
      taken.add(name);
    }
  }
  if (renames.size) {
    json.$defs = Object.fromEntries(
      Object.entries(defs).map(([k, v]) => [renames.get(k) ?? k, v]),
    );
    renameRefs(json, renames);
  }
  loosen(json);
  inlineSingles(json);
  openNested(json);
  acceptNumbers(json);
  hoist(json);
  // Remaining generated names: shorter, stable within one schema.
  const rest = Object.keys(json.$defs ?? {}).filter((k) => k.startsWith("__schema"));
  if (rest.length) {
    const short = new Map(rest.map((k) => [k, "S" + k.slice(8)]));
    json.$defs = Object.fromEntries(
      Object.entries(json.$defs).map(([k, v]) => [short.get(k) ?? k, v]),
    );
    renameRefs(json, short);
  }
  if (json.$defs && !Object.keys(json.$defs).length) delete json.$defs;
  return json;
}
