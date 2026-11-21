import { z } from "zod";
import { Id } from "../semantic-ir/identifiers.js";

const finite = z.number().finite();
export const Digest = z.string().regex(/^[a-f0-9]{64}$/);
export const V3 = z.tuple([finite, finite, finite]);
export const Box2 = z.tuple([
  finite.min(0).max(1),
  finite.min(0).max(1),
  finite.min(0).max(1),
  finite.min(0).max(1),
]);
/** The old camera: framed relative to the current model's bounding box. */
export const RelativeCamera = z.strictObject({
  direction: V3.refine(
    (v) => Math.hypot(...v) > 0.001,
    "Nonzero camera direction required",
  ),
  up: V3.default([0, 0, 1]),
  projection: z.enum(["perspective", "orthographic"]).default("perspective"),
  target: V3.optional(),
  distance_factor: finite.min(0.15).max(10).default(2.5),
  fov: finite.min(10).max(90).default(28),
});
/** A real camera in millimetres for one image of width_px x height_px pixels.
 *  Shared contract of this schema, the viewer's captureReference and the VTK
 *  renderer; it is never rescaled to the model's bounds. */
export const AbsoluteCamera = z
  .strictObject({
    mode: z.literal("absolute"),
    projection: z.enum(["perspective", "orthographic"]).default("perspective"),
    position_mm: V3,
    target_mm: V3,
    up: V3.default([0, 0, 1]),
    /** Perspective: vertical field of view in degrees. */
    fov_deg: finite.gt(0.1).lt(179).optional(),
    /** Orthographic: millimetres per image pixel. */
    mm_per_px: finite.positive().max(1000).optional(),
    width_px: z.number().int().min(16).max(16384),
    height_px: z.number().int().min(16).max(16384),
    principal_px: z.tuple([finite, finite]).optional(),
  })
  .refine(
    (c) => (c.projection === "orthographic" ? !!c.mm_per_px : !!c.fov_deg),
    "Perspective cameras need fov_deg, orthographic cameras need mm_per_px",
  )
  .refine(
    (c) => Math.hypot(...c.position_mm.map((v, i) => v - c.target_mm[i])) > 1e-6,
    "position_mm and target_mm must differ",
  );
export const Camera = z.union([RelativeCamera, AbsoluteCamera]);
export type AbsoluteCamera = z.infer<typeof AbsoluteCamera>;
export const isAbsolute = (c: any): c is AbsoluteCamera => c?.mode === "absolute";
/** What an image is: it decides the camera model of a registration and the gate
 *  thresholds (orthographic sources: turntable frames, orthographic/datasheet
 *  renders; perspective marketing photos; close-ups of a detail). */
export const SOURCE_TYPES = [
  "orthographic_turntable",
  "orthographic_render",
  "datasheet_render",
  "perspective_photo",
  "closeup",
] as const;
export const SourceType = z.enum(SOURCE_TYPES);
const Axis = z.enum(["x", "y", "z"]);
/** An image point bound to the model: a feature's bound centre or an explicit
 *  point in mm. role check: reported by registration and gates, never fitted (a
 *  feature that may be misplaced). */
export const Landmark = z
  .strictObject({
    id: Id.optional(),
    px: z.tuple([finite, finite]),
    feature_id: Id.optional(),
    point_mm: V3.optional(),
    role: z.enum(["fit", "check"]).optional(),
  })
  .refine((l) => !!l.feature_id !== !!l.point_mm, "A landmark needs exactly one of feature_id or point_mm");
/** Stored by cad_reference_register with the camera it fitted. */
export const Registration = z.strictObject({
  method: z.string().max(160),
  revision: Id,
  registered_at: z.iso.datetime(),
  source_type: SourceType,
  registered: z.boolean(),
  reasons: z.array(z.string().max(300)).max(12),
  residuals: z.record(z.string(), finite.nullable()),
  thresholds: z.record(z.string(), finite),
  facing_margin: finite.nullable(),
  handedness: z.strictObject({
    verdict: z.enum(["model_fits_better", "mirror_fits_better", "ambiguous", "not_checked"]),
    margin: finite.nullable(),
  }),
  overlay_artifact_id: Id.nullable(),
});
export const Reference = z.strictObject({
  id: Id,
  artifact_id: Id,
  label: z.string().min(1).max(200),
  source: z.string().max(1000).default("user supplied"),
  camera: Camera.optional(),
  // Pixel scores are only authoritative when an explicit registration is declared.
  registered: z.boolean().default(false),
  source_type: SourceType.optional(),
  landmarks: z.array(Landmark).max(32).optional(),
  /** Normalised [x0,y0,x1,y1] boxes left out of the silhouette (badges, hands, stands). */
  exclusions: z.array(Box2).max(16).optional(),
  /** Alpha or black/white object mask for the image (instead of thresholding). */
  mask_artifact_id: Id.optional(),
  registration: Registration.optional(),
});
/** Starting thresholds of the image gates per source class (lens-quality 3.2);
 *  any field overrides the default of its class. */
export const GateThresholds = z.strictObject({
  iou_min: finite.min(0).max(1).optional(),
  chamfer_mm_max: finite.positive().optional(),
  p95_mm_max: finite.positive().optional(),
  landmark_pct_max: finite.positive().max(100).optional(),
  marking_mm_max: finite.positive().optional(),
  marking_pct_max: finite.positive().max(100).optional(),
  marking_ratio_min: finite.positive().optional(),
  marking_ratio_max: finite.positive().optional(),
  delta_e_max: finite.positive().max(100).optional(),
  visible_fraction_min: finite.min(0).max(1).optional(),
  colour: z.enum(["delta_e", "hue_family"]).optional(),
});
const Source = z.string().min(3).max(300);
/** Declarations cad_gates checks (from the Reference Pack; all optional). */
export const GateSpec = z.strictObject({
  thresholds: z
    .strictObject({
      orthographic: GateThresholds.optional(),
      perspective: GateThresholds.optional(),
      closeup: GateThresholds.optional(),
    })
    .optional(),
  /** Contact graph tolerance (default 0.05 mm). */
  contact_tolerance_mm: finite.positive().max(10).optional(),
  /** Controls (keys, wheels, buttons): >= 200 ray samples on their outward faces
   *  must be unoccluded along direction (default: from the model centre outward). */
  controls: z
    .array(
      z.strictObject({
        id: Id,
        feature_ids: z.array(Id).min(1).max(32),
        direction: V3.optional(),
        samples: z.number().int().min(200).max(2000).optional(),
      }),
    )
    .max(32)
    .optional(),
  /** Optical axes: a ray bundle from origin along direction must not hit geometry
   *  other than glass and the allowed features. */
  optical_axes: z
    .array(
      z.strictObject({
        id: Id,
        origin_mm: V3,
        direction: V3,
        length_mm: finite.positive().max(10000),
        radius_mm: finite.nonnegative().max(1000).optional(),
        allowed_feature_ids: z.array(Id).max(64).optional(),
      }),
    )
    .max(8)
    .optional(),
  /** Station widths: extent along axis of the section at at_axis = at_mm. */
  stations: z
    .array(
      z.strictObject({
        id: Id,
        axis: Axis,
        at_axis: Axis,
        at_mm: finite,
        value_mm: finite.positive().max(1e6),
        tolerance_mm: finite.positive().max(100),
        feature_ids: z.array(Id).max(64).optional(),
        source: Source,
      }),
    )
    .max(32)
    .optional(),
  /** Handedness: the features' centre lies on the sign side of plane_mm (default:
   *  the model's mid-plane) along axis. */
  sides: z
    .array(
      z.strictObject({
        id: Id,
        feature_ids: z.array(Id).min(1).max(32),
        axis: Axis,
        sign: z.union([z.literal(1), z.literal(-1)]),
        plane_mm: finite.optional(),
        source: Source,
      }),
    )
    .max(32)
    .optional(),
  /** Counts: separate items in the features (touching pieces merged); visible:
   *  expected visible items per reference id. */
  counts: z
    .array(
      z.strictObject({
        id: Id,
        label: z.string().min(1).max(80),
        feature_ids: z.array(Id).min(1).max(64),
        expected: z.number().int().min(0).max(1000),
        visible: z.record(z.string(), z.number().int().min(0).max(1000)).optional(),
      }),
    )
    .max(32)
    .optional(),
  /** Markings (logos, prints): source box in a reference image; centroid (mm),
   *  size ratio and visible fraction of the model's marking features. */
  markings: z
    .array(z.strictObject({ id: Id, feature_ids: z.array(Id).min(1).max(32), reference_id: Id, box: Box2 }))
    .max(32)
    .optional(),
  must_be_visible: z
    .array(
      z.strictObject({
        id: Id,
        feature_ids: z.array(Id).min(1).max(32),
        reference_ids: z.array(Id).max(12).optional(),
        min_fraction: finite.min(0).max(1).optional(),
      }),
    )
    .max(32)
    .optional(),
  /** Words the model's name (or purpose) must contain (the product it is). */
  name_terms: z.array(z.string().min(2).max(60)).max(8).optional(),
});
export type GateSpec = z.infer<typeof GateSpec>;
export const CATEGORIES = [
  "silhouette",
  "curvature",
  "placement",
  "continuity",
  "detail",
  "text",
  "material",
  "optics",
] as const;
const InventoryRequirement = z.strictObject({
  id: Id,
  description: z.string().min(5).max(600),
  category: z.enum(CATEGORIES),
  reference_ids: z.array(Id).min(1).max(12),
  required: z.boolean().default(true),
});
/** A frozen ledger item. The optional fields steer the judge (gates_judge):
 *  features whose registered projection locates the item, a declared source
 *  region, and explicit binary questions (else category templates are used). */
export const Requirement = InventoryRequirement.extend({
  feature_ids: z.array(Id).min(1).max(32).optional(),
  region: z
    .strictObject({ reference_id: Id, box: Box2 })
    .optional(),
  questions: z.array(z.string().min(5).max(300)).min(1).max(5).optional(),
});
export const JUDGE_EFFORTS = ["low", "medium", "high", "xhigh", "max"] as const;
/** Final-review judge. provider claude_cli runs `claude -p` (default Opus 5.5);
 *  openrouter keeps the paid OpenRouter route selectable. */
export const JudgeConfig = z.strictObject({
  provider: z.enum(["claude_cli", "openrouter"]),
  model: z.string().regex(/^[a-zA-Z0-9._:\/-]{3,100}$/),
  effort: z.enum(JUDGE_EFFORTS),
  max_parallel: z.number().int().min(1).max(3),
  pairwise: z.boolean(),
  /** Budget of the judge calls of one review run (reported CLI/provider cost). */
  max_cost_usd: finite.positive().max(50),
});
export const Judge = z.strictObject({
  provider: JudgeConfig.shape.provider.default("claude_cli"),
  model: JudgeConfig.shape.model.default("claude-opus-5-5"),
  effort: JudgeConfig.shape.effort.default("xhigh"),
  max_parallel: JudgeConfig.shape.max_parallel.default(2),
  pairwise: JudgeConfig.shape.pairwise.default(true),
  max_cost_usd: JudgeConfig.shape.max_cost_usd.default(5),
});
/** gates_judge (default): deterministic cad_gates floor on the reviewed
 *  revision plus one advisory judge whose critical findings must be confirmed
 *  deterministically or accepted as assumptions. two_vlm: the earlier rule, two
 *  independent OpenRouter models must each match every required item. */
export const ACCEPTANCE = ["gates_judge", "two_vlm"] as const;
export const AcceptedAssumption = z.strictObject({
  requirement_id: Id,
  note: z.string().min(5).max(500),
});
export const Dimension = z.strictObject({
  id: Id,
  feature_id: Id.optional(),
  axis: z.enum(["x", "y", "z"]),
  value_mm: finite.positive().max(1e6),
  tolerance_mm: finite.positive().max(100),
  source: z.string().min(5).max(500),
});
export const Contact = z.strictObject({
  id: Id,
  feature_id: Id,
  other_feature_id: Id,
  maximum_gap_mm: finite.nonnegative().max(100),
});
export const Setup = z.strictObject({
  model_id: Id,
  base_revision: Id,
  idempotency_key: z.string().min(16).max(128),
  expected_contract_hash: Digest.nullable(),
  references: z.array(Reference).min(1).max(12),
  requirements: z.array(Requirement).max(128).default([]),
  dimensions: z.array(Dimension).max(32).default([]),
  contacts: z.array(Contact).max(32).default([]),
  models: z
    .array(z.string().regex(/^[a-zA-Z0-9._-]+\/[a-zA-Z0-9._:-]+$/))
    .min(1)
    .max(3)
    .default(["qwen/qwen3-vl-32b-instruct", "google/gemini-2.5-flash"]),
  maximum_cost_usd: finite.positive().max(20).default(0.5),
  minimum_confidence: finite.min(0.5).max(0.99).default(0.85),
  image_pixel_limit: z.number().int().min(512).max(2576).default(1400),
  silhouette_iou_min: finite.min(0.8).max(1).default(0.97),
  edge_f1_min: finite.min(0.8).max(1).default(0.94),
  scope: z
    .string()
    .min(10)
    .max(1000)
    .default(
      "Visible exterior features supported by the supplied images. Hidden geometry and physical surface accuracy are not certified.",
    ),
  acceptance: z.enum(ACCEPTANCE).default("gates_judge"),
  judge: Judge.default({
    provider: "claude_cli",
    model: "claude-opus-5-5",
    effort: "xhigh",
    max_parallel: 2,
    pairwise: true,
    max_cost_usd: 5,
  }),
  /** Judge findings the user/agent explicitly accepts as assumptions (by item). */
  accepted_assumptions: z.array(AcceptedAssumption).max(64).default([]),
  /** Declared gate checks and threshold overrides for cad_gates. */
  gates: GateSpec.optional(),
});
export const InspectionView = z.strictObject({
  id: Id.refine((id) => id.length <= 80, "View ID must be at most 80 characters"),
  label: z.string().min(1).max(200),
  camera: Camera,
  width: z.number().int().min(128).max(2000).optional(),
  height: z.number().int().min(128).max(2000).optional(),
});
export const Start = z.strictObject({
  model_id: Id,
  revision: Id,
  contract_hash: Digest,
  phase: z.enum(["inventory", "compare", "inspect"]).default("compare"),
  views: z.array(InspectionView).min(1).max(3).optional(),
  idempotency_key: z.string().min(16).max(128),
}).refine((a) => !a.views || a.phase === "inspect", {
  message: "Explicit views are only supported for inspect", path: ["views"],
}).refine((a) => !a.views || new Set(a.views.map((v) => v.id)).size === a.views.length, {
  message: "View IDs must be unique", path: ["views"],
});
export const ReportQuery = z.strictObject({
  model_id: Id,
  revision: Id.optional(),
  run_id: Id.optional(),
  offset: z.number().int().min(0).default(0),
  limit: z.number().int().min(1).max(3).default(3),
});
export const Contract = Setup.omit({
  base_revision: true,
  idempotency_key: true,
  expected_contract_hash: true,
}).extend({
  schema_version: z.literal("1"),
  hash: Digest,
  reference_hashes: z.record(z.string(), Digest),
  created_at: z.iso.datetime(),
  inventory_runs: z.array(Id).default([]),
  // Contracts frozen before the acceptance policy existed keep their rule:
  // a missing acceptance reads as two_vlm (see acceptanceOf in evidence.ts).
  acceptance: z.enum(ACCEPTANCE).optional(),
  judge: JudgeConfig.optional(),
  accepted_assumptions: z.array(AcceptedAssumption).max(64).optional(),
});
export type Contract = z.infer<typeof Contract>;
export type Requirement = z.infer<typeof Requirement>;
export type Camera = z.infer<typeof Camera>;
export const Finding = z.strictObject({
  requirement_id: Id,
  reference_support: z.enum(["supported", "contradicted", "uncertain"]),
  verdict: z.enum(["match", "mismatch", "not_visible", "uncertain"]),
  confidence: finite.min(0).max(1),
  evidence: z.string().min(1).max(1500),
  reference_id: Id,
  render_id: Id.nullable(),
  reference_region: Box2.nullable(),
  render_region: Box2.nullable(),
  correction: z.string().max(1000),
});
export const VisionComparison = z.strictObject({
  findings: z.array(Finding).max(128),
  additional_issues: z
    .array(
      z.strictObject({
        description: z.string().min(5).max(600),
        category: z.enum(CATEGORIES),
        reference_id: Id,
        render_id: Id.nullable(),
      }),
    )
    .max(32),
  limitations: z.array(z.string().max(500)).max(32),
});
/** Constrain provider output to the actual batch and supplied image IDs. */
export function comparisonSchema(requirementIds: string[], referenceIds: string[], renderIds: string[]) {
  const references = z.enum(referenceIds as [string, ...string[]]);
  const renders = z.enum(renderIds as [string, ...string[]]).nullable();
  return VisionComparison.extend({
    findings: z.array(Finding.extend({
      requirement_id: z.enum(requirementIds as [string, ...string[]]),
      reference_id: references,
      render_id: renders,
    })).max(128),
    additional_issues: z.array(VisionComparison.shape.additional_issues.element.extend({
      reference_id: references,
      render_id: renders,
    })).max(32),
  });
}
export const VisionInventory = z.strictObject({
  requirements: z.array(InventoryRequirement).min(1).max(128),
  limitations: z.array(z.string().max(500)).max(32),
});
export const Summary = z.strictObject({
  status: z.enum([
    "not_configured",
    "not_checked",
    "running",
    "matches_declared_visible_checks",
    "needs_correction",
    "insufficient_evidence",
    "stale",
    "failed",
  ]),
  contract_hash: Digest.nullable(),
  run_id: Id.nullable(),
  required: z.number().int().nonnegative(),
  matched: z.number().int().nonnegative(),
  mismatched: z.number().int().nonnegative(),
  unknown: z.number().int().nonnegative(),
  exact_replica_certified: z.literal(false),
  scope: z.string().max(1000),
  report_artifact_id: Id.nullable(),
  /** Rule that produced the status (absent on legacy runs = two_vlm). */
  acceptance: z.enum(ACCEPTANCE).optional(),
  /** gates_judge: cad_gates floor on the reviewed revision (null = not available). */
  floor_passed: z.boolean().nullable().optional(),
  /** gates_judge: critical judge findings neither confirmed nor accepted. */
  open_findings: z.number().int().nonnegative().optional(),
});
export type Summary = z.infer<typeof Summary>;
export const BASE_SCOPE =
  "Only declared visible features; hidden geometry and physical accuracy are not certified.";

export const SetupResult = z.strictObject({
  model_id: Id,
  contract_hash: Digest,
  reference: Summary,
});
export const StartResult = z.strictObject({
  status: z.enum(["ok", "queued"]),
  model_id: Id,
  revision: Id,
  run_id: Id,
  state: z.enum(["queued", "completed"]),
  contract_hash: Digest,
  cached: z.boolean(),
});
export const NativeCheck = z.strictObject({
  id: Id,
  status: z.enum(["passed", "failed", "unknown"]),
  method: z.string(),
  measured: z.union([z.number(), z.boolean(), z.null()]),
  expected: z.union([z.number(), z.boolean()]),
  unit: z.string(),
  reason: z.string(),
});
export const ReviewUsage = z.strictObject({
  model: z.string(),
  served_model: z.string(),
  request_id: z.string(),
  provider: z.string().nullable(),
  cost_usd: z.number().nonnegative(),
  input_tokens: z.number().nonnegative(),
  output_tokens: z.number().nonnegative(),
  input_hash: Digest,
  elapsed_ms: z.number().nonnegative(),
});
export const ReviewImageInfo = z.strictObject({
  id: Id,
  label: z.string(),
  artifact_id: Id,
  kind: z.enum(["reference", "render", "difference"]),
});
export const ReviewedFinding = z.strictObject({
  id: Id,
  description: z.string(),
  category: z.enum(CATEGORIES),
  required: z.boolean(),
  verdict: z.enum(["match", "mismatch", "uncertain"]),
  observations: z.array(Finding.extend({ model: z.string() })),
});
export const JudgeAnswer = z.strictObject({
  question: z.string().max(300),
  answer: z.enum(["yes", "no", "cannot_tell"]),
  /** Region in the render crop, normalized [x0,y0,x1,y1] (null if not located). */
  region: Box2.nullable(),
});
/** One advisory judge result per ledger item and view. A candidate defect is
 *  never a pass condition by itself: it is confirmed by a deterministic probe
 *  (then it blocks), accepted as an assumption, or stays open. */
export const JudgeFinding = z.strictObject({
  requirement_id: Id,
  view_id: Id,
  outcome: z.enum(["match", "candidate_defect", "uncertain"]),
  answers: z.array(JudgeAnswer).max(6),
  largest_difference: z.string().max(600),
  region: Box2.nullable(),
  region_check: z.enum(["consistent", "inconsistent", "not_checked"]),
  confirmation: z.enum(["confirmed", "unconfirmed", "refuted", "not_needed"]),
  confirmed_by: z.array(z.string().max(200)).max(8),
  critical: z.boolean(),
  accepted: z.boolean(),
});
export const JudgePairwise = z.strictObject({
  requirement_id: Id,
  view_id: Id,
  previous_revision: Id,
  preferred: z.enum(["current", "previous", "tie", "inconsistent"]),
});
export const JudgeIssue = z.strictObject({
  view_id: Id,
  description: z.string().max(600),
  severity: z.enum(["critical", "minor"]),
  region: Box2.nullable(),
  confirmation: z.enum(["confirmed", "unconfirmed", "refuted", "not_needed"]),
});
export const JudgeSummary = z.strictObject({
  provider: z.string(),
  model: z.string(),
  effort: z.string(),
  calls: z.number().int().nonnegative(),
  retries: z.number().int().nonnegative(),
  failed_calls: z.number().int().nonnegative(),
  reused_calls: z.number().int().nonnegative(),
  elapsed_ms: z.number().nonnegative(),
  total_findings: z.number().int().nonnegative(),
  open_critical: z.number().int().nonnegative(),
  confirmed_critical: z.number().int().nonnegative(),
});
export const GatesDigest = z.strictObject({
  source: z.enum(["cad_gates", "unavailable"]),
  floor_passed: z.boolean().nullable(),
  residual_count: z.number().int().nonnegative(),
  top: z.array(z.record(z.string(), z.unknown())).max(5),
  error: z.string().max(300).nullable(),
});
export const Assumption = z.strictObject({
  requirement_id: Id.nullable(),
  view_id: Id.nullable(),
  text: z.string().max(700),
  state: z.enum(["open", "accepted", "informational"]),
});
export const ReportResult = z.strictObject({
  model_id: Id,
  revision: Id,
  reference: Summary,
  checked_summary: Summary.nullable(),
  report_artifact_id: Id.nullable(),
  run_id: Id.nullable(),
  state: z.enum(["queued", "running", "completed", "failed"]).nullable(),
  phase: z.enum(["inventory", "compare", "inspect"]).nullable(),
  views: z.array(InspectionView).nullable(),
  elapsed_ms: z.number().nonnegative().nullable(),
  stage: z.string().nullable(),
  checked_revision: Id.nullable(),
  contract_hash: Digest.nullable(),
  references: z.array(Reference),
  dimensions: z.array(Dimension),
  contacts: z.array(Contact),
  configuration: Setup.pick({
    models: true,
    maximum_cost_usd: true,
    minimum_confidence: true,
    image_pixel_limit: true,
    silhouette_iou_min: true,
    edge_f1_min: true,
    scope: true,
  })
    .extend({
      acceptance: z.enum(ACCEPTANCE).optional(),
      judge: JudgeConfig.optional(),
      accepted_assumptions: z.array(AcceptedAssumption).optional(),
    })
    .nullable(),
  requirements: z.array(Requirement),
  findings: z.array(ReviewedFinding),
  additional_issues: z.array(
    VisionComparison.shape.additional_issues.element.extend({
      model: z.string(),
    }),
  ),
  native_checks: z.array(NativeCheck),
  images: z.array(ReviewImageInfo),
  usage: z.array(ReviewUsage),
  limitations: z.array(z.string()),
  error: z
    .object({
      code: z.string(),
      message: z.string(),
      details: z.record(z.string(), z.unknown()),
    })
    .nullable(),
  next_offset: z.number().int().nonnegative().nullable(),
  vision_configured: z.boolean(),
  total_items: z.number().int().nonnegative(),
  /** gates_judge runs only (paged like findings). */
  gates: GatesDigest.nullable().optional(),
  judge: JudgeSummary.nullable().optional(),
  judge_findings: z.array(JudgeFinding).optional(),
  judge_issues: z.array(JudgeIssue).optional(),
  pairwise: z.array(JudgePairwise).optional(),
  assumptions: z.array(Assumption).optional(),
});
