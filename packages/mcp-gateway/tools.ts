import {
  ToolSchemas,
  READ_TOOLS,
  ToolName,
  inputJSONSchema,
} from "../semantic-ir/schema.js";
import { outputJSONSchema } from "../semantic-ir/results.js";
export const SERVER_INFO = { name: "mathforge-3d", version: "0.1.0" };
export const INSTRUCTIONS =
  "Operate CAD entirely through tools; never require the user to click in the viewer. Discover owned and explicitly shared models with cad_list_models and identify features with cad_find and cad_inspect. Read the current revision before editing. For shared projects, inspect cad_access for the existing role, feature scope and job budget. Grant changes require explicit user authorization through the trusted policy host. Apply a bounded patch, poll its job, validate the candidate, then commit with the returned validation digest. Always preserve units and protected constraints. Resolve genuine ambiguity through semantic tool queries or a short natural-language clarification. Preview is not validation.";
export const descriptions: Record<ToolName, string> = {
  cad_access:
    "Inspect your current role, feature scope and remaining job budget for a model project. Owners can inspect all grants or propose a grant/revocation using the exact current base_revision. Proposals disclose the recipient, permissions, budget and expiry and require a separately authenticated trusted policy confirmation; this tool cannot approve or fabricate confirmation tokens. Read a proposal's state with mode:request. Permission changes invalidate pending work from the previous grant version. Ordinary CAD edits continue through plan, candidate, validate and commit within the existing grant.",
  cad_capabilities:
    "Read implemented operators, formats, precision profiles and resource limits.",
  cad_list_models:
    "Discover the authenticated user's own or explicitly shared model projects by name or purpose, with bounded pagination. cad_access reveals the current role and permitted edit scope. Use discovery before asking the user for a model ID or viewer interaction.",
  cad_create_model:
    "Create an empty, private mathematical model. Add geometry with a candidate patch.",
  cad_get_model:
    "Read a revision summary, feature page, quality status and geometric measurements.",
  cad_structure:
    "Discover versioned project, assembly, part and frame summaries. Page by kind, optionally filter by entity_id or query. Includes structural definitions, their shared structure_hash, world bounds and provenance. Use cad_find with owner_part to locate that part's features. Edit structure through a normal set_structure candidate patch using the returned hash; feature ownership/frame changes use set_feature_context and the context_hash from cad_inspect.",
  cad_find:
    "Find semantic features in an exact revision. Multiple results require an explicit selection.",
  cad_inspect:
    "Inspect a feature and paged native faces. face_limit is 1..16 (default 8); use face_offset for more. Supply feature_id, face_id and an explicit revision to select a face's proven source feature. selection_handle plus rebind:true and an explicit descendant revision creates a new binding only when every intermediate revision has a unique successor. Splits, merges and unknown provenance require a new selection.",
  cad_measure:
    "Read registered measurements. distance, clearance, angle and curvature create durable jobs and require feature_id and idempotency_key; all except curvature also require other_feature_id. Curvature measures native curves or surfaces at one point; angle compares their oriented tangents/normals. Multi-face features require face_id from cad_inspect and an explicit revision. uv uses native UV bounds; curve_parameter is normalized [0,1], default 0.5. clearance measures whole static features against optional minimum_clearance; it does not certify motion or global wall thickness.",
  cad_plan_edit:
    "Compile a bounded edit and inspect dependencies and resource estimates. For one linear/circular pattern occurrence, use set_pattern_occurrence with the construction_hash from cad_inspect and the zero-based index from face origins. An override translates in the pattern feature's local frame in mm and/or references an explicit variant feature; inspect frame_to_world to interpret world directions. Null restores the shared occurrence. The shared source stays unchanged.",
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
export function toolDefinitions() {
  return Object.entries(ToolSchemas).map(([name, schema]) => ({
    name,
    title: name.replace("cad_", "").replaceAll("_", " "),
    description: descriptions[name as ToolName],
    inputSchema: inputJSONSchema(name as ToolName),
    outputSchema: outputJSONSchema(name as ToolName),
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
