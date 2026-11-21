import { z } from "zod";
import { Id } from "../semantic-ir/identifiers.js";

export const EFFORT_LEVELS = ["low", "medium", "high", "xhigh", "max"] as const;
const Effort = z.enum(EFFORT_LEVELS);
/** Task kinds; the static effort table (router.ts) maps each to a default effort. */
export const TASK_KINDS = [
  "geometry",
  "edge",
  "colour",
  "marking",
  "optics",
  "curvature",
] as const;
/** Model ids go to the worker CLI as one argv entry: no flags, no spaces. */
export const WorkerModel = z
  .string()
  .regex(/^(agy:)?[a-z0-9][a-z0-9._-]{1,63}(\[1m\])?$/)
  .describe(
    "Worker model id, e.g. claude-opus-5-5 (default for Claude orchestrators), claude-fable-5-1, claude-sonnet-5, gpt-6-astra (default for GPT/Codex), agy:gemini-4-neon (default for Antigravity orchestrators; any agy:<model> runs through the agy CLI).",
  );
const WaitMs = z.int().min(0).max(110000).optional();

export const FeatureTask = z.strictObject({
  id: z.string().regex(/^[a-z][a-z0-9-]{0,39}$/),
  title: z.string().min(3).max(120),
  description: z.string().min(10).max(2000),
  kind: z.enum(TASK_KINDS),
  owned_feature_ids: z.array(Id).max(64).default([]),
  context_feature_ids: z.array(Id).max(64).default([]),
  /** lanes only: parts whose outputs this lane may change (its own features only). */
  owned_part_ids: z.array(Id).max(16).optional(),
  reference_ids: z.array(Id).min(1).max(4),
  render_ids: z.array(Id).min(1).max(3),
  evidence: z.string().min(10).max(2000),
  /** Explicit effort for this task; wins over worker.effort and the static table. */
  effort: Effort.optional(),
});
export const WorkerConfig = z.strictObject({
  /** Explicit worker model; default by orchestrator (claude-opus-5-5 | gpt-6-astra | agy:gemini-4-neon). */
  model: WorkerModel.optional(),
  /** Needed only when the model id does not name its provider. */
  provider: z.enum(["claude", "codex", "agy"]).optional(),
  /** auto (default): static table per task kind, Jev may pick a neighbour level. */
  effort: z.union([Effort, z.literal("auto")]).optional(),
  /** Per-kind overrides of the static effort table. */
  effort_by_kind: z
    .strictObject(
      Object.fromEntries(TASK_KINDS.map((kind) => [kind, Effort.optional()])) as Record<
        (typeof TASK_KINDS)[number],
        z.ZodOptional<typeof Effort>
      >,
    )
    .optional(),
  /** false: never ask Jev (static table only). Default: Jev when available, table on any failure. */
  jev: z.boolean().optional(),
});
export const BatchStart = z.strictObject({
  model_id: Id,
  base_revision: Id,
  idempotency_key: z.string().min(16).max(128),
  orchestrator_model: z.string().min(3).max(100),
  inspection_run_id: Id,
  mvp: z.strictObject({
    silhouette: z.boolean(),
    proportions: z.boolean(),
    dominant_colours: z.boolean(),
    main_components: z.boolean(),
    orientation: z.boolean(),
    evidence: z.string().min(20).max(1500),
  }),
  tasks: z.array(FeatureTask).min(1).max(8),
  /** Per worker (default 600 s). */
  timeout_seconds: z.number().int().min(30).max(1800).default(600),
  /** Concurrent workers (default 2; each Claude/Codex CLI needs about 0.4 GB). */
  max_parallel: z.int().min(1).max(4).default(2),
  worker: WorkerConfig.optional(),
  /** worker_candidate: each worker builds and validates its own candidate through the
   *  llcad MCP (designer grant: edit, never commit) and returns a validated patch plus
   *  before/after crops. none: read-only proposal workers. Default: worker_candidate
   *  when the service knows its MCP URL, else none. */
  validation: z.enum(["worker_candidate", "none"]).optional(),
  /** Opt-in parallel build lanes after a committed skeleton: a task may add parts/frames
   *  in its namespace (pt-<id>-, fr-<id>-) and change the outputs of owned_part_ids. */
  lanes: z.boolean().default(false),
});
export const BatchReport = z.strictObject({
  model_id: Id,
  batch_id: Id,
  /** Wait server-side until the batch is terminal (at most 110 s); running -> call again. */
  wait_ms: WaitMs,
});
export const BatchStarted = z.strictObject({
  model_id: Id,
  base_revision: Id,
  batch_id: Id,
  status: z.literal("queued"),
  max_parallel: z.int().min(1).max(4),
  validation: z.enum(["worker_candidate", "none"]).optional(),
  lanes: z.boolean().optional(),
  warnings: z.array(z.string()).optional(),
});
const TaskValidation = z.strictObject({
  status: z.enum(["passed", "failed", "not_validated", "mismatch", "not_requested"]),
  transaction_id: Id.nullable().optional(),
  candidate_revision: Id.nullable().optional(),
  digest: z.string().nullable().optional(),
  failed_checks: z.array(z.string()).optional(),
  note: z.string().optional(),
});
const Crop = z.strictObject({
  view: z.string(),
  before_artifact_id: Id,
  after_artifact_id: Id,
  pair_artifact_id: Id.optional(),
  box_px: z.array(z.number()).optional(),
  changed_fraction: z.number().optional(),
});
const Wave = z.strictObject({
  wave: z.int().min(1),
  task_ids: z.array(z.string()),
  operations: z.int().min(0),
  patch_artifact_id: Id,
  /** Current head still leaves every task of this wave untouched since the frozen base. */
  applicable: z.boolean(),
  conflicts: z.array(z.string()).optional(),
});
export const BatchResult = z.strictObject({
  model_id: Id,
  base_revision: Id,
  batch_id: Id,
  status: z.enum(["queued", "running", "completed", "failed", "stale", "interrupted"]),
  stage: z.string(),
  max_parallel: z.int().min(1).max(4),
  mvp_check_coverage: z.number().min(0).max(1),
  coverage_basis: z.literal("orchestrator_declared_coarse_checks_not_visual_accuracy"),
  elapsed_ms: z.number().nonnegative(),
  proposal_artifact_id: Id.nullable(),
  tasks: z.array(
    z.strictObject({
      id: Id,
      title: z.string(),
      state: z.enum(["queued", "routing", "running", "completed", "failed"]),
      model: z.string().nullable(),
      effort: z.string().nullable(),
      effort_source: z.enum(["task", "worker", "jev", "table"]).optional(),
      summary: z.string().nullable(),
      proposal_artifact_id: Id.nullable(),
      operations: z.int().min(0).optional(),
      validation: TaskValidation.optional(),
      crops: z.array(Crop).optional(),
      elapsed_ms: z.number().nonnegative().nullable(),
      error: z.string().nullable(),
    }),
  ),
  validation: z.enum(["worker_candidate", "none"]).optional(),
  lanes: z.boolean().optional(),
  /** Apply by reference: cad_apply_patch {patch_ref: {artifact_id: patch_artifact_id}, ...}
   *  wave by wave (each <= 64 operations = one transaction). */
  waves: z.array(Wave).optional(),
  next_action: z.string().optional(),
  error: z.string().nullable(),
  applied: z.literal(false),
});
