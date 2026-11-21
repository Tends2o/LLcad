import { z } from "zod";
import { Id } from "../semantic-ir/identifiers.js";
import { Box2, Camera, Digest, Landmark, SourceType } from "./schema.js";

/* Contracts of cad_view, cad_gates and cad_reference_register (registered by
 * semantic-ir/schema.ts and results.ts; the implementation is gates.ts). */
const finite = z.number().finite();
const count = z.number().int().nonnegative();
export const VIEW_PRESETS = ["front", "back", "left", "right", "top", "bottom", "iso", "iso_back"] as const;
export const VIEW_CHANNELS = ["lit", "albedo", "ids"] as const;
export const GATE_CHECKS = [
  "envelope",
  "stations",
  "contacts",
  "controls",
  "sightline",
  "sides",
  "counts",
  "name",
  "candidates",
  "images",
] as const;
const ViewId = z.string().regex(/^[a-zA-Z0-9][a-zA-Z0-9_-]{0,39}$/);

export const ViewSpec = z
  .strictObject({
    id: ViewId.optional(),
    preset: z.enum(VIEW_PRESETS).optional().describe("Framed view of the whole model; direction = target -> camera (front +Y, left +X, top +Z)."),
    projection: z.enum(["perspective", "orthographic"]).optional().describe("For presets (default perspective)."),
    fov_deg: finite.min(1).max(90).optional().describe("For perspective presets (default 28)."),
    camera: Camera.optional().describe("An absolute camera {mode: absolute, projection, position_mm, target_mm, up, fov_deg | mm_per_px, width_px, height_px, principal_px?} or the old bbox-relative one."),
    reference_id: Id.optional().describe("The camera of this reference of the model's reference contract; a registered reference also gets photo colours per part."),
  })
  .refine((v) => [v.preset, v.camera, v.reference_id].filter((x) => x !== undefined).length === 1, {
    message: "A view needs exactly one of preset, camera or reference_id",
  });
export const ViewInput = z.strictObject({
  model_id: Id,
  revision: Id.optional(),
  views: z
    .array(ViewSpec)
    .min(1)
    .max(8)
    .optional()
    .describe("Default: front, left, back, iso."),
  width: z.number().int().min(64).max(2576).default(1024),
  height: z.number().int().min(64).max(2576).default(1024),
  channels: z
    .array(z.enum(VIEW_CHANNELS))
    .min(1)
    .max(3)
    .default(["lit"])
    .describe("lit: shaded declared colours; albedo: flat declared colours (what colour gates compare); ids: one false colour per part."),
  return_images: z.boolean().default(true).describe("Attach the images as MCP image content (also stored as artifacts)."),
  parts: z.array(Id).min(1).max(64).optional().describe("Restrict the part table to these parts."),
  detail: z.enum(["brief", "full"]).default("brief").describe("brief: at most 24 parts per view."),
});
const PartRow = z.strictObject({
  part_id: Id,
  visible_px: count,
  bbox_px: z.tuple([count, count, count, count]),
  centroid_px: z.tuple([finite, finite]),
  mean_rgb: z.tuple([count, count, count]).optional(),
  delta_e00: finite.optional(),
  photo_rgb: z.tuple([count, count, count]).optional(),
  photo_delta_e00: finite.optional(),
});
const AbsoluteCameraOut = z.record(z.string(), z.unknown());
export const ViewResult = z.strictObject({
  model_id: Id,
  revision: Id,
  views: z.array(
    z.strictObject({
      id: ViewId,
      camera: AbsoluteCameraOut,
      mm_per_px: finite,
      reference_id: Id.optional(),
      photo_compared: z.boolean(),
      images: z.array(
        z.strictObject({ channel: z.enum(VIEW_CHANNELS), artifact_id: Id, width: count, height: count }),
      ),
      parts: z.array(PartRow),
      parts_visible: count,
    }),
  ),
  /** Per part once: name, declared colour and class (glass/emissive are excluded from colour gates). */
  parts: z.record(
    z.string(),
    z.strictObject({ name: z.string(), hex_declared: z.string(), class: z.enum(["opaque", "glass", "emissive"]) }),
  ),
  channels: z.array(z.enum(VIEW_CHANNELS)),
  images_returned: count,
  cached: z.boolean(),
  seconds: finite,
  note: z.string(),
});

export const GatesInput = z.strictObject({
  model_id: Id,
  revision: Id.optional(),
  contract_hash: Digest.optional().describe("Bind to this reference contract (cad_reference_report); default: the current one, if any."),
  checks: z.array(z.enum(GATE_CHECKS)).min(1).max(GATE_CHECKS.length).optional().describe("Subset of checks (default all)."),
  views: z.array(Id).min(1).max(12).optional().describe("Reference ids for the image checks (default: every reference with an absolute camera)."),
  wait_ms: z.number().int().min(0).max(110000).default(100000).describe("Wait for the contact-graph job; still running -> status running, call again."),
  detail: z.enum(["brief", "full"]).default("brief").describe("brief: the ranked failing/advisory residuals within about 2 KB; full: every row."),
});
const Residual = z.strictObject({
  check: z.string(),
  target: z.union([z.string(), finite]),
  measured: z.union([z.string(), finite, z.null()]),
  unit: z.string(),
  severity: z.enum(["fail", "warn", "pass", "info"]),
  where: z.string(),
  excess: finite,
  role: z.string().optional(),
  blocked_by: z.array(z.string()).optional(),
  delta_e00: finite.optional(),
});
export const GatesResult = z.strictObject({
  status: z.enum(["ok", "running"]),
  model_id: Id,
  revision: Id,
  contract_hash: Digest.nullable(),
  /** The deterministic floor: no hard gate failed. A floor, never a finish. */
  floor_passed: z.boolean().nullable(),
  summary: z.strictObject({ fail: count, warn: count, pass: count, info: count }),
  coverage: z.strictObject({
    native_checks: count,
    image_views: count,
    registered_views: count,
    advisory_views: count,
    contact_graph: z.enum(["done", "pending", "skipped", "failed"]),
  }),
  residuals: z.array(Residual),
  truncated: count,
  pending: z.array(z.string()).optional(),
  views: z.array(z.record(z.string(), z.unknown())).optional(),
  cached: z.boolean(),
  seconds: finite,
  note: z.string(),
});

export const RegisterInput = z.strictObject({
  model_id: Id,
  revision: Id.optional().describe("Revision whose geometry is fitted (default head)."),
  reference_id: Id,
  expected_contract_hash: Digest.optional(),
  landmarks: z
    .array(Landmark)
    .max(32)
    .optional()
    .describe("Image pixel [u,v] (original image, origin top-left) <-> feature_id (bound centre) or point_mm; >= 4 (perspective) / >= 3 (orthographic) spread, non-collinear, including asymmetric ones (button side, lens facing). Default: the reference's stored landmarks."),
  mode: z.enum(["perspective", "orthographic"]).optional().describe("Default from source_type: orthographic sources -> orthographic, else perspective."),
  source_type: SourceType.optional(),
  exclusion_boxes: z.array(Box2).max(16).optional().describe("Normalised [x0,y0,x1,y1] boxes left out of the silhouette (badges); stored on the reference."),
  mask_artifact_id: Id.optional().describe("Uploaded alpha/black-white mask of the object."),
  store: z.boolean().default(true).describe("Store camera, landmarks and residuals in the contract (new contract hash)."),
  max_seconds: z.number().int().min(20).max(150).default(95),
});
export const RegisterResult = z.strictObject({
  model_id: Id,
  revision: Id,
  reference_id: Id,
  registered: z.boolean(),
  reasons: z.array(z.string()),
  camera: AbsoluteCameraOut,
  residuals: z.record(z.string(), finite.nullable()),
  thresholds: z.record(z.string(), finite),
  landmarks: z.array(
    z.strictObject({
      id: z.string().nullable(),
      role: z.string(),
      px: z.tuple([finite, finite]),
      projected_px: z.tuple([finite, finite]),
      error_px: finite,
      error_mm: finite,
    }),
  ),
  facing: z.record(z.string(), z.unknown()),
  handedness: z.record(z.string(), z.unknown()),
  contract_hash: Digest,
  stored: z.boolean(),
  overlay_artifact_id: Id,
  evaluations: count,
  seconds: finite,
  note: z.string(),
});

