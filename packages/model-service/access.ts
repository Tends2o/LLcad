import type { Store } from "./store.js";
import {
  authorize,
  POLICY_HASH,
  Principal,
  ROLE_SCOPES,
} from "../policy/index.js";
import {
  AccessAction,
  AccessDecision,
  AccessProposal,
  GrantSpec,
} from "../semantic-ir/access.js";
import { assertResult } from "./result-contracts.js";
import { requireThat } from "../semantic-ir/errors.js";
import { hash, id } from "../semantic-ir/hash.js";
import { compile, LIMITS } from "../compiler/index.js";
import type { ModelIR } from "../semantic-ir/schema.js";
import type { ApprovalClaims } from "../policy/approvals.js";

export const JOB_SCOPES: Record<string, string> = {
  evaluate: "model:edit",
  validate: "model:edit",
  solve: "model:edit",
  measure: "model:read",
  analysis: "model:read",
  render: "model:read",
  export: "model:export",
};
export class ProjectAccess {
  onRevoked?: (jobs: string[]) => void;
  constructor(private store: Store) {}
  private currentGrant(model: string, user: string) {
    const row = this.store.get(
      "SELECT * FROM model_grants WHERE model=? AND recipient=? AND state='active' AND expires>?",
      model,
      user,
      Date.now(),
    );
    return row ? { ...row, spec: GrantSpec.parse(JSON.parse(row.spec)) } : null;
  }
  context(p: Principal, model: string, scope = "model:read") {
    authorize(p, scope);
    const row = this.store.get("SELECT * FROM models WHERE id=?", model);
    requireThat(
      row?.tenant === p.tenant,
      "ACCESS_DENIED",
      "Modell nicht zugänglich.",
    );
    if (row.owner === p.user) return { model: row, grant: null };
    const grant = this.currentGrant(model, p.user);
    requireThat(
      grant &&
        (scope === "model:export"
          ? grant.spec.can_export
          : ROLE_SCOPES[grant.spec.role as keyof typeof ROLE_SCOPES].includes(
              scope,
            )),
      "ACCESS_DENIED",
      "Aktuelle Projektrolle erlaubt diese Aktion nicht.",
    );
    return { model: row, grant };
  }
  visible(p: Principal, alias = "models") {
    authorize(p, "model:read");
    // alias is always a server constant, never a request value.
    return {
      sql: `${alias}.tenant=? AND (${alias}.owner=? OR EXISTS (SELECT 1 FROM model_grants g WHERE g.model=${alias}.id AND g.recipient=? AND g.state='active' AND g.expires>?))`,
      params: [p.tenant, p.user, p.user, Date.now()],
    };
  }
  inspect(p: Principal, model: string, offset: number, limit: number) {
    const context = this.context(p, model);
    const generation = this.generation(model);
    const members = context.grant
      ? []
      : this.store.all(
          "SELECT * FROM model_grants WHERE model=? ORDER BY recipient LIMIT ? OFFSET ?",
          model,
          limit,
          offset,
        );
    const count = context.grant
      ? 0
      : this.store.get(
          "SELECT COUNT(*) AS n FROM model_grants WHERE model=?",
          model,
        ).n;
    const summary = (g: any) => {
      const spec =
        g.spec && typeof g.spec === "object"
          ? g.spec
          : GrantSpec.parse(JSON.parse(g.spec));
      return {
        grant_id: g.id,
        version: g.version,
        state:
          g.state === "active" && g.expires <= Date.now() ? "expired" : g.state,
        grant: spec,
        used_jobs: this.usedJobs(g.id, g.version),
      };
    };
    return {
      model_id: model,
      acl_generation: generation,
      role: context.grant?.spec.role ?? "owner",
      own_grant: context.grant ? summary(context.grant) : null,
      members: members.map(summary),
      member_count: count,
      next_offset: offset + limit < count ? offset + limit : null,
      approval_required_for_changes: true,
    };
  }
  generation(model: string) {
    return (
      this.store.get("SELECT generation FROM model_acl WHERE model=?", model)
        ?.generation ?? 0
    );
  }
  usedJobs(grant: string, version: number) {
    return this.store.get(
      "SELECT COUNT(*) AS n FROM grant_actions WHERE grant_id=? AND grant_version=?",
      grant,
      version,
    ).n;
  }
  propose(p: Principal, args: any) {
    const { model } = this.context(p, args.model_id, "model:publish");
    requireThat(
      this.store.get(
        "SELECT COUNT(*) AS n FROM approval_requests WHERE model=? AND state='pending' AND expires>?",
        model.id,
        Date.now(),
      ).n < 16,
      "BUDGET_EXCEEDED",
      "Zu viele offene Freigabeanträge für dieses Projekt.",
    );
    requireThat(
      model.head === args.base_revision,
      "STALE_REVISION",
      "Freigabe benötigt die aktuelle Basisrevision.",
    );
    const change = AccessAction.parse(args.change);
    const recipient =
      change.action === "grant" ? change.grant.recipient : change.recipient;
    requireThat(
      recipient !== model.owner,
      "OUT_OF_SCOPE",
      "Eigentümerrechte werden nicht durch eine Projektfreigabe geändert.",
    );
    if (change.action === "grant") {
      const expires = Date.parse(change.grant.expires_at);
      requireThat(
        expires > Date.now() && expires <= Date.now() + 365 * 86400000,
        "OUT_OF_SCOPE",
        "Freigabe muss in der Zukunft und höchstens ein Jahr entfernt enden.",
      );
      change.grant.expires_at = new Date(expires).toISOString();
      const ir = this.store.revision(p, model.id, model.head).ir;
      if (change.grant.edit_scope.kind === "features") {
        const features = change.grant.edit_scope.feature_ids;
        requireThat(
          new Set(features).size === features.length &&
            features.every((fid) => ir.features.some((f: any) => f.id === fid)),
          "OUT_OF_SCOPE",
          "Freigabe muss eindeutige vorhandene Features benennen.",
        );
      }
    } else
      requireThat(
        this.store.get(
          "SELECT 1 FROM model_grants WHERE model=? AND recipient=? AND state='active'",
          model.id,
          recipient,
        ),
        "OUT_OF_SCOPE",
        "Keine aktive Freigabe für diesen Empfänger vorhanden.",
      );
    const revision = this.store.revision(p, model.id, model.head);
    const body = {
      schema_version: "1" as const,
      actor: { tenant: p.tenant, user: p.user },
      model_id: model.id,
      base_revision: model.head,
      policy_hash: POLICY_HASH,
      acl_generation: this.generation(model.id),
      change,
    };
    const proposal: AccessProposal = {
      ...body,
      candidate_hash: hash({
        ...body,
        source_ir_hash: revision.ir_hash,
        geometry_hash: revision.geometry ? hash(revision.geometry.facts) : null,
      }),
    };
    const rid = id("approval"),
      digest = hash(proposal),
      expires = Date.now() + 15 * 60000;
    this.store.run(
      "INSERT INTO approval_requests(id,tenant,owner,model,proposal,digest,state,expires,created) VALUES(?,?,?,?,?,?,'pending',?,?)",
      rid,
      p.tenant,
      p.user,
      model.id,
      JSON.stringify(proposal),
      digest,
      expires,
      new Date().toISOString(),
    );
    this.store.audit("project_access_proposed", {
      model_id: model.id,
      approval_request_id: rid,
      action_digest: digest,
    });
    return {
      status: "needs_approval",
      model_id: model.id,
      base_revision: model.head,
      approval_request_id: rid,
      action_digest: digest,
      proposal,
      expires_at: new Date(expires).toISOString(),
      committed: false,
      recommended_next_actions: [
        "trusted_policy_approval_then_cad_access_request",
      ],
    };
  }
  request(p: Principal, model: string, request: string) {
    this.context(p, model, "model:publish");
    const row = this.store.get(
      "SELECT * FROM approval_requests WHERE id=? AND model=? AND tenant=? AND owner=?",
      request,
      model,
      p.tenant,
      p.user,
    );
    requireThat(row, "ACCESS_DENIED", "Freigabeantrag nicht zugänglich.");
    return {
      model_id: model,
      approval_request_id: row.id,
      action_digest: row.digest,
      proposal: AccessProposal.parse(JSON.parse(row.proposal)),
      request_state:
        row.state === "pending" && row.expires <= Date.now()
          ? "expired"
          : row.state,
      expires_at: new Date(row.expires).toISOString(),
      result: row.result ? JSON.parse(row.result) : null,
    };
  }
  /** Called only after cryptographic verification by the separate policy route. */
  approve(p: Principal, request: string, claims: ApprovalClaims) {
    return this.store.atomic(() => {
      const row = this.store.get(
        "SELECT * FROM approval_requests WHERE id=?",
        request,
      );
      requireThat(row, "ACCESS_DENIED", "Freigabeantrag nicht zugänglich.");
      const view = this.request(p, row.model, request),
        proposal = view.proposal;
      requireThat(
        proposal.actor.tenant === p.tenant &&
          proposal.actor.user === p.user &&
          proposal.model_id === row.model,
        "NEEDS_APPROVAL",
        "Freigabeakteur oder Projektbindung stimmen nicht überein.",
      );
      requireThat(
        row.state === "pending" && row.expires > Date.now(),
        "NEEDS_APPROVAL",
        "Freigabeantrag ist verbraucht oder abgelaufen.",
      );
      requireThat(
        claims.approval_request_id === request &&
          claims.sub === p.user &&
          claims.tenant_id === p.tenant &&
          claims.action_digest === row.digest &&
          row.digest === hash(proposal) &&
          hash(claims.proposal) === row.digest &&
          claims.exp * 1000 > Date.now(),
        "NEEDS_APPROVAL",
        "Bestätigung passt nicht zum vollständigen Freigabeantrag.",
      );
      requireThat(
        !this.store.get(
          "SELECT 1 FROM approval_consumptions WHERE nonce=?",
          claims.jti,
        ),
        "NEEDS_APPROVAL",
        "Bestätigung wurde bereits verbraucht.",
      );
      const { model } = this.context(p, row.model, "model:publish");
      requireThat(
        model.head === proposal.base_revision &&
          proposal.policy_hash === POLICY_HASH &&
          proposal.acl_generation === this.generation(model.id),
        "STALE_REVISION",
        "Modell, Policy oder Projektfreigabe haben sich seit dem Antrag geändert.",
      );
      const revision = this.store.revision(p, model.id, model.head),
        { candidate_hash, ...body } = proposal;
      requireThat(
        candidate_hash ===
          hash({
            ...body,
            source_ir_hash: revision.ir_hash,
            geometry_hash: revision.geometry
              ? hash(revision.geometry.facts)
              : null,
          }),
        "INTEGRITY_FAILURE",
        "Freigabekandidat stimmt nicht mit dem Modell überein.",
      );
      const change = proposal.change,
        recipient =
          change.action === "grant" ? change.grant.recipient : change.recipient;
      const old = this.store.get(
        "SELECT * FROM model_grants WHERE model=? AND recipient=?",
        model.id,
        recipient,
      );
      const cancelled = old ? this.invalidate(old.id, old.version) : [];
      const now = new Date().toISOString(),
        gid = old?.id ?? id("grant"),
        version = (old?.version ?? 0) + 1;
      if (change.action === "grant") {
        requireThat(
          Date.parse(change.grant.expires_at) > Date.now(),
          "NEEDS_APPROVAL",
          "Beantragte Freigabe ist bereits abgelaufen.",
        );
        this.store.run(
          "INSERT INTO model_grants(id,model,tenant,recipient,spec,state,version,expires,created) VALUES(?,?,?,?,?,'active',?,?,?) ON CONFLICT(model,recipient) DO UPDATE SET spec=excluded.spec,state='active',version=excluded.version,expires=excluded.expires",
          gid,
          model.id,
          p.tenant,
          recipient,
          JSON.stringify(change.grant),
          version,
          Date.parse(change.grant.expires_at),
          now,
        );
      } else
        this.store.run(
          "UPDATE model_grants SET state='revoked',version=? WHERE id=?",
          version,
          gid,
        );
      this.store.run(
        "INSERT INTO model_acl(model,generation) VALUES(?,1) ON CONFLICT(model) DO UPDATE SET generation=generation+1",
        model.id,
      );
      const result = {
        status: "approved",
        model_id: model.id,
        approval_request_id: request,
        action_digest: row.digest,
        grant_id: gid,
        grant_version: version,
        acl_generation: this.generation(model.id),
        action: change.action,
        recipient,
        cancelled_jobs: cancelled,
        approved_at: now,
      };
      assertResult(AccessDecision, result, "access_decision");
      this.store.run(
        "INSERT INTO approval_consumptions VALUES(?,?,?)",
        claims.jti,
        request,
        claims.exp * 1000,
      );
      this.store.run(
        "UPDATE approval_requests SET state='approved',result=? WHERE id=?",
        JSON.stringify(result),
        request,
      );
      this.store.audit("project_access_changed", {
        model_id: model.id,
        approval_request_id: request,
        action_digest: row.digest,
        action: change.action,
        grant_id: gid,
        grant_version: version,
      });
      this.store.afterCommit(() => this.onRevoked?.(cancelled));
      return result;
    });
  }
  private invalidate(grant: string, version: number) {
    const jobs = this.store.all(
      "SELECT j.id FROM jobs j JOIN job_authorizations a ON a.job=j.id WHERE a.grant_id=? AND a.grant_version=? AND j.state IN ('queued','running')",
      grant,
      version,
    );
    for (const job of jobs)
      this.store.run(
        "UPDATE jobs SET state='cancelled',lease=NULL,lease_until=NULL WHERE id=?",
        job.id,
      );
    this.store.run(
      "UPDATE transactions SET state='aborted' WHERE state!='committed' AND id IN (SELECT tx FROM transaction_authorizations WHERE grant_id=? AND grant_version=?)",
      grant,
      version,
    );
    return jobs.map((j) => j.id) as string[];
  }
  checkEditForCommit(p: Principal, tx: any) {
    this.checkEdit(
      p,
      tx.model,
      this.store.revision(p, tx.model, tx.base).ir,
      tx.plan,
      "model:commit",
    );
  }
  checkEdit(
    p: Principal,
    model: string,
    base: ModelIR,
    plan: any,
    scope = "model:edit",
  ) {
    const { grant } = this.context(p, model, scope);
    if (!grant || grant.spec.edit_scope.kind === "model") return;
    const allowed = new Set<string>(grant.spec.edit_scope.feature_ids);
    const before = compile(base),
      old = new Map(before.features.map((f) => [f.id, f]));
    const dirty = new Set<string>([
      ...base.features
        .filter((f) => !plan.ir.features.some((g: any) => g.id === f.id))
        .map((f) => f.id),
      ...plan.features
        .filter(
          (f: any) =>
            !old.has(f.id) ||
            f.cache_key !== old.get(f.id)!.cache_key ||
            hash(f) !== hash(old.get(f.id)),
        )
        .map((f: any) => f.id),
    ]);
    requireThat(
      [...dirty].every((fid) => allowed.has(fid)) &&
        hash({ ...base, features: [] }) === hash({ ...plan.ir, features: [] }),
      "NEEDS_APPROVAL",
      "Änderung oder abhängige Geometrie überschreitet die genehmigten Features oder Projektgrenzen.",
    );
  }
  bindTransaction(p: Principal, model: string, tx: string) {
    const { grant } = this.context(p, model, "model:edit");
    this.store.run(
      "INSERT INTO transaction_authorizations VALUES(?,?,?)",
      tx,
      grant?.id ?? null,
      grant?.version ?? null,
    );
  }
  checkTransaction(tx: any) {
    const a = this.store.get(
      "SELECT * FROM transaction_authorizations WHERE tx=?",
      tx.id,
    );
    if (!a?.grant_id) return;
    const grant = this.currentGrant(tx.model, tx.owner);
    requireThat(
      grant &&
        grant.id === a.grant_id &&
        grant.version === a.grant_version &&
        ROLE_SCOPES[grant.spec.role as keyof typeof ROLE_SCOPES].includes(
          "model:edit",
        ),
      "ACCESS_DENIED",
      "Die Erlaubnis für diesen Kandidaten ist abgelaufen oder widerrufen.",
    );
  }
  reserveJob(p: Principal, model: string, job: string, kind: string) {
    const scope = JOB_SCOPES[kind];
    requireThat(scope, "OUT_OF_SCOPE", "Nicht registrierter Jobtyp.");
    const { grant } = this.context(p, model, scope);
    if (grant)
      requireThat(
        this.usedJobs(grant.id, grant.version) < grant.spec.budget.jobs,
        "NEEDS_APPROVAL",
        "Das genehmigte Projektbudget ist ausgeschöpft.",
      );
    if (grant)
      this.store.run(
        "INSERT INTO grant_actions VALUES(?,?,?,?)",
        job,
        grant.id,
        grant.version,
        kind,
      );
    this.store.run(
      "INSERT INTO job_authorizations VALUES(?,?,?,?,?)",
      job,
      grant?.id ?? null,
      grant?.version ?? null,
      scope,
      grant?.spec.budget.seconds_per_job ?? LIMITS.job_seconds,
    );
  }
  reserveIRExport(p: Principal, model: string) {
    const { grant } = this.context(p, model, "model:export");
    if (!grant) return;
    requireThat(
      this.usedJobs(grant.id, grant.version) < grant.spec.budget.jobs,
      "NEEDS_APPROVAL",
      "Das genehmigte Projektbudget ist ausgeschöpft.",
    );
    this.store.run(
      "INSERT INTO grant_actions VALUES(?,?,?,?)",
      id("export"),
      grant.id,
      grant.version,
      "ir_export",
    );
  }
  checkJob(job: any) {
    const scope = JOB_SCOPES[job.kind];
    requireThat(scope, "OUT_OF_SCOPE", "Nicht registrierter Jobtyp.");
    const p: Principal = {
      tenant: job.tenant,
      user: job.owner,
      scopes: ["model:read", scope],
    };
    const { grant } = this.context(p, job.model, scope);
    const bound = this.store.get(
      "SELECT * FROM job_authorizations WHERE job=?",
      job.id,
    );
    requireThat(
      !grant
        ? !bound?.grant_id
        : bound &&
            bound.grant_id === grant.id &&
            bound.grant_version === grant.version &&
            bound.scope === scope,
      "ACCESS_DENIED",
      "Jobfreigabe ist nicht mehr aktuell.",
    );
    if (job.tx) this.checkTransaction(this.store.transaction(p, job.tx));
    return {
      principal: p,
      seconds: bound?.seconds ?? LIMITS.job_seconds,
      expires: grant?.expires ?? null,
    };
  }
}
