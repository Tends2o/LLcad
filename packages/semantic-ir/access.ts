import { z } from "zod";
import { Id, Subject } from "./identifiers.js";

export const ProjectRole = z.enum(["reader", "designer", "reviewer", "editor"]);
export const GrantBudget = z.strictObject({
  jobs: z.int().min(0).max(1000),
  seconds_per_job: z.int().min(1).max(45),
});
export const GrantSpec = z.strictObject({
  recipient: Subject,
  role: ProjectRole,
  can_export: z.boolean(),
  edit_scope: z.union([
    z.strictObject({ kind: z.literal("model") }),
    z.strictObject({
      kind: z.literal("features"),
      feature_ids: z.array(Id).min(1).max(256),
    }),
  ]),
  budget: GrantBudget,
  expires_at: z.iso.datetime(),
});
export type GrantSpec = z.infer<typeof GrantSpec>;
const Digest = z.string().regex(/^[a-f0-9]{64}$/);
export const PublishSpec = z.strictObject({
  recipient: Subject,
  revision: Id,
  package_artifact_id: Id,
  package_hash: Digest,
  validity_days: z.int().min(1).max(30),
});
export const AccessAction = z.discriminatedUnion("action", [
  z.strictObject({ action: z.literal("grant"), grant: GrantSpec }),
  z.strictObject({ action: z.literal("revoke"), recipient: Subject }),
  z.strictObject({ action: z.literal("publish"), ...PublishSpec.shape }),
]);
export const AccessTool = z.discriminatedUnion("mode", [
  z.strictObject({
    mode: z.literal("inspect"),
    model_id: Id,
    offset: z.int().min(0).default(0),
    limit: z.int().min(1).max(16).default(8),
  }),
  z.strictObject({
    mode: z.literal("propose"),
    model_id: Id,
    base_revision: Id,
    change: AccessAction,
    idempotency_key: z.string().min(16).max(128),
  }),
  z.strictObject({
    mode: z.literal("request"),
    model_id: Id,
    approval_request_id: Id,
  }),
  z.strictObject({
    mode: z.literal("publications"),
    offset: z.int().min(0).default(0),
    limit: z.int().min(1).max(16).default(8),
  }),
]);
export const AccessProposal = z.strictObject({
  schema_version: z.literal("1"),
  actor: z.strictObject({ tenant: Subject, user: Subject }),
  model_id: Id,
  base_revision: Id,
  candidate_hash: z.string().regex(/^[a-f0-9]{64}$/),
  policy_hash: z.string().regex(/^[a-f0-9]{64}$/),
  acl_generation: z.int().nonnegative(),
  change: AccessAction,
});
export type AccessProposal = z.infer<typeof AccessProposal>;
export const PublishedArtifact = z.strictObject({
  artifact_id: Id,
  filename: z.string().max(200).nullable(),
  sha256: Digest,
  download: z.string().regex(/^\/api\/artifacts\/[a-zA-Z0-9_-]+$/),
  signed_download: z.string().max(4096).nullable(),
  expires_at: z.iso.datetime().nullable(),
});
export const AccessDecision = z.union([
  z.strictObject({
    status: z.literal("approved"),
    model_id: Id,
    approval_request_id: Id,
    action_digest: Digest,
    grant_id: Id,
    grant_version: z.int().positive(),
    acl_generation: z.int().positive(),
    action: z.enum(["grant", "revoke"]),
    recipient: Subject,
    cancelled_jobs: z.array(Id),
    approved_at: z.iso.datetime(),
  }),
  z.strictObject({
    status: z.literal("approved"),
    model_id: Id,
    approval_request_id: Id,
    action_digest: Digest,
    action: z.literal("publish"),
    publication_id: Id,
    revision: Id,
    package_hash: Digest,
    recipient: Subject,
    expires_at: z.iso.datetime(),
    artifacts: z.array(PublishedArtifact).min(1),
    transfer: z.literal("internal_recipient_only_no_external_transmission"),
    approved_at: z.iso.datetime(),
  }),
]);
export const PublicationView = z.strictObject({
  publication_id: Id,
  model_id: Id,
  revision: Id,
  package_hash: Digest,
  role: z.enum(["owner", "recipient"]),
  recipient: Subject,
  expires_at: z.iso.datetime(),
  created: z.iso.datetime(),
  artifacts: z.array(PublishedArtifact),
});
const GrantSummary = z.strictObject({
  grant_id: Id,
  version: z.int().positive(),
  state: z.enum(["active", "revoked", "expired"]),
  grant: GrantSpec,
  used_jobs: z.int().nonnegative(),
});
export const AccessPayload = z.union([
  z.strictObject({
    model_id: Id,
    acl_generation: z.int().nonnegative(),
    role: z.enum(["owner", ...ProjectRole.options]),
    own_grant: GrantSummary.nullable(),
    members: z.array(GrantSummary).max(16),
    member_count: z.int().nonnegative(),
    next_offset: z.int().nonnegative().nullable(),
    approval_required_for_changes: z.literal(true),
  }),
  z.strictObject({
    status: z.literal("needs_approval"),
    model_id: Id,
    base_revision: Id,
    approval_request_id: Id,
    action_digest: z.string().regex(/^[a-f0-9]{64}$/),
    proposal: AccessProposal,
    expires_at: z.iso.datetime(),
    committed: z.literal(false),
    recommended_next_actions: z.array(z.string()),
  }),
  z.strictObject({
    model_id: Id,
    approval_request_id: Id,
    action_digest: z.string().regex(/^[a-f0-9]{64}$/),
    proposal: AccessProposal,
    request_state: z.enum(["pending", "approved", "expired"]),
    expires_at: z.iso.datetime(),
    result: AccessDecision.nullable(),
  }),
  z.strictObject({
    publications: z.array(PublicationView).max(16),
    total: z.int().nonnegative(),
    next_offset: z.int().nonnegative().nullable(),
    link_lifetime_seconds: z.int().positive(),
    external_transmission: z.literal(false),
  }),
]);
