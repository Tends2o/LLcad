import { requireThat } from "../semantic-ir/errors.js";
import { hash } from "../semantic-ir/hash.js";
import { ToolName } from "../semantic-ir/schema.js";
export type Principal = { tenant: string; user: string; scopes: string[] };
export const SCOPES = [
  "model:read",
  "model:create",
  "model:edit",
  "model:commit",
  "model:export",
  "model:publish",
];
export const ROLE_SCOPES = {
  reader: ["model:read"],
  designer: ["model:read", "model:edit"],
  reviewer: ["model:read", "model:commit"],
  editor: ["model:read", "model:edit", "model:commit"],
};
export const POLICY = {
  version: 2,
  scopes: SCOPES,
  object_acl: "tenant_project_owner_or_current_expiring_grant",
  project_boundary: "one_model_and_its_versioned_project_structure",
  roles: ROLE_SCOPES,
  grant_limits: { jobs: 1000, seconds_per_job: 45, validity_days: 365 },
  approval_path: "separate_operator_JWS_with_one_time_bound_request",
  mandatory_hooks: true,
  external_publication: false,
  worker_sandbox: "bubblewrap",
  max_queued: 16,
};
export const POLICY_HASH = hash(POLICY);
export function scopeFor(tool: ToolName) {
  if (tool === "cad_create_model") return "model:create";
  if (tool === "cad_commit") return "model:commit";
  if (tool === "cad_export") return "model:export";
  if (
    [
      "cad_apply_patch",
      "cad_validate",
      "cad_discard",
      "cad_revert",
      "cad_rebuild",
      "cad_solve_constraints",
      "cad_import",
      "cad_job_cancel",
    ].includes(tool)
  )
    return "model:edit";
  return "model:read";
}
export function authorize(
  p: Principal,
  scope: string,
  object?: { tenant: string; owner: string },
) {
  requireThat(
    p && p.tenant && p.user,
    "AUTH_REQUIRED",
    "Authentifizierung erforderlich.",
  );
  requireThat(
    p.scopes.includes(scope),
    "ACCESS_DENIED",
    "Erforderlicher Scope fehlt.",
  );
  if (object)
    requireThat(
      object.tenant === p.tenant && object.owner === p.user,
      "ACCESS_DENIED",
      "Kein Zugriff auf dieses Objekt.",
    );
}
