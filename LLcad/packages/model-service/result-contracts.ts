import { z } from "zod";
import { CadError, requireThat, safeError } from "../semantic-ir/errors.js";
import { hash } from "../semantic-ir/hash.js";
import { ToolName } from "../semantic-ir/tool-registry.js";
import {
  FailureResponse,
  JobKind,
  JobResultSchemas,
  JobView,
  LegacyExportResult,
  ToolOutputSchemas,
  ValidationReport,
} from "../semantic-ir/results.js";
import { LIMITS } from "../compiler/index.js";

function invalid(contract: string): never {
  // Never include rejected values, native paths or user-controlled property names.
  throw new CadError(
    "OUTPUT_CONTRACT_VIOLATION",
    "The result does not satisfy the registered response contract.",
    { contract },
  );
}
function jsonValues(value: unknown, contract: string) {
  const ancestors = new Set<object>();
  let nodes = 0;
  const visit = (v: unknown, depth: number) => {
    if (++nodes > 1_000_000 || depth > 64) invalid(contract);
    if (v === null || typeof v === "string" || typeof v === "boolean") return;
    if (typeof v === "number") {
      if (!Number.isFinite(v)) invalid(contract);
      return;
    }
    if (typeof v !== "object" || ancestors.has(v)) invalid(contract);
    if (
      !Array.isArray(v) &&
      Object.getPrototypeOf(v) !== Object.prototype &&
      Object.getPrototypeOf(v) !== null
    )
      invalid(contract);
    ancestors.add(v);
    if (Array.isArray(v)) for (const item of v) visit(item, depth + 1);
    else
      for (const item of Object.values(v))
        if (item !== undefined) visit(item, depth + 1);
    ancestors.delete(v);
  };
  visit(value, 0);
}
export function assertResult(
  schema: z.ZodType,
  value: unknown,
  contract: string,
  maxBytes = LIMITS.response_bytes,
) {
  jsonValues(value, contract);
  requireThat(
    Buffer.byteLength(JSON.stringify(value)) <= maxBytes,
    "BUDGET_EXCEEDED",
    "The response exceeds the budget. Use a narrower selection or a resource fetch.",
  );
  const checked = schema.safeParse(value);
  if (!checked.success || hash(checked.data) !== hash(value)) invalid(contract);
  // Validation is an assertion: do not coerce, strip, fill defaults or replace the
  // value whose digest/idempotency identity was established by the producer.
}
export function assertValidation(
  value: any,
  binding?: {
    candidate?: string;
    ir_hash: string;
    facts: Record<string, unknown>;
  },
) {
  assertResult(ValidationReport, value, "validation", LIMITS.request_bytes * 8);
  const { digest, ...body } = value;
  requireThat(
    digest === hash(body),
    "INTEGRITY_FAILURE",
    "The validation report and its digest do not match.",
  );
  if (
    (value.status === "checks_passed_within_profile") !==
    value.checks.every((c: any) => c.status === "passed")
  )
    invalid("validation_status");
  if (
    binding &&
    ((binding.candidate && value.candidate_revision !== binding.candidate) ||
      value.ir_hash !== binding.ir_hash ||
      value.geometry_digest !== hash(binding.facts))
  )
    invalid("validation_binding");
  if (value.schema_version === "2")
    for (const check of value.checks) {
      if (
        check.revision !== value.candidate_revision ||
        check.engine_build !== value.engine_build
      )
        invalid("check_binding");
      if (
        binding &&
        check.source_geometry_hash !==
          hash(binding.facts[check.target] ?? binding.facts)
      )
        invalid("check_geometry");
    }
}
export function assertJobResult(kind: string, value: any, stored = false) {
  if (!Object.hasOwn(JobResultSchemas, kind)) invalid("job_kind");
  assertResult(
    stored && kind === "export" && value?.result_schema_version === undefined
      ? LegacyExportResult
      : JobResultSchemas[kind as JobKind],
    value,
    "job_" + kind,
    kind === "validate"
      ? LIMITS.request_bytes * 8
      : LIMITS.response_bytes - 2048,
  );
  if (kind === "validate") assertValidation(value);
}
export function assertJobView(value: any, kind: string) {
  assertResult(JobView, value, "job_view", LIMITS.request_bytes * 8);
  if (value.status === "succeeded") {
    if (value.result === null || value.error !== null) invalid("job_state");
    if (
      (value.result.model_id !== undefined &&
        value.result.model_id !== value.model_id) ||
      (value.result.transaction_id !== undefined &&
        value.result.transaction_id !== value.transaction_id)
    )
      invalid("job_binding");
    assertJobResult(kind, value.result, true);
  } else if (value.status === "failed") {
    if (value.error === null || value.result !== null) invalid("job_state");
  } else if (value.result !== null || value.error !== null)
    invalid("job_state");
}
const defaults = () => ({
  result_schema_version: "1",
  model_id: null,
  revision: null,
  candidate_revision: null,
  transaction_id: null,
  job_id: null,
  measurements: null,
  checks: [],
  warnings: [],
  errors: [],
  assumptions: [],
  artifacts: [],
  recommended_next_actions: [],
});
/**
 * Answers leave out what carries no information: envelope fields the tool's payload did
 * not set and that are still null or empty, and the trace id. A field the payload set
 * itself (even to null) stays, so every answer still satisfies its declared contract.
 */
export function compactResponse(response: any, payload: any) {
  const out = { ...response };
  delete out.trace_id;
  for (const [key, value] of Object.entries(defaults())) {
    if (payload && typeof payload === "object" && Object.hasOwn(payload, key))
      continue;
    const current = out[key];
    if (
      current === null ||
      (Array.isArray(current) && current.length === 0 && Array.isArray(value))
    )
      delete out[key];
  }
  return out;
}
export function checkedToolResponse(
  tool: ToolName,
  result: any,
  trace: string,
) {
  const response = {
    status: "ok",
    ...defaults(),
    ...result,
    trace_id: trace,
    ...(result?.status === "failed" && result?.error
      ? { errors: [result.error] }
      : {}),
  };
  // The compact form is what callers get. Should a contract still require a field only
  // the envelope defaults fill, the complete form is checked and returned instead.
  const compact = compactResponse(response, result);
  if (ToolOutputSchemas[tool].safeParse(compact).success) {
    assertResult(ToolOutputSchemas[tool], compact, tool);
    return compact;
  }
  assertResult(ToolOutputSchemas[tool], response, tool);
  const { trace_id, ...rest } = response;
  return rest;
}
/** Ever smaller forms of an error detail, for the rare payload that would not fit. */
function* shrinking(detail: any) {
  yield detail;
  const issues = detail?.details?.issues;
  if (Array.isArray(issues))
    for (const n of [16, 8, 3])
      yield {
        ...detail,
        details: {
          ...detail.details,
          issues: issues.slice(0, n).map((i: any) => ({
            ...i,
            ...(Array.isArray(i.options) ? { options: i.options.slice(0, 24) } : {}),
            ...(Array.isArray(i.allowed_keys)
              ? { allowed_keys: i.allowed_keys.slice(0, 24) }
              : {}),
          })),
        },
      };
  yield { ...detail, details: {} };
}
export function failureResponse(
  error: unknown,
  trace: string,
  actions: string[] = [],
) {
  const next = actions.slice(0, 8).map((a) => String(a).slice(0, 600));
  for (const detail of shrinking(safeError(error))) {
    const response = {
      ...defaults(),
      status: "failed",
      errors: [detail],
      trace_id: trace,
      committed: false,
      recommended_next_actions: next,
    };
    try {
      assertResult(FailureResponse, response, "failure");
      return compactResponse(response, {
        status: 1,
        errors: 1,
        committed: 1,
        ...(next.length ? { recommended_next_actions: 1 } : {}),
      });
    } catch {
      /* try a smaller form */
    }
  }
  return compactResponse(
    {
      ...defaults(),
      status: "failed",
      errors: [
        {
          code: "INTERNAL_ERROR",
          message: "The request could not be processed.",
          details: {},
        },
      ],
      trace_id: trace,
      committed: false,
    },
    { status: 1, errors: 1, committed: 1 },
  );
}
