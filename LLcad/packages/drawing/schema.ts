import { z } from "zod";
import { Id } from "../semantic-ir/identifiers.js";

/* Contracts of cad_drawing_analyze, cad_drawing_render and cad_drawing_publish
 * (registered by semantic-ir/schema.ts and results.ts; the implementation is
 * drawing/service.ts, the drawing kernel drawing/drawing.py). */
const finite = z.number().finite();
const count = z.number().int().nonnegative();
const json = z.json();

export const DRAWING_ROLES = ["manufactured", "standard", "purchased", "reference", "duplicate"] as const;
export const DRAWING_FORMS = ["wire", "square_bar", "round_bar", "flat_bar", "sheet_metal", "plate", "tube", "3d_print", "casting", "other"] as const;

/** A drawing set as the agent describes it. Paths (B-rep, output folder) and the part
 *  to solid mapping belong to the service and are rejected here. Sheets are checked in
 *  depth by the kernel, whose problems come back as findings. */
export const DrawingSpec = z.strictObject({
  title: z
    .record(z.string().max(40), z.string().max(200))
    .optional()
    .describe(
      "Title block: title, supplementary_title, drawing_number, revision, material, surface, document_type, general_tolerance, tolerancing, created_by, created_date, approval_date, approved_by, replaces (scale and sheet are filled per sheet unless given).",
    ),
  company: z.array(z.string().max(80)).max(3).optional().describe("Three company lines of the title block (default: drawing-config.json of the service)."),
  parts: z
    .array(
      z.strictObject({
        id: Id,
        name: z.string().max(120),
        role: z.enum(DRAWING_ROLES).optional(),
        quantity: z.int().min(1).max(999).optional(),
      }),
    )
    .max(256)
    .optional()
    .describe("Name overrides for parts (ids from cad_drawing_analyze), optional role (manufactured|standard|purchased|reference|duplicate) and quantity."),
  changes: z.array(json).max(20).optional().describe("Change block rows [{index, date, name, text}] for every sheet."),
  sheets_reason: z
    .string()
    .min(10)
    .max(300)
    .optional()
    .describe("Only when the person explicitly asked for separate sheets: their wording. Without it, sheets whose content fits on one sheet block as sheets_mergeable."),
  sheets: z
    .array(z.record(z.string().max(40), json))
    .min(1)
    .max(20)
    .describe(
      "Sheets {format A4..A0, orientation, scale 'a:b', views[], annotations[], tables[], parts_list[], notes, changes, title}; see the tool description. Optional coverage claims (R54): annotation features [ids] = counted collective ('4× R2', '2× 0,5×45°'; equal fillets/corner radii or chamfers only), note line {text, general {kind radius|chamfer, size R|[width, angle], part?}} = general note for every otherwise uncovered element of that size, model_ref [ids] = reference to the 3D data set (freeform only).",
    ),
});

export const DrawingAnalyzeInput = z.strictObject({
  model_id: Id,
  revision: Id.optional(),
  parts: z.array(Id).min(1).max(64).optional().describe("Only these parts (default all)."),
  offset: z.int().min(0).default(0),
  limit: z.int().min(1).max(32).default(16),
});

export const DrawingFacts = z.strictObject({
  title: z
    .strictObject({
      title: z.string().max(120),
      supplementary_title: z.string().max(200).optional(),
      drawing_number: z.string().max(40).optional(),
    })
    .optional(),
  parts: z
    .array(
      z.strictObject({
        id: Id,
        designation: z.string().max(120).optional(),
        material: z.string().max(80).optional(),
        form: z.enum(DRAWING_FORMS).optional(),
        role: z.enum(DRAWING_ROLES).optional(),
        tolerances: z.array(z.string().max(40)).max(8).optional(),
      }),
    )
    .max(256)
    .optional(),
});

export const DrawingPlanInput = z.strictObject({
  model_id: Id,
  revision: Id.optional(),
  facts: DrawingFacts.optional().describe("Only what the model's own texts say (materials, names, roles, tolerances quoted literally); gaps are listed under open."),
  max_candidates: z.int().min(1).max(4).default(3),
});

export const DrawingRenderInput = z
  .strictObject({
    model_id: Id,
    revision: Id.optional(),
    spec: DrawingSpec.optional(),
    spec_ref: z
      .union([
        z.strictObject({ path: z.string().min(1).max(1000) }),
        z.strictObject({ artifact_id: Id }),
      ])
      .optional()
      .describe("{path}: absolute path of a *.json file with the spec, inside the folders patch_ref may read; {artifact_id}: a JSON artifact (e.g. a cad_drawing_plan candidate)."),
    crops: z
      .array(z.strictObject({ sheet: z.int().min(0).max(19), box_mm: z.tuple([finite, finite, finite, finite]) }))
      .max(8)
      .optional()
      .describe("Zoomed images of sheet regions [x0, y0, x1, y1] in sheet mm (origin bottom left)."),
    images: z
      .enum(["all", "crops", "none"])
      .default("all")
      .describe("Which images come back as MCP image content (all = every sheet and crop)."),
    strict: z.boolean().default(false).describe("New findings (coverage, thread, sections) block instead of warn."),
    autofix: z.boolean().default(false).describe("The kernel repairs layout findings itself."),
  })
  .refine((a) => (a.spec === undefined) !== (a.spec_ref === undefined), { message: "Exactly one of spec or spec_ref" });

export const DrawingPublishInput = z.strictObject({
  model_id: Id,
  revision: Id,
  render_id: Id,
  review: z.strictObject({
    sheets_viewed: z.int().min(1).max(20).describe("Number of sheets whose full image you looked at in this render."),
    statement: z.string().min(20).max(2000).describe("What you checked on every sheet (completeness, legibility, norms)."),
  }),
});

const Finding = z.strictObject({
  code: z.string(),
  severity: z.enum(["block", "warn"]),
  sheet: count,
  bbox_mm: z.array(finite).nullable(),
  message: z.string(),
});

export const DrawingAnalyzeResult = z.strictObject({
  model_id: Id,
  revision: Id,
  parts: z.array(json),
  parts_total: count,
  next_offset: count.nullable(),
  extents: json,
  suggestion: json,
  title_defaults: z.record(z.string(), z.string()),
  company: z.array(z.string()),
  conventions: z.string(),
});

export const DrawingRenderResult = z.strictObject({
  model_id: Id,
  revision: Id,
  render_id: Id,
  pdf_artifact_id: Id,
  sheets: z.array(
    z.strictObject({
      index: count,
      artifact_id: Id,
      format: z.string(),
      orientation: z.string(),
      width_px: count,
      height_px: count,
      size_mm: z.tuple([finite, finite]),
    }),
  ),
  crops: z.array(z.strictObject({ sheet: count, box_mm: z.tuple([finite, finite, finite, finite]), artifact_id: Id })),
  blocking: count,
  warnings_count: count,
  findings: z.array(Finding),
  findings_total: count,
  annotations: z.array(json),
  view_frames: json,
  images_returned: count,
  seconds: finite,
  note: z.string(),
  truncated: z.array(z.string()).optional(),
  coverage: json.optional(),
  autofix: json.optional(),
});

export const DrawingPlanResult = z.strictObject({
  model_id: Id,
  revision: Id,
  candidates: z.array(z.strictObject({ candidate_id: z.string(), spec_artifact_id: Id, metrics: json })),
  parts: z.array(json),
  notes: z.array(z.string()),
  open: z.array(z.string()),
  seconds: finite,
});

export const DrawingPublishResult = z.strictObject({
  model_id: Id,
  revision: Id,
  drawing_id: Id,
  render_id: Id,
  pdf_artifact_id: Id,
  sheets: count,
  published: z.literal(true),
  download: z.string(),
});
