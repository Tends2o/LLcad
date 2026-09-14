import { z } from "zod";
import { ToolSchemas, READ_TOOLS, ToolName } from "../semantic-ir/schema.js";
export const SERVER_INFO = { name: "mathforge-3d", version: "0.1.0" };
export const INSTRUCTIONS =
  "Operate CAD entirely through tools; never require the user to click in the viewer. Discover existing private models with cad_list_models and identify features with cad_find and cad_inspect. Read the current revision before editing. Apply a bounded patch, poll its job, validate the candidate, then commit with the returned validation digest. Always preserve units and protected constraints. Resolve genuine ambiguity through semantic tool queries or a short natural-language clarification. Preview is not validation.";
export const descriptions: Record<ToolName, string> = {
  cad_capabilities:
    "Read implemented operators, formats, precision profiles and resource limits.",
  cad_list_models:
    "Discover the authenticated user's private models by name or purpose, with bounded pagination. Use this before asking the user for a model ID or viewer interaction.",
  cad_create_model:
    "Create an empty, private mathematical model. Add geometry with a candidate patch.",
  cad_get_model:
    "Read a revision summary, feature page, quality status and geometric measurements.",
  cad_find:
    "Find semantic features in an exact revision. Multiple results require an explicit selection.",
  cad_inspect:
    "Inspect a feature and paged native faces. face_limit is 1..16 (default 8); use face_offset for more. Supply feature_id, face_id and an explicit revision to select a face's proven source feature. selection_handle plus rebind:true and an explicit descendant revision creates a new binding only when every intermediate revision has a unique successor. Splits, merges and unknown provenance require a new selection.",
  cad_measure:
    "Read registered geometric measurements. Distance creates a durable analysis job and requires two feature IDs and an idempotency key.",
  cad_plan_edit:
    "Compile a bounded edit and inspect dependencies and resource estimates.",
  cad_solve_constraints:
    "Solve up to 12 bounded continuous parameters against normalized dimensionless equations and inequalities using SLSQP. Returns a job with suggested patch operations and persistent equation constraints. Does not change the model; plan/apply the returned operations, validate and commit normally. Local convergence is checked and never claimed to be a global optimum.",
  cad_apply_patch:
    "Build an isolated candidate from strict registered operations. Returns a durable job; does not commit.",
  cad_validate:
    "Run all required profile checks on one candidate. Poll the job for its server-generated validation digest.",
  cad_compare:
    "Compare two exact revisions, changed features and measured dimensions.",
  cad_commit:
    "Atomically commit the exact validated candidate and proof digest if its base is still current.",
  cad_discard:
    "Discard an uncommitted candidate and cancel its remaining jobs.",
  cad_revert:
    "Propose a previous committed construction as a new candidate, retaining current protected constraints.",
  cad_rebuild:
    "Explicitly rebuild the current immutable construction for target_registry_hash from cad_capabilities. Read mode:plan first, then mode:candidate with a new idempotency key. Preserves all constraints and the old revision; requires normal validation and commit. Use when cad_get_model reports rebuild_required after a build upgrade.",
  cad_render:
    "Create a derived geometric preview artifact for an exact revision or feature. Returns a job.",
  cad_import:
    "Decode an uploaded, authorized artifact into a new candidate. Units must be explicit.",
  cad_export:
    "Export a committed revision, re-read the file and report actual losses and roundtrip checks.",
  cad_job_get: "Read status, diagnostics and results of an authorized job.",
  cad_job_cancel:
    "Cancel an authorized unfinished job. Committed revisions remain immutable.",
};
export const OUTPUT_SCHEMA = {
  type: "object",
  required: ["status", "errors", "trace_id"],
  properties: {
    status: { type: "string" },
    errors: {
      type: "array",
      items: {
        type: "object",
        required: ["code", "message"],
        properties: {
          code: { type: "string" },
          message: { type: "string" },
          details: { type: "object" },
        },
      },
    },
    trace_id: { type: "string" },
  },
  additionalProperties: true,
} as const;
export function toolDefinitions() {
  return Object.entries(ToolSchemas).map(([name, schema]) => ({
    name,
    title: name.replace("cad_", "").replaceAll("_", " "),
    description: descriptions[name as ToolName],
    inputSchema: z.toJSONSchema(schema),
    outputSchema: OUTPUT_SCHEMA,
    annotations: {
      readOnlyHint: READ_TOOLS.has(name as ToolName) && name !== "cad_measure",
      destructiveHint: ["cad_commit", "cad_discard", "cad_job_cancel"].includes(
        name,
      ),
      idempotentHint: true,
      openWorldHint: false,
    },
  }));
}
export const resourceTemplates = [
  {
    uriTemplate: "cad://models/{model_id}/revisions/{revision}/summary",
    name: "Model revision summary",
    mimeType: "application/json",
  },
  {
    uriTemplate:
      "cad://models/{model_id}/revisions/{revision}/features/{feature_id}",
    name: "Feature detail",
    mimeType: "application/json",
  },
  {
    uriTemplate: "cad://transactions/{transaction_id}/validation",
    name: "Bound validation evidence",
    mimeType: "application/json",
  },
  {
    uriTemplate: "cad://artifacts/{artifact_id}/manifest",
    name: "Artifact manifest",
    mimeType: "application/json",
  },
];
export function toolResult(result: any) {
  return {
    isError: result.status === "failed",
    structuredContent: result,
    content: [{ type: "text" as const, text: JSON.stringify(result) }],
  };
}
