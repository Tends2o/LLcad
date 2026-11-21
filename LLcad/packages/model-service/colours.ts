import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import type { Store } from "./store.js";
import { Principal, authorize } from "../policy/index.js";
import { requireThat } from "../semantic-ir/errors.js";
/** What a model is really made of.
 *
 *  A part carries its material in its name — "Dark grey matte #2A2B2E ·
 *  Front shell grip" — and the full description in its purpose. That is enough
 *  to colour a model the way the thing looks in the world instead of by a
 *  palette that only tells parts apart. Saved material overrides survive
 *  rebuilds; parts added later receive their current derived colours. */
export const COLOUR_SCHEMA = "llcad-colours-1";
const HEX = /#([0-9a-fA-F]{6})\b/;
/** The materials this plan names, and what each one means for a shaded
 *  surface. The first rule that matches the material line wins — and only the
 *  material line, never the prose beneath it: "glass bead blasted" is an
 *  aluminium, not a glass. */
const MATERIALS: {
  match: RegExp;
  finish: string;
  colour?: string;
  roughness: number;
  metalness: number;
  opacity?: number;
  glow?: number;
}[] = [
  {
    match: /light guide|light pipe|fib(?:re|er) optic|light exit|translucent/i,
    finish: "translucent",
    colour: "#f4e7c6",
    roughness: 0.3,
    metalness: 0,
    opacity: 0.8,
    glow: 0.5,
  },
  {
    match: /\bled\b|phosphor/i,
    finish: "luminous",
    colour: "#f2e6a0",
    roughness: 0.35,
    metalness: 0,
    glow: 0.6,
  },
  {
    match: /glass(?! bead)|pmma|acrylic/i,
    finish: "clear glass",
    colour: "#e3ecea",
    roughness: 0.05,
    metalness: 0,
    opacity: 0.4,
  },
  {
    match: /gold|stainless|polished|chrom(?:e|ed|ium)|spring steel/i,
    finish: "polished metal",
    colour: "#bfc3c6",
    roughness: 0.26,
    metalness: 0.95,
  },
  {
    match: /alumin(?:i)?um|anodi[sz]ed|metal/i,
    finish: "satin metal",
    colour: "#c9cbcc",
    roughness: 0.42,
    metalness: 0.82,
  },
  {
    match: /silicone/i,
    finish: "silicone",
    colour: "#202123",
    roughness: 0.78,
    metalness: 0,
  },
  {
    match: /pcb|circuit board/i,
    finish: "matte",
    colour: "#1e6b3a",
    roughness: 0.8,
    metalness: 0.05,
  },
  {
    match: /batter(?:y|ies)|\bcell\b/i,
    finish: "matte",
    colour: "#3f6fb5",
    roughness: 0.5,
    metalness: 0.4,
  },
  {
    match: /satin/i,
    finish: "satin",
    roughness: 0.6,
    metalness: 0.05,
  },
  {
    match: /matte?|print|laser marking|plastic|polyimide|button/i,
    finish: "matte",
    roughness: 0.86,
    metalness: 0,
  },
];
/** Everything else on such a device is a matte surface. */
const PLAIN = { finish: "matte", roughness: 0.86, metalness: 0 };
function directory(store: Store) {
  return join(store.root, "colours");
}
function file(store: Store, model: string) {
  return join(directory(store), `${model}.json`);
}
/** One part's material, read out of the words the model was built with. */
function material(part: any) {
  const name = String(part.semantic_name ?? part.id),
    described = String(part.purpose?.value ?? ""),
    [left, right] = name.split(" · "),
    label = right ? left.trim() : "",
    title = (right ?? left ?? part.id).trim();
  const rule = MATERIALS.find((m) => m.match.test(label || title)) ?? PLAIN;
  // The value in the name is the measured one; a rule only fills what the name
  // leaves open, and the prose is the last resort for a colour.
  const hex = (label.match(HEX) ?? described.match(HEX) ?? [])[0];
  const colour = (hex ?? (rule as any).colour ?? "#9aa3a8").toLowerCase();
  return {
    part: part.id,
    name: title,
    material: label || null,
    colour,
    finish: rule.finish,
    roughness: rule.roughness,
    metalness: rule.metalness,
    opacity: (rule as any).opacity ?? 1,
    // A light guide or a lamp is lit on the device; it carries its own colour.
    emissive: (rule as any).glow ? colour : null,
    emissive_intensity: (rule as any).glow ?? 0,
  };
}
export function deriveColours(store: Store, p: Principal, model: string, revisionID?: string) {
  const revision = store.revision(p, model, revisionID),
    parts = (revision.ir.structure?.parts ?? []) as any[];
  const entries = parts
    .filter((part) => (part.outputs ?? []).length)
    .map((part) => material(part));
  return {
    schema: COLOUR_SCHEMA,
    model_id: model,
    revision: revision.id,
    name: "Original",
    description:
      "Colours and surface finishes as the parts are named in the model; derived from part name and purpose.",
    created: new Date().toISOString(),
    source: "derived_from_part_names",
    parts: Object.fromEntries<ReturnType<typeof material>>(
      entries.map((e) => [e.part, e]),
    ),
  };
}
/** Current visible parts with saved appearance overrides. Reading never writes:
 *  a read-only viewer can derive a fresh mask, and older masks remain intact. */
export function readColours(store: Store, p: Principal, model: string, revisionID?: string) {
  authorize(p, "model:read");
  const derived = deriveColours(store, p, model, revisionID);
  const path = file(store, model);
  if (existsSync(path)) {
    try {
      const stored = JSON.parse(readFileSync(path, "utf8"));
      if (
        stored?.schema === COLOUR_SCHEMA &&
        stored.parts &&
        typeof stored.parts === "object" &&
        !Array.isArray(stored.parts)
      ) {
        const parts = Object.fromEntries(
          Object.entries(derived.parts).map(([id, current]) => {
            const saved = Object.hasOwn(stored.parts, id)
              ? stored.parts[id]
              : null;
            const valid =
              saved &&
              typeof saved === "object" &&
              !Array.isArray(saved) &&
              typeof saved.colour === "string" &&
              /^#[0-9a-fA-F]{6}$/.test(saved.colour);
            return [
              id,
              valid
                ? {
                    ...current,
                    ...saved,
                    part: current.part,
                    name: current.name,
                    material: current.material,
                  }
                : current,
            ];
          }),
        );
        return {
          ...derived,
          ...stored,
          model_id: derived.model_id,
          revision: derived.revision,
          parts,
          stored: true,
        };
      }
    } catch {
      // An unreadable file is ignored, not repaired by a read operation.
    }
  }
  return { ...derived, stored: false };
}
export function writeColours(
  store: Store,
  p: Principal,
  model: string,
  mask: any,
) {
  authorize(p, "model:edit");
  store.model(p, model, "model:edit");
  requireThat(
    mask &&
      typeof mask === "object" &&
      mask.parts &&
      typeof mask.parts === "object",
    "INVALID_SCHEMA",
    "A colour mask needs a parts field.",
  );
  const parts = Object.entries(mask.parts as Record<string, any>);
  requireThat(
    parts.length <= 512 &&
      parts.every(
        ([id, entry]) =>
          /^[a-zA-Z0-9][\w-]{0,127}$/.test(id) &&
          typeof entry?.colour === "string" &&
          /^#[0-9a-fA-F]{6}$/.test(entry.colour),
      ),
    "INVALID_SCHEMA",
    "Every entry needs a colour of the form #rrggbb.",
  );
  const record = {
    ...mask,
    schema: COLOUR_SCHEMA,
    model_id: model,
    updated: new Date().toISOString(),
  };
  mkdirSync(directory(store), { recursive: true, mode: 0o700 });
  writeFileSync(file(store, model), JSON.stringify(record, null, 1), {
    mode: 0o600,
  });
  store.audit("model_colours_written", {
    model_id: model,
    parts: parts.length,
    source: record.source ?? "edited",
  });
  return { ...record, stored: true };
}
