import { z } from "zod";
import { Id } from "../semantic-ir/identifiers.js";

/* cad_fit_parameters contracts. Kept apart from semantic-ir/schema.ts (which
 * registers them) so the fitting package owns its shapes. */
const finite = z.number().finite();
const Name = z.string().regex(/^[a-z][a-z0-9_]{0,63}$/);
const Box = z.tuple([
  finite.min(0).max(1),
  finite.min(0).max(1),
  finite.min(0).max(1),
  finite.min(0).max(1),
]);
export const FitVariable = z.strictObject({
  /** A model variable (ModelIR.variables, set_variable); its current unit is used. */
  name: Name,
  min: finite,
  max: finite,
  /** Finite-difference step of the stand-in (default 8 % of max - min). */
  step: finite.positive().optional(),
});
export const FitObjective = z.strictObject({
  /** Weight of (1 - silhouette IoU). */
  silhouette_weight: finite.min(0).max(100).default(1),
  /** Weight per mm of mean symmetric edge (chamfer) distance. */
  chamfer_weight: finite.min(0).max(100).default(0.05),
  /** Weight per mm of mean landmark residual. */
  landmark_weight: finite.min(0).max(100).default(0.05),
  /** Image landmarks (pixels of the reference image) bound to a feature's bound centre. */
  landmarks: z
    .array(z.strictObject({ reference_id: Id, px: z.tuple([finite, finite]), feature_id: Id }))
    .max(32)
    .default([]),
  /** Normalized boxes excluded from a reference silhouette (badges, hands, stands). */
  exclusions: z.array(z.strictObject({ reference_id: Id, box: Box })).max(16).default([]),
});
export const FitParameters = z
  .strictObject({
    model_id: Id,
    /** Base revision (default and only allowed value: the head). */
    revision: Id.optional(),
    /** Poll a running fit instead of starting one. */
    fit_id: Id.optional(),
    variables: z.array(FitVariable).min(1).max(12).optional(),
    /** Reference ids of the model's reference contract: registered, absolute cameras. */
    views: z.array(Id).min(1).max(6).optional(),
    objective: FitObjective.optional(),
    max_evals: z.number().int().min(20).max(3000).default(400),
    /** Tier 3: build the optimum as a real candidate and measure it (default true). */
    confirm: z.boolean().default(true),
    /** Long edge of the stand-in images in pixels. */
    resolution_px: z.number().int().min(160).max(1600).default(480),
    /** Wait up to this long for the result; poll later with fit_id. */
    wait_ms: z.number().int().min(0).max(110000).default(100000),
  })
  .refine((a) => !!a.fit_id || (!!a.variables && !!a.views), {
    message: "variables and views are required to start a fit; fit_id polls one",
  });

const Metrics = z.strictObject({
  objective: z.number(),
  views: z.array(
    z.strictObject({
      reference_id: Id,
      iou: z.number(),
      chamfer_mm: z.number(),
      landmark_mm: z.number().nullable(),
    }),
  ),
});
const QuantityOut = z.strictObject({ value: z.string(), unit: z.string() });
export const FitResult = z.strictObject({
  status: z.enum(["running", "succeeded"]),
  model_id: Id,
  revision: Id,
  fit_id: Id,
  stage: z.string(),
  cached: z.boolean(),
  variables: z.array(
    z.strictObject({
      name: Name,
      unit: z.string(),
      min: z.number(),
      max: z.number(),
      before: z.number(),
      after: z.number().nullable(),
    }),
  ),
  /** Proposal only: apply with cad_apply_patch, then validate and commit as usual. */
  patch: z
    .strictObject({
      operations: z.array(
        z.strictObject({
          op: z.literal("set_variable"),
          name: Name,
          expected: QuantityOut,
          value: QuantityOut,
        }),
      ),
    })
    .nullable(),
  metrics: z.strictObject({
    before: Metrics.nullable(),
    standin_after: Metrics.nullable(),
    confirmed_after: Metrics.nullable(),
  }),
  tiers: z.strictObject({
    samples: z.number().int().nonnegative(),
    standin: z
      .strictObject({
        method: z.string(),
        evals: z.number().int().nonnegative(),
        seconds: z.number().nonnegative(),
        extrapolation_steps: z.number(),
      })
      .nullable(),
    confirmation: z
      .strictObject({
        candidate_revision: Id.nullable(),
        objective: z.number().nullable(),
        agrees_with_standin: z.boolean().nullable(),
      })
      .nullable(),
    iterations: z.number().int().nonnegative(),
  }),
  elapsed_ms: z.number().nonnegative(),
  committed: z.literal(false),
});
