import { createHash, randomBytes } from "node:crypto";
import { z } from "zod";
import type { ModelService } from "../model-service/index.js";
import type { Principal } from "../policy/index.js";
import { GrantSpec } from "../semantic-ir/access.js";
import { CadError, requireThat } from "../semantic-ir/errors.js";
import { id } from "../semantic-ir/hash.js";
import { PatchOperation } from "../semantic-ir/schema.js";
import { ToolSchemas } from "../semantic-ir/tool-registry.js";
import { normaliseDecimals } from "../semantic-ir/units.js";
import { structureOf } from "../compiler/structure.js";
import { waveConflicts } from "./merge.js";

/** Validated feature workers act as their own principal: an ephemeral designer grant
 *  (model:read + model:edit, never commit/export) on exactly one model, bound to one
 *  task of one batch, reachable only with a random token that lives in memory for the
 *  task's lifetime. The owner's own call to cad_feature_batch issues it; it is audited,
 *  revoked when the task ends and never survives a restart. */
export const WORKER_USER_PREFIX = "feature-worker:";
/** Server-enforced tool allowlist of validated workers (tools a build lacks drop out). */
const WORKER_TOOL_CANDIDATES = [
  "cad_capabilities",
  "cad_get_model",
  "cad_structure",
  "cad_context",
  "cad_find",
  "cad_inspect",
  "cad_measure",
  "cad_compare",
  "cad_plan_edit",
  "cad_apply_patch",
  "cad_validate",
  "cad_discard",
  "cad_job_get",
  "cad_job_cancel",
  "cad_render",
  "cad_view",
  "cad_gates",
];
export function workerTools() {
  return WORKER_TOOL_CANDIDATES.filter((tool) => Object.hasOwn(ToolSchemas, tool));
}

export interface OwnershipTask {
  id: string;
  owned_feature_ids: string[];
  owned_part_ids?: string[];
}
interface Binding {
  principal: Principal;
  owner: Principal;
  model: string;
  base_revision: string;
  batch_id: string;
  task: OwnershipTask;
  lanes: boolean;
  base_ir: any;
  tools: string[];
  expires: number;
  token_hash: string;
}
const byToken = new Map<string, Binding>();
const byUser = new Map<string, Binding>();
const digest = (token: string) => createHash("sha256").update(token).digest("hex");

/** Auth hook: a live worker token authenticates as its worker principal. */
export function workerPrincipal(token: string): Principal | null {
  if (!token || token.length < 32 || byToken.size === 0) return null;
  const binding = byToken.get(digest(token));
  if (!binding) return null;
  if (binding.expires <= Date.now()) return null;
  return binding.principal;
}

export function issueWorkerAccess(
  service: ModelService,
  owner: Principal,
  a: {
    batch_id: string;
    task: OwnershipTask;
    model: string;
    base_revision: string;
    base_ir: any;
    lanes: boolean;
    ttl_ms: number;
    jobs?: number;
  },
) {
  const store = service.store;
  const user = `${WORKER_USER_PREFIX}${a.batch_id}:${a.task.id}`;
  const expires = Date.now() + a.ttl_ms;
  const spec = GrantSpec.parse({
    recipient: user,
    role: "designer",
    can_export: false,
    edit_scope: { kind: "model" },
    budget: { jobs: Math.min(a.jobs ?? 120, 1000), seconds_per_job: 45 },
    expires_at: new Date(expires).toISOString(),
  });
  store.run(
    "INSERT INTO model_grants(id,model,tenant,recipient,spec,state,version,expires,created) VALUES(?,?,?,?,?,'active',1,?,?) ON CONFLICT(model,recipient) DO UPDATE SET spec=excluded.spec,state='active',version=model_grants.version+1,expires=excluded.expires",
    id("grant"),
    a.model,
    owner.tenant,
    user,
    JSON.stringify(spec),
    expires,
    new Date().toISOString(),
  );
  const token = randomBytes(32).toString("base64url");
  const binding: Binding = {
    principal: { tenant: owner.tenant, user, scopes: ["model:read", "model:edit"] },
    owner,
    model: a.model,
    base_revision: a.base_revision,
    batch_id: a.batch_id,
    task: a.task,
    lanes: a.lanes,
    base_ir: a.base_ir,
    tools: workerTools(),
    expires,
    token_hash: digest(token),
  };
  byToken.set(binding.token_hash, binding);
  byUser.set(user, binding);
  store.audit("feature_worker_access_issued", {
    model_id: a.model,
    batch_id: a.batch_id,
    task_id: a.task.id,
    recipient: user,
    role: "designer",
    expires_at: spec.expires_at,
  });
  return { token, user, tools: binding.tools, principal: binding.principal };
}

/** Ends a worker's access: token, grant, queued/running jobs and open candidates. */
export function revokeWorkerAccess(service: ModelService, model: string, user: string) {
  const binding = byUser.get(user);
  if (binding) {
    byToken.delete(binding.token_hash);
    byUser.delete(user);
  }
  const store = service.store;
  const jobs = store.all(
    "SELECT id FROM jobs WHERE model=? AND owner=? AND state IN ('queued','running')",
    model,
    user,
  );
  for (const job of jobs)
    store.run(
      "UPDATE jobs SET state='cancelled',lease=NULL,lease_until=NULL,phase='cancelled',finished=? WHERE id=?",
      Date.now(),
      job.id,
    );
  store.run(
    "UPDATE transactions SET state='aborted' WHERE model=? AND owner=? AND state NOT IN ('committed','aborted','discarded','failed')",
    model,
    user,
  );
  store.run(
    "UPDATE model_grants SET state='revoked',version=version+1 WHERE model=? AND recipient=? AND state='active'",
    model,
    user,
  );
  if (jobs.length) service.jobs.cancelRevoked(jobs.map((j: any) => j.id));
  store.audit("feature_worker_access_revoked", { model_id: model, recipient: user, cancelled_jobs: jobs.length });
}

/** After a restart no worker token exists any more: close their grants and candidates. */
export function revokeStaleWorkers(service: ModelService) {
  for (const row of service.store.all(
    "SELECT model,recipient FROM model_grants WHERE recipient LIKE ? AND state='active'",
    WORKER_USER_PREFIX + "%",
  ))
    revokeWorkerAccess(service, row.model, row.recipient);
}

const same = (a: string[], b: string[]) => a.length === b.length && a.every((v, i) => v === b[i]);
/** Changes of an id list limited to `own`: the remaining entries keep their order. */
function ownChange(before: string[], after: string[], own: Set<string>) {
  return same(
    before.filter((x) => !own.has(x)),
    after.filter((x) => !own.has(x)),
  );
}
const STRUCTURE_ID = (task: string) => new RegExp(`^(?:(?:pt|fr|as)-)?${task}-`);

/** Ownership rules of one task's operations against the frozen base (proposal check
 *  and live worker patches alike). New features carry the task namespace; edits touch
 *  owned or task-created features only; structure/output edits need lanes (opt-in) and
 *  may only move the task's own features on owned or task-created parts. */
export function checkOwnership(
  task: OwnershipTask,
  input: unknown[],
  baseIR: any,
  options: { lanes?: boolean; max?: number } = {},
) {
  const max = options.max ?? 64;
  requireThat(
    Array.isArray(input) && input.length <= max,
    "BUDGET_EXCEEDED",
    `A feature proposal may contain at most ${max} operations.`,
  );
  const schema = z.array(PatchOperation);
  const parsed = schema.safeParse(normaliseDecimals(schema as any, input));
  if (!parsed.success) {
    const issue = parsed.error.issues[0];
    throw new CadError(
      "INVALID_SCHEMA",
      `Feature proposal: operation does not match the schema (${issue?.path.join(".")}: ${issue?.message}).`,
      { issues: parsed.error.issues.slice(0, 8).map((i) => ({ path: i.path, message: i.message })) },
    );
  }
  const operations = parsed.data as any[];
  const existing = new Set<string>(baseIR.features.map((f: any) => f.id));
  const owned = new Set<string>(task.owned_feature_ids);
  const created = new Set<string>();
  const own = () => new Set<string>([...owned, ...created]);
  const structure = structureOf(baseIR);
  const partOutputs = new Map<string, string[]>(
    structure.parts.map((p: any) => [p.id, [...(p.outputs ?? [])]]),
  );
  const ownedParts = new Set<string>(task.owned_part_ids ?? []);
  const createdStructure = new Set<string>();
  let modelOutputs: string[] = [...(baseIR.outputs ?? [])];
  const lanesOnly = (op: string) =>
    requireThat(
      options.lanes === true,
      "ACCESS_DENIED",
      `${op}: structure and outputs belong to the orchestrator; only lanes (lanes: true) change their own parts.`,
    );
  for (const op of operations) {
    switch (op.op) {
      case "add_feature": {
        requireThat(
          op.feature.id.startsWith(task.id + "-") &&
            !existing.has(op.feature.id) &&
            !created.has(op.feature.id),
          "CONSTRAINT_CONFLICT",
          "New features must have a unique task namespace.",
          { feature_id: op.feature.id, namespace: task.id + "-" },
        );
        created.add(op.feature.id);
        requireThat(
          (op.feature.depends_on ?? []).every((fid: string) => existing.has(fid) || created.has(fid)),
          "CONSTRAINT_CONFLICT",
          "A worker must not depend on a parallel proposal.",
        );
        break;
      }
      case "add_part":
      case "add_frame":
      case "add_assembly": {
        lanesOnly(op.op);
        const entity = op.part ?? op.frame ?? op.assembly;
        requireThat(
          STRUCTURE_ID(task.id).test(entity.id),
          "CONSTRAINT_CONFLICT",
          `New structure ids of a lane start with pt-${task.id}-, fr-${task.id}- or ${task.id}-.`,
          { id: entity.id },
        );
        createdStructure.add(entity.id);
        if (op.op === "add_part") {
          requireThat(
            (entity.outputs ?? []).every((fid: string) => own().has(fid)),
            "ACCESS_DENIED",
            "A new lane part outputs only its own features.",
          );
          partOutputs.set(entity.id, [...(entity.outputs ?? [])]);
        }
        break;
      }
      case "set_part_outputs": {
        lanesOnly(op.op);
        const before = partOutputs.get(op.part_id);
        requireThat(
          before && (ownedParts.has(op.part_id) || createdStructure.has(op.part_id)),
          "ACCESS_DENIED",
          "Lanes change only the outputs of their owned_part_ids or of their own new parts.",
          { part_id: op.part_id },
        );
        requireThat(
          ownChange(before!, op.outputs, own()),
          "ACCESS_DENIED",
          "Part outputs: add or remove only own features.",
          { part_id: op.part_id },
        );
        partOutputs.set(op.part_id, [...op.outputs]);
        break;
      }
      case "set_outputs": {
        lanesOnly(op.op);
        requireThat(
          ownChange(modelOutputs, op.outputs, own()),
          "ACCESS_DENIED",
          "Model outputs: add or remove only own features.",
        );
        modelOutputs = [...op.outputs];
        break;
      }
      default: {
        requireThat(
          "feature_id" in op && own().has(op.feature_id),
          "ACCESS_DENIED",
          "A worker may change only its assigned features; global structure and outputs belong to the orchestrator.",
          { op: op.op, feature_id: op.feature_id ?? null },
        );
        if (op.op === "set_feature_dependencies")
          requireThat(
            op.depends_on.every((fid: string) => existing.has(fid) || created.has(fid)),
            "CONSTRAINT_CONFLICT",
            "Unknown proposal dependency.",
          );
      }
    }
  }
  return { operations, created: [...created], structure: [...createdStructure] };
}

const WRITE_TOOLS = new Set(["cad_apply_patch", "cad_plan_edit"]);
/** Called by ModelService for every tool call after schema parsing:
 *  1. worker principals: tool allowlist, model/base binding, ownership of every patch,
 *     no patch_ref, no commit, only their own candidates;
 *  2. patch_ref to a feature-batch artifact: the tasks it carries must still be
 *     untouched on the base it is applied to (rebase check against the frozen base). */
export function featureWorkGuard(service: ModelService, p: Principal, tool: string, args: any) {
  if (p?.user?.startsWith(WORKER_USER_PREFIX)) {
    const b = byUser.get(p.user);
    requireThat(
      b && b.principal.tenant === p.tenant && b.expires > Date.now(),
      "ACCESS_DENIED",
      "The feature worker access has expired.",
    );
    requireThat(b!.tools.includes(tool), "ACCESS_DENIED", `Feature workers must not call ${tool}.`, {
      allowed_tools: b!.tools,
    });
    if (args?.model_id !== undefined)
      requireThat(args.model_id === b!.model, "ACCESS_DENIED", "Feature workers work only on the model of their task.");
    if (WRITE_TOOLS.has(tool)) {
      requireThat(!args.patch_ref, "INVALID_SCHEMA", "Feature workers pass operations inline (no patch_ref).");
      requireThat(
        args.commit !== "if_valid",
        "ACCESS_DENIED",
        "Feature workers never commit: validate: true checks their own candidate.",
      );
      requireThat(
        args.base_revision === b!.base_revision,
        "STALE_REVISION",
        "Feature workers build only on the frozen base of their task.",
        { base_revision: b!.base_revision },
      );
      checkOwnership(b!.task, args.operations ?? [], b!.base_ir, { lanes: b!.lanes });
    }
    if (args?.transaction_id) {
      const tx = service.store.transaction(p, args.transaction_id);
      requireThat(tx.owner === p.user, "ACCESS_DENIED", "Feature workers edit only their own candidates.");
    }
    return;
  }
  if (WRITE_TOOLS.has(tool) && args?.patch_ref?.artifact_id) {
    const artifact = service.store.getArtifact(p, args.patch_ref.artifact_id);
    const kind = artifact.manifest?.kind;
    if (!["feature_wave", "feature_proposal"].includes(kind) || !artifact.manifest?.batch_id) return;
    const row = service.store.get(
      "SELECT record FROM feature_batches WHERE id=? AND model=?",
      artifact.manifest.batch_id,
      artifact.model,
    );
    if (!row) return;
    const batch = JSON.parse(row.record);
    if (args.base_revision === batch.revision) return;
    const head = service.store.revision(p, artifact.model, args.base_revision);
    const base = service.store.revision(p, artifact.model, batch.revision);
    const taskIds: string[] = artifact.manifest.task_ids ?? [];
    const conflicts = waveConflicts(batch, taskIds, base.ir, head.ir, artifact.manifest.wave ?? null);
    requireThat(
      conflicts.length === 0,
      "STALE_REVISION",
      "The proposals of this patch document were created on a base whose features have changed since.",
      { batch_id: batch.id, frozen_base_revision: batch.revision, conflicts: conflicts.slice(0, 16) },
    );
  }
}
