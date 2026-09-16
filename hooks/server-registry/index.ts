import { z } from "zod";
import { CadError, requireThat } from "../../packages/semantic-ir/errors.js";
/** Operator-owned mandatory pipeline gates (Bauplan 12.2). Names are internal server events. */
export const MANDATORY = [
  "before_request",
  "before_import",
  "before_compile",
  "after_compile",
  "before_resolve",
  "after_resolve",
  "before_execute",
  "after_execute",
  "before_validate",
  "after_validate",
  "before_commit",
  "after_commit",
  "before_export",
  "after_export",
  "before_publish",
  "on_failure",
  "on_cancel",
] as const;
export type Hook = (typeof MANDATORY)[number];
const id = z.string().min(1).max(128);
const digest = z.string().regex(/^[a-f0-9]{64}$/);
/** Each hook receives only the immutable minimum it needs; unknown keys are rejected. */
export const HOOK_CONTEXTS: Record<Hook, z.ZodType> = {
  before_request: z.strictObject({ trace_id: id, tool: id }),
  before_import: z.strictObject({ model_id: id, artifact_id: id }),
  before_compile: z.strictObject({ model_id: id }),
  after_compile: z.strictObject({ model_id: id, registry_hash: digest }),
  before_resolve: z.strictObject({ model_id: id, revision: id }),
  after_resolve: z.strictObject({ feature_id: id, revision: id }),
  before_execute: z.strictObject({ job_id: id, input_hash: digest }),
  after_execute: z.strictObject({
    job_id: id,
    engine_build: z.string().max(200),
  }),
  before_validate: z.strictObject({ transaction_id: id }),
  after_validate: z.strictObject({ transaction_id: id, digest }),
  before_commit: z.strictObject({ transaction_id: id }),
  after_commit: z.strictObject({ model_id: id, revision: id }),
  before_export: z.strictObject({
    model_id: id,
    revision: id,
    format: z.string().max(16),
  }),
  after_export: z.strictObject({
    artifact_id: id.optional(),
    job_id: id.optional(),
    artifact_count: z.number().int().nonnegative(),
  }),
  before_publish: z.strictObject({
    model_id: id,
    revision: id,
    package_hash: digest,
    recipient: id,
    approval_request_id: id,
    action_digest: digest,
  }),
  on_failure: z.strictObject({
    job_id: id,
    code: z.string().max(64),
    transaction_id: id.nullable(),
  }),
  on_cancel: z.strictObject({ job_id: id, transaction_id: id.nullable() }),
};
export type HookOutput =
  | { decision: "allow" }
  | { decision: "require_approval"; action_digest: string };
/** Static operator-owned gates. Never loaded from model data or request arguments. */
export class Gates {
  private enabled = new Set<string>(MANDATORY);
  constructor(
    private recorder: (event: string, data: Record<string, unknown>) => void,
  ) {}
  /** Run a mandatory synchronous gate; a failing check throws and the state change is blocked. */
  run(
    name: Hook,
    context: Record<string, unknown>,
    check: () => void = () => {},
  ) {
    requireThat(
      this.enabled.has(name),
      "POLICY_GATE_FAILED",
      `Pflichtprüfung ${name} fehlt.`,
    );
    const parsed = HOOK_CONTEXTS[name].safeParse(context);
    requireThat(
      parsed.success,
      "POLICY_GATE_FAILED",
      `Pflichtprüfung ${name} erhielt einen unzulässigen Kontext.`,
    );
    const frozen = Object.freeze({
      ...(parsed.data as Record<string, unknown>),
      event: name,
    });
    const start = performance.now();
    check();
    requireThat(
      performance.now() - start < 1000,
      "POLICY_GATE_FAILED",
      "Pflichtprüfung hat das Zeitbudget überschritten.",
    );
    this.recorder(name, frozen);
    return { decision: "allow" } as HookOutput;
  }
  /** Like run, but a NEEDS_APPROVAL failure becomes an explicit require_approval decision bound to its digest. */
  decide(
    name: Hook,
    context: Record<string, unknown>,
    check: () => void,
  ): HookOutput {
    try {
      return this.run(name, context, check);
    } catch (error) {
      if (
        error instanceof CadError &&
        error.code === "NEEDS_APPROVAL" &&
        typeof error.details.action_digest === "string"
      )
        return {
          decision: "require_approval",
          action_digest: error.details.action_digest,
        };
      throw error;
    }
  }
  /** Operator fault injection for fail-closed tests, inaccessible through MCP. */
  disableForTest(name: Hook) {
    this.enabled.delete(name);
  }
}
/** Effective pipeline policy document (Bauplan 12.4), generated from code rather than YAML. */
export const PIPELINE_POLICY = {
  version: 2,
  mandatory_gates: MANDATORY,
  gate_contexts: Object.fromEntries(
    MANDATORY.map((name) => [
      name,
      Object.keys((HOOK_CONTEXTS[name] as any).shape ?? {}),
    ]),
  ),
  defaults: {
    mandatory_gate_failure: "deny",
    unknown_operator: "deny",
    ambiguous_selection: "deny",
    silent_tolerance_increase: "deny",
    arbitrary_code_execution: "deny",
    dynamic_hook_loading: "deny",
    external_publication_without_bound_approval: "deny",
  },
  repair_policy: {
    max_candidate_retries: 3,
    preserve_required_dimensions: true,
    require_new_candidate_hash: true,
    allowed_automatic_repairs: ["re_tessellation_of_derived_previews"],
  },
  gate_time_budget_ms: 1000,
};
