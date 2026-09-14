import { z } from "zod";
import { CadError, requireThat, safeError } from "../semantic-ir/errors.js";
import { hash } from "../semantic-ir/hash.js";
import { ToolName } from "../semantic-ir/schema.js";
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
    "Das Ergebnis erfüllt den registrierten Antwortvertrag nicht.",
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
    "Antwort überschreitet das Budget. Engeren Ausschnitt oder Ressourcenabruf verwenden.",
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
    "Prüfbericht und Prüfdigest stimmen nicht überein.",
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
  assertResult(ToolOutputSchemas[tool], response, tool);
  return response;
}
export function failureResponse(error: unknown, trace: string) {
  const response = {
    ...defaults(),
    status: "failed",
    errors: [safeError(error)],
    trace_id: trace,
    committed: false,
  };
  try {
    assertResult(FailureResponse, response, "failure");
    return response;
  } catch {
    return {
      ...defaults(),
      status: "failed",
      errors: [
        {
          code: "INTERNAL_ERROR",
          message: "Die Anfrage konnte nicht verarbeitet werden.",
          details: {},
        },
      ],
      trace_id: trace,
      committed: false,
    };
  }
}
