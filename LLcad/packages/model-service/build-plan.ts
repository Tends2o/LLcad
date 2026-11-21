import {
  copyFileSync,
  existsSync,
  mkdirSync,
  readFileSync,
  writeFileSync,
} from "node:fs";
import { join } from "node:path";
import type { Store } from "./store.js";
import { requireThat } from "../semantic-ir/errors.js";
/** The structured plan an intake run writes for a new model (schema
 *  llcad-build-plan-1). It replaces the old 1,800-character prose purpose: the
 *  plan carries dimensions with tolerances, parts with colours and a
 *  construction strategy, details, checks and the reference views, and it is
 *  kept as two files next to the model — build-plan.json for tools and agents,
 *  build-plan.md for people. The model's purpose keeps only a short summary. */
export const BUILD_PLAN_SCHEMA_ID = "llcad-build-plan-1";
/** Reference categories accepted by cad_reference_setup requirements. */
const CATEGORIES = [
  "silhouette",
  "curvature",
  "placement",
  "continuity",
  "detail",
  "text",
  "material",
  "optics",
];
const text = (description?: string) => ({
  type: "string",
  ...(description ? { description } : {}),
});
const num = (description?: string) => ({
  type: "number",
  ...(description ? { description } : {}),
});
const vec3 = (description?: string) => ({
  type: "array",
  items: { type: "number" },
  minItems: 3,
  maxItems: 3,
  ...(description ? { description } : {}),
});
const measure = {
  type: "object",
  properties: {
    value_mm: num(),
    tolerance_mm: num("± in mm"),
    class: {
      type: "string",
      enum: ["A", "B", "C"],
      description:
        "A = manufacturer figure/measured, B = measured from an image of known scale, C = estimated",
    },
    source: text("source: sources[].id, image + measuring method or justification"),
  },
  required: ["value_mm", "tolerance_mm", "class", "source"],
};
/** JSON Schema handed to `claude --json-schema`. Loose on purpose: every array
 *  element keeps a free `notes` field, and additional properties are allowed,
 *  so an unusual product never has to be forced into the wrong box. */
export const BUILD_PLAN_SCHEMA = {
  type: "object",
  properties: {
    schema: { type: "string", enum: [BUILD_PLAN_SCHEMA_ID] },
    summary: text(
      "2–5 sentences: what is built, variant, main dimensions. Shown as the model purpose.",
    ),
    product: {
      type: "object",
      properties: {
        name: text(),
        manufacturer: text(),
        variant: text(),
        category: text(),
        description: text(),
      },
      required: ["name", "description"],
    },
    sources: {
      type: "array",
      items: {
        type: "object",
        properties: {
          id: text(),
          kind: {
            type: "string",
            enum: ["attachment", "web", "datasheet", "user_text", "estimate"],
          },
          ref: text("file name of the attachment or URL"),
          title: text(),
          notes: text(),
        },
        required: ["id", "kind", "ref"],
      },
    },
    frame: {
      type: "object",
      description:
        "Model frame in millimetres. LLcad standard views: front = camera on +Y, back = −Y, left = +X, right = −X, top = +Z.",
      properties: {
        origin: text("where the origin lies"),
        x_axis: text(),
        y_axis: text(),
        z_axis: text(),
        notes: text(),
      },
      required: ["origin", "x_axis", "y_axis", "z_axis"],
    },
    envelope: {
      type: "object",
      description: "Envelope dimensions of the product in the model frame",
      properties: { x: measure, y: measure, z: measure, notes: text() },
      required: ["x", "y", "z"],
    },
    parts: {
      type: "array",
      minItems: 1,
      items: {
        type: "object",
        properties: {
          id: text("part id, e.g. pt-grip"),
          name: text(
            "'material colour #RRGGBB · part', e.g. 'Plastic black #1A1A1A · Grip'",
          ),
          colour_hex: { type: "string", pattern: "^#[0-9A-Fa-f]{6}$" },
          finish: text("matte, satin, glossy, transparent …"),
          assembly: text(),
          bbox_mm: {
            type: "object",
            properties: { min: vec3(), max: vec3() },
            required: ["min", "max"],
          },
          construction: {
            type: "object",
            properties: {
              strategy: text(
                "construction: operators, profiles, sections, order",
              ),
              operators: { type: "array", items: { type: "string" } },
              notes: text(),
            },
            required: ["strategy"],
          },
          sections: {
            type: "array",
            description: "Cross-section table along the longitudinal axis",
            items: {
              type: "object",
              properties: {
                z_mm: num(),
                width_mm: num(),
                depth_mm: num(),
                corner_radius_mm: num(),
                notes: text(),
              },
              required: ["z_mm", "width_mm", "depth_mm"],
            },
          },
          interfaces: {
            type: "array",
            items: {
              type: "object",
              properties: {
                with: text("other part"),
                kind: {
                  type: "string",
                  enum: [
                    "contact",
                    "fit",
                    "joint",
                    "seam",
                    "gap",
                    "coaxial",
                    "other",
                  ],
                },
                gap_mm: num(),
                description: text(),
              },
              required: ["with", "kind"],
            },
          },
          visibility: {
            type: "string",
            enum: ["visible", "partly_visible", "internal"],
          },
          priority: { type: "string", enum: ["MVP", "detail", "internal"] },
          lane: text("work package for parallel part lanes"),
          notes: text(),
        },
        required: ["id", "name", "construction", "visibility", "priority"],
      },
    },
    details: {
      type: "array",
      items: {
        type: "object",
        properties: {
          id: text(),
          part: text(),
          type: text(
            "logo, text, button, led, window, knurl, seam, screw, label, other",
          ),
          position_mm: vec3(),
          size_mm: { type: "array", items: { type: "number" } },
          normal: vec3(),
          colour_hex: { type: "string", pattern: "^#[0-9A-Fa-f]{6}$" },
          source: {
            type: "object",
            properties: {
              ref: text("image file or URL"),
              bbox_px: {
                type: "array",
                items: { type: "number" },
                minItems: 4,
                maxItems: 4,
                description: "[x0, y0, x1, y1] in pixels of the original",
              },
            },
          },
          must_be_visible: { type: "boolean" },
          notes: text(),
        },
        required: ["id", "part", "type", "must_be_visible"],
      },
    },
    dimensions: {
      type: "array",
      items: {
        type: "object",
        properties: {
          id: text(),
          what: text(),
          part: text(),
          axis: {
            type: "string",
            enum: ["x", "y", "z", "diameter", "radius", "other"],
          },
          value_mm: num(),
          tolerance_mm: num(),
          class: { type: "string", enum: ["A", "B", "C"] },
          source: text(),
        },
        required: ["id", "what", "value_mm", "tolerance_mm", "class", "source"],
      },
    },
    checks: {
      type: "array",
      description: "Objectively verifiable acceptance criteria",
      items: {
        type: "object",
        properties: {
          id: text(),
          description: text(),
          category: { type: "string", enum: CATEGORIES },
          kind: {
            type: "string",
            enum: [
              "dimension",
              "visibility",
              "colour",
              "contact",
              "silhouette",
              "count",
              "other",
            ],
          },
          reference_ids: { type: "array", items: { type: "string" } },
          target: text(),
        },
        required: ["id", "description", "category"],
      },
    },
    references: {
      type: "array",
      items: {
        type: "object",
        properties: {
          id: text("e.g. ref-image-1"),
          source: text("file name of the attachment (image-1.jpg) or URL"),
          view: {
            type: "string",
            enum: [
              "front",
              "back",
              "left",
              "right",
              "top",
              "bottom",
              "iso",
              "detail",
              "other",
            ],
          },
          label: text(),
          width_px: num(),
          height_px: num(),
          camera_estimate: {
            type: "object",
            properties: {
              direction: vec3(
                "unit vector from model centre to camera in the model frame",
              ),
              up: vec3(),
              projection: {
                type: "string",
                enum: ["perspective", "orthographic"],
              },
              fov_deg: num("vertical field of view"),
              notes: text(),
            },
          },
          mm_per_px: num(),
          scale_basis: text("known length from which mm_per_px follows"),
          notes: text(),
        },
        required: ["id", "source", "view"],
      },
    },
    open_questions: { type: "array", items: { type: "string" } },
    notes: text(),
  },
  required: [
    "summary",
    "product",
    "sources",
    "frame",
    "envelope",
    "parts",
    "details",
    "dimensions",
    "checks",
    "references",
    "open_questions",
  ],
} as const;
export type BuildPlan = Record<string, any>;
/** Where a model's plan lives: beside the colour files, one folder per model. */
export function buildPlanDirectory(store: Store, model: string) {
  requireThat(
    /^[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}$/.test(model),
    "INVALID_SCHEMA",
    "Invalid model id.",
  );
  return join(store.root, "build-plans", model);
}
/** An answer the intake run produced: the structured output, or — when a run
 *  answered in text — JSON inside that text. Anything else is not a plan. */
export function parseBuildPlan(
  structured: unknown,
  text: unknown,
): BuildPlan | null {
  const candidates: unknown[] = [structured];
  if (typeof text === "string") {
    const stripped = text.replace(/^\s*```[a-z]*\n?|```\s*$/g, "").trim();
    if (stripped.startsWith("{")) {
      try {
        candidates.push(JSON.parse(stripped));
      } catch {
        /* prose, not a plan */
      }
    }
  }
  for (const c of candidates)
    if (
      c &&
      typeof c === "object" &&
      !Array.isArray(c) &&
      Array.isArray((c as any).parts) &&
      (c as any).parts.length > 0
    )
      return { schema: BUILD_PLAN_SCHEMA_ID, ...(c as any) };
  return null;
}
const list = (v: unknown): any[] => (Array.isArray(v) ? v : []);
const cell = (v: unknown) =>
  v === undefined || v === null || v === ""
    ? "—"
    : String(Array.isArray(v) ? v.join(", ") : v)
        .replace(/\|/g, "/")
        .replace(/\n+/g, " ");
const mm = (v: unknown) =>
  typeof v === "number" && Number.isFinite(v)
    ? `${Math.round(v * 1000) / 1000}`
    : "—";
const vec = (v: unknown) =>
  Array.isArray(v) ? `(${v.map((x) => mm(x)).join(", ")})` : "—";
function measureText(m: any) {
  if (!m || typeof m !== "object") return "—";
  return `${mm(m.value_mm)} ± ${mm(m.tolerance_mm)} mm [${m.class ?? "?"}] — ${cell(m.source)}`;
}
/** The same plan for people: headings and tables, nothing left out. */
export function renderBuildPlan(b: BuildPlan) {
  const product = b.product ?? {},
    frame = b.frame ?? {},
    envelope = b.envelope ?? {};
  const out: string[] = [
    `# Build plan: ${product.name ?? "—"}${product.variant ? ` (${product.variant})` : ""}`,
    "",
    `Schema \`${b.schema ?? BUILD_PLAN_SCHEMA_ID}\`. Machine-readable in \`build-plan.json\`.`,
    "",
    b.summary ?? "",
    "",
    "## Product",
    "",
    `- Name: ${cell(product.name)}`,
    `- Manufacturer: ${cell(product.manufacturer)}`,
    `- Variant: ${cell(product.variant)}`,
    `- Category: ${cell(product.category)}`,
    `- Description: ${cell(product.description)}`,
    "",
    "## Sources",
    "",
    ...(list(b.sources).length
      ? [
          "| ID | Kind | Reference | Title / note |",
          "| --- | --- | --- | --- |",
          ...list(b.sources).map(
            (s) =>
              `| ${cell(s.id)} | ${cell(s.kind)} | ${cell(s.ref)} | ${cell([s.title, s.notes].filter(Boolean).join(" — "))} |`,
          ),
        ]
      : ["- (none)"]),
    "",
    "## Frame",
    "",
    `- Origin: ${cell(frame.origin)}`,
    `- X: ${cell(frame.x_axis)}`,
    `- Y: ${cell(frame.y_axis)}`,
    `- Z: ${cell(frame.z_axis)}`,
    ...(frame.notes ? [`- Note: ${cell(frame.notes)}`] : []),
    "",
    "## Envelope dimensions",
    "",
    `- X: ${measureText(envelope.x)}`,
    `- Y: ${measureText(envelope.y)}`,
    `- Z: ${measureText(envelope.z)}`,
    ...(envelope.notes ? [`- Note: ${cell(envelope.notes)}`] : []),
    "",
    "## Parts",
    "",
    "| ID | Name | Priority | Visible | Lane | BBox min → max (mm) | Construction |",
    "| --- | --- | --- | --- | --- | --- | --- |",
    ...list(b.parts).map(
      (p) =>
        `| ${cell(p.id)} | ${cell(p.name)} | ${cell(p.priority)} | ${cell(p.visibility)} | ${cell(p.lane)} | ${vec(p.bbox_mm?.min)} → ${vec(p.bbox_mm?.max)} | ${cell(p.construction?.strategy)} |`,
    ),
    "",
  ];
  for (const p of list(b.parts)) {
    const sections = list(p.sections),
      interfaces = list(p.interfaces);
    if (
      !sections.length &&
      !interfaces.length &&
      !p.notes &&
      !p.finish &&
      !list(p.construction?.operators).length
    )
      continue;
    out.push(`### ${cell(p.id)} · ${cell(p.name)}`, "");
    if (p.finish) out.push(`- Surface finish: ${cell(p.finish)}`);
    if (p.colour_hex) out.push(`- Colour: ${cell(p.colour_hex)}`);
    if (list(p.construction?.operators).length)
      out.push(`- Operators: ${cell(p.construction.operators)}`);
    if (p.construction?.notes)
      out.push(`- Construction: ${cell(p.construction.notes)}`);
    if (p.notes) out.push(`- Note: ${cell(p.notes)}`);
    if (sections.length)
      out.push(
        "",
        "| z (mm) | Width | Depth | Corner radius | Note |",
        "| --- | --- | --- | --- | --- |",
        ...sections.map(
          (s) =>
            `| ${mm(s.z_mm)} | ${mm(s.width_mm)} | ${mm(s.depth_mm)} | ${mm(s.corner_radius_mm)} | ${cell(s.notes)} |`,
        ),
      );
    if (interfaces.length)
      out.push(
        "",
        ...interfaces.map(
          (i) =>
            `- Interface to ${cell(i.with)}: ${cell(i.kind)}${typeof i.gap_mm === "number" ? `, gap ${mm(i.gap_mm)} mm` : ""}${i.description ? ` — ${cell(i.description)}` : ""}`,
        ),
      );
    out.push("");
  }
  out.push(
    "## Details",
    "",
    ...(list(b.details).length
      ? [
          "| ID | Part | Kind | Position (mm) | Size (mm) | Normal | Colour | Source (px) | must be visible |",
          "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
          ...list(b.details).map(
            (d) =>
              `| ${cell(d.id)} | ${cell(d.part)} | ${cell(d.type)} | ${vec(d.position_mm)} | ${cell(list(d.size_mm).map(mm))} | ${vec(d.normal)} | ${cell(d.colour_hex)} | ${cell(d.source?.ref)} ${d.source?.bbox_px ? vec(d.source.bbox_px) : ""} | ${d.must_be_visible ? "yes" : "no"} |`,
          ),
        ]
      : ["- (none)"]),
    "",
    "## Dimensions",
    "",
    ...(list(b.dimensions).length
      ? [
          "| ID | What | Part | Axis | Value (mm) | ± | Class | Source |",
          "| --- | --- | --- | --- | --- | --- | --- | --- |",
          ...list(b.dimensions).map(
            (d) =>
              `| ${cell(d.id)} | ${cell(d.what)} | ${cell(d.part)} | ${cell(d.axis)} | ${mm(d.value_mm)} | ${mm(d.tolerance_mm)} | ${cell(d.class)} | ${cell(d.source)} |`,
          ),
        ]
      : ["- (none)"]),
    "",
    "## Checks",
    "",
    ...(list(b.checks).length
      ? list(b.checks).map(
          (c) =>
            `- **${cell(c.id)}** (${cell(c.category)}${c.kind ? `/${cell(c.kind)}` : ""}): ${cell(c.description)}${c.target ? ` — Target: ${cell(c.target)}` : ""}${list(c.reference_ids).length ? ` — References: ${cell(c.reference_ids)}` : ""}`,
        )
      : ["- (none)"]),
    "",
    "## Reference views",
    "",
    ...(list(b.references).length
      ? [
          "| ID | Source | View | Size (px) | mm/px | Camera estimate | Note |",
          "| --- | --- | --- | --- | --- | --- | --- |",
          ...list(b.references).map(
            (r) =>
              `| ${cell(r.id)} | ${cell(r.source)} | ${cell(r.view)} | ${r.width_px ? `${mm(r.width_px)}×${mm(r.height_px)}` : "—"} | ${mm(r.mm_per_px)}${r.scale_basis ? ` (${cell(r.scale_basis)})` : ""} | ${r.camera_estimate ? `direction ${vec(r.camera_estimate.direction)}, up ${vec(r.camera_estimate.up)}${typeof r.camera_estimate.fov_deg === "number" ? `, fov ${mm(r.camera_estimate.fov_deg)}°` : ""}` : "—"} | ${cell([r.label, r.notes].filter(Boolean).join(" — "))} |`,
          ),
        ]
      : ["- (none)"]),
    "",
    "## Open",
    "",
    ...(list(b.open_questions).length
      ? list(b.open_questions).map((q) => `- ${cell(q)}`)
      : ["- (nothing)"]),
    "",
  );
  if (b.notes) out.push("## Notes", "", String(b.notes), "");
  return out.join("\n");
}
/** What the model list shows as purpose: the summary, the product and the
 *  envelope — enough to recognise the model, not a plan to build from. */
export function purposeSummary(b: BuildPlan) {
  const env = b.envelope ?? {};
  const dims = ["x", "y", "z"]
    .map((axis) =>
      typeof env[axis]?.value_mm === "number"
        ? `${axis.toUpperCase()} ${mm(env[axis].value_mm)}`
        : null,
    )
    .filter(Boolean)
    .join(" × ");
  return [
    String(b.summary ?? b.product?.description ?? "").trim(),
    `**Product:** ${[b.product?.name, b.product?.variant].filter(Boolean).join(" · ") || "—"}${dims ? ` — envelope ${dims} mm` : ""}`,
    `**Build plan:** ${list(b.parts).length} parts, ${list(b.details).length} details, ${list(b.dimensions).length} dimensions, ${list(b.checks).length} checks, ${list(b.references).length} reference views (build-plan.md / build-plan.json).`,
    list(b.open_questions).length
      ? `**Open:** ${list(b.open_questions).slice(0, 4).map(cell).join("; ")}${list(b.open_questions).length > 4 ? " …" : ""}`
      : "",
  ]
    .filter(Boolean)
    .join("\n")
    .slice(0, 2000);
}
/** Keep a plan as the model's files. Returns the summary for models.purpose. */
export function storeBuildPlan(
  store: Store,
  model: string,
  b: BuildPlan,
  meta: Record<string, unknown>,
) {
  const directory = buildPlanDirectory(store, model);
  mkdirSync(directory, { recursive: true, mode: 0o700 });
  const plan = {
    ...b,
    schema: b.schema ?? BUILD_PLAN_SCHEMA_ID,
    _meta: { ...meta, model_id: model, stored_at: new Date().toISOString() },
  };
  writeFileSync(
    join(directory, "build-plan.json"),
    JSON.stringify(plan, null, 1),
    { mode: 0o600 },
  );
  writeFileSync(join(directory, "build-plan.md"), renderBuildPlan(plan), {
    mode: 0o600,
  });
  return { directory, summary: purposeSummary(plan) };
}
export function readBuildPlan(store: Store, model: string) {
  const directory = buildPlanDirectory(store, model),
    json = join(directory, "build-plan.json");
  if (!existsSync(json)) return null;
  try {
    return {
      json: JSON.parse(readFileSync(json, "utf8")) as BuildPlan,
      markdown: existsSync(join(directory, "build-plan.md"))
        ? readFileSync(join(directory, "build-plan.md"), "utf8")
        : null,
    };
  } catch {
    return null;
  }
}
/** Hand the plan to a build: both files next to the order. */
export function copyBuildPlan(store: Store, model: string, target: string) {
  const directory = buildPlanDirectory(store, model);
  let copied = 0;
  for (const name of ["build-plan.json", "build-plan.md"])
    if (existsSync(join(directory, name))) {
      copyFileSync(join(directory, name), join(target, name));
      copied++;
    }
  return copied > 0;
}
