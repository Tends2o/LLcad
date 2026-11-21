import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { join, resolve } from "node:path";
import type { ModelService } from "../model-service/index.js";
import type { Principal } from "../policy/index.js";
import { hash, id } from "../semantic-ir/hash.js";
import { imagePython } from "../semantic-ir/host.js";
import { CadError, requireThat, safeError } from "../semantic-ir/errors.js";
import { applyPatch } from "../compiler/index.js";
import { structureOf } from "../compiler/structure.js";
import { checkedToolResponse, failureResponse } from "../model-service/result-contracts.js";
import { readStoredPreview } from "../model-service/stored-preview.js";
import { fingerprint } from "../reference-review/evidence.js";
import { captureViews } from "../reference-review/capture.js";
import { routeFeature } from "./router.js";
import { executeFeature } from "./runtime.js";
import {
  checkOwnership,
  featureWorkGuard,
  issueWorkerAccess,
  revokeStaleWorkers,
  revokeWorkerAccess,
} from "./access.js";
import { closureOf, packWaves, waveConflicts } from "./merge.js";

const execFileAsync = promisify(execFile);
/** Helpers run without a console window (the service itself has none on Windows). */
const execute = ((file: string, args: string[], options: any) =>
  execFileAsync(file, args, { windowsHide: true, ...options })) as typeof execFileAsync;
const COVERAGE = { silhouette: .25, proportions: .25, dominant_colours: .2, main_components: .2, orientation: .1 };
export function coarseCoverage(mvp: Record<string, unknown>) {
  return Object.entries(COVERAGE).reduce((n, [k, weight]) => n + (mvp[k] === true ? weight : 0), 0);
}
/** Where validated workers reach the llcad MCP: LLCAD_WORKER_MCP_URL (off disables), else
 *  MATHFORGE_PUBLIC_URL + /mcp. Without it workers are read-only proposers. */
export function workerMcpUrl() {
  const explicit = process.env.LLCAD_WORKER_MCP_URL;
  if (explicit) return /^(off|0|none|false)$/i.test(explicit) ? null : explicit;
  const base = process.env.MATHFORGE_PUBLIC_URL;
  return base ? base.replace(/\/+$/, "") + "/mcp" : null;
}
/** Messages of our own runtime/router errors are fixed texts without secrets: keep them. */
function taskError(error: unknown) {
  if (!(error instanceof CadError) && error instanceof Error && /^(Feature worker|Jev|Unknown worker|Unsupported orchestrator|Invalid feature worker)/.test(error.message))
    return error.message.slice(0, 1000);
  const safe = safeError(error);
  return `${safe.code}: ${safe.message}`.slice(0, 1000);
}
/** Workers propose local edits against one frozen base; only the parent commits. */
export function validateProposal(task: any, proposal: any, revision: string, baseIR: any, options: { lanes?: boolean } = {}) {
  requireThat(proposal.task_id === task.id && proposal.base_revision === revision, "STALE_REVISION", "The feature proposal does not belong to the frozen task/revision.");
  return checkOwnership(task, proposal.operations, baseIR, { lanes: options.lanes, max: 64 });
}

export class FeatureBatches {
  private active = new Map<string, { controller: AbortController; done: Promise<void> }>();
  /** Whether a feature batch is in progress. */
  busy() {
    return this.active.size > 0;
  }
  /** Viewer captures for crops run one at a time (Chromium next to OCCT and workers). */
  private captureQueue: Promise<unknown> = Promise.resolve();
  constructor(private service: ModelService, private deps = { routeFeature, executeFeature }) {
    service.store.run("CREATE TABLE IF NOT EXISTS feature_batches(id TEXT PRIMARY KEY,model TEXT NOT NULL REFERENCES models(id) ON DELETE CASCADE,record TEXT NOT NULL)");
    for (const row of service.store.all("SELECT id,record FROM feature_batches WHERE json_extract(record,'$.status') IN ('queued','running')")) {
      const r = JSON.parse(row.record); r.status = "interrupted"; r.stage = "interrupted"; r.error = "Service restart: no automatic billable retries."; r.finished_at = Date.now(); this.save(r);
    }
    try { revokeStaleWorkers(service); } catch { /* a fresh store has no grants yet */ }
  }
  /** ModelService.callInner calls this after schema parsing for every tool call: worker
   *  principals are confined, patch_ref to batch artifacts is rebase-checked. */
  guard(p: Principal, tool: string, args: any) { featureWorkGuard(this.service, p, tool, args); }
  private save(r: any) { this.service.store.run("UPDATE feature_batches SET record=? WHERE id=?", JSON.stringify(r), r.id); }
  start(p: Principal, a: any) {
    const store = this.service.store, m = store.model(p, a.model_id, "model:edit"), rev = store.revision(p, a.model_id, a.base_revision);
    requireThat(m.head === rev.id, "STALE_REVISION", "Feature tasks need the current MVP state.");
    requireThat(rev.ir.outputs.length && rev.geometry?.aggregate?.valid === true, "MVP_REQUIRED", "Create a visible, geometrically valid MVP first.");
    const coverage = coarseCoverage(a.mvp);
    requireThat(a.mvp.silhouette && a.mvp.proportions && coverage >= .5, "MVP_REQUIRED", "Check the basic shape and proportions in the viewer first; at least 50 % of the weighted coarse criteria must be supported.");
    requireThat(this.active.size === 0, "BUDGET_EXCEEDED", "A feature batch is already running; wait for its result with cad_feature_batch_report.");
    const row = store.get("SELECT record FROM reference_runs WHERE id=? AND model=?", a.inspection_run_id, a.model_id);
    const inspected = row && JSON.parse(row.record);
    const contractRow = store.get("SELECT contract FROM reference_contracts WHERE model=?", a.model_id), contract = contractRow && JSON.parse(contractRow.contract);
    requireThat(inspected?.phase === "inspect" && inspected.state === "completed" && inspected.revision === rev.id && contract && hash(inspected.binding) === hash(fingerprint(store, p, a.model_id, rev.id, contract.hash)), "MVP_INSPECTION_REQUIRED", "Look at the current MVP revision with phase inspect before distributing details.");
    const byId = new Map<string, any>(rev.ir.features.map((f: any) => [f.id, f])), owners = new Map<string, string>();
    const parts = new Set<string>(structureOf(rev.ir).parts.map((part: any) => part.id)), partOwners = new Map<string, string>();
    requireThat(new Set(a.tasks.map((t: any) => t.id)).size === a.tasks.length, "INVALID_SCHEMA", "Task ids must be unique.");
    for (const task of a.tasks) {
      const ownedParts: string[] = task.owned_part_ids ?? [];
      requireThat(!ownedParts.length || a.lanes === true, "INVALID_SCHEMA", "owned_part_ids exist only for lanes (lanes: true).");
      requireThat(task.owned_feature_ids.length + task.context_feature_ids.length + ownedParts.length > 0, "INVALID_SCHEMA", "A feature task needs at least one assigned or context feature (lanes: or an own part).");
      for (const fid of [...task.owned_feature_ids, ...task.context_feature_ids]) requireThat(byId.has(fid), "NOT_FOUND", "Unknown task feature.", { feature_id: fid });
      for (const fid of task.owned_feature_ids) { requireThat(!owners.has(fid), "CONSTRAINT_CONFLICT", "Parallel workers must not edit the same feature."); owners.set(fid, task.id); }
      for (const part of ownedParts) {
        requireThat(parts.has(part), "NOT_FOUND", "Unknown lane part.", { part_id: part });
        requireThat(!partOwners.has(part), "CONSTRAINT_CONFLICT", "Two lanes must not own the same part.", { part_id: part });
        partOwners.set(part, task.id);
      }
      requireThat(task.reference_ids.every((rid: string) => inspected.images.some((i: any) => i.id === rid && i.kind === "reference")) && task.render_ids.every((rid: string) => inspected.images.some((i: any) => i.id === rid && i.kind === "render")), "INVALID_SCHEMA", "The task needs existing original and viewer images from the MVP inspection run.");
    }
    for (const task of a.tasks) {
      const seen = new Set<string>();
      const visit = (fid: string) => { if (seen.has(fid)) return; seen.add(fid); requireThat(!owners.has(fid) || owners.get(fid) === task.id, "CONSTRAINT_CONFLICT", "Dependent feature changes must be made one after another."); for (const dep of byId.get(fid)?.depends_on ?? []) visit(dep); };
      task.owned_feature_ids.forEach(visit);
    }
    const url = workerMcpUrl();
    const validation = a.validation ?? (url ? "worker_candidate" : "none");
    requireThat(validation === "none" || url, "OUT_OF_SCOPE", "Validated workers need the MCP address of the service (MATHFORGE_PUBLIC_URL or LLCAD_WORKER_MCP_URL); validation: \"none\" uses proposal-only workers.");
    const warnings: string[] = [];
    if (!a.validation && validation === "none") warnings.push("validation none: the service has no MCP URL for workers; proposals are unvalidated");
    // LLCAD_FEATURE_MAX_PARALLEL caps the request for the host (each CLI worker ~0.4 GB).
    const cap = Math.min(Math.max(Math.trunc(Number(process.env.LLCAD_FEATURE_MAX_PARALLEL)) || 4, 1), 4);
    const maxParallel = Math.min(a.max_parallel ?? 2, a.tasks.length, cap);
    const run = { id: id("features"), kind: a.lanes ? "lanes" : "feature_batch", model: a.model_id, revision: rev.id, binding: inspected.binding, request: a, status: "queued", stage: "queued", started_at: Date.now(), mvp_check_coverage: coverage, max_parallel: maxParallel, validation, lanes: a.lanes === true,
      tasks: a.tasks.map((t: any) => ({ id: t.id, title: t.title, state: "queued", model: null, effort: null, summary: null, proposal_artifact_id: null, elapsed_ms: null, error: null })), proposal_artifact_id: null, error: null, merge: { tasks: {}, waves: [] } };
    store.run("INSERT INTO feature_batches(id,model,record) VALUES(?,?,?)", run.id, run.model, JSON.stringify(run));
    const controller = new AbortController();
    const done = new Promise<void>(resolve => setImmediate(() => { void this.execute(p, run, rev, inspected, controller.signal).finally(resolve); }));
    this.active.set(run.id, { controller, done }); void done.finally(() => this.active.delete(run.id));
    return { model_id: run.model, base_revision: rev.id, batch_id: run.id, status: "queued", max_parallel: maxParallel, validation, lanes: run.lanes, ...(warnings.length ? { warnings } : {}) };
  }
  report(p: Principal, a: any) {
    const store = this.service.store;
    store.model(p, a.model_id);
    const row = store.get("SELECT record FROM feature_batches WHERE id=? AND model=?", a.batch_id, a.model_id);
    requireThat(row, "NOT_FOUND", "Feature batch not found."); const r = JSON.parse(row.record);
    const waves = (r.merge?.waves ?? []).filter((w: any) => w.artifact_id);
    let listed: any[] | undefined;
    if (waves.length) {
      // Applicability against the current head (read-only): the rebase check the
      // patch_ref guard repeats when a wave is applied.
      const head = store.model(p, r.model).head;
      const base = store.revision(p, r.model, r.revision), current = head === r.revision ? base : store.revision(p, r.model, head);
      listed = waves.map((w: any) => {
        const conflicts = head === r.revision ? (w.wave === 1 ? [] : [`wave ${w.wave}: apply waves 1..${w.wave - 1} first`]) : waveConflicts(r, w.task_ids, base.ir, current.ir, w.wave);
        return { wave: w.wave, task_ids: w.task_ids, operations: w.operations, patch_artifact_id: w.artifact_id, applicable: conflicts.length === 0, ...(conflicts.length ? { conflicts: conflicts.slice(0, 8) } : {}) };
      });
    }
    const next = listed?.find((w) => w.applicable);
    return { model_id: r.model, base_revision: r.revision, batch_id: r.id, status: r.status, stage: r.stage, max_parallel: r.max_parallel ?? 2, mvp_check_coverage: r.mvp_check_coverage,
      coverage_basis: "orchestrator_declared_coarse_checks_not_visual_accuracy", elapsed_ms: (r.finished_at ?? Date.now()) - r.started_at, proposal_artifact_id: r.proposal_artifact_id, tasks: r.tasks,
      ...(r.validation ? { validation: r.validation } : {}), ...(r.lanes !== undefined ? { lanes: r.lanes } : {}), ...(listed ? { waves: listed } : {}),
      ...(next ? { next_action: `Review the task crops and summaries, then cad_apply_patch {model_id, base_revision: <head>, idempotency_key: <new>, patch_ref: {artifact_id: "${next.patch_artifact_id}"}, validate: true, commit: "if_valid", wait_ms: 100000}; after the commit look at the changed views and apply the next wave.` } : {}),
      error: r.error, applied: false };
  }
  /** cad_feature_batch_report with wait_ms: waits server-side until the batch is terminal. */
  async waitReport(p: Principal, a: any, trace: string) {
    try {
      const deadline = Date.now() + Math.min(a.wait_ms ?? 0, 110_000);
      for (;;) {
        const result = this.report(p, a);
        if (!["queued", "running"].includes(result.status) || Date.now() >= deadline) return checkedToolResponse("cad_feature_batch_report" as any, result, trace);
        await new Promise((done) => setTimeout(done, Math.min(1000, Math.max(deadline - Date.now(), 10))));
      }
    } catch (error) {
      return failureResponse(error, trace);
    }
  }
  private async execute(p: Principal, run: any, rev: any, inspection: any, parent: AbortSignal) {
    const store = this.service.store, request = run.request, reports: any[] = [], entries: { task_id: string; operations: any[] }[] = [];
    const validated = run.validation === "worker_candidate", url = workerMcpUrl();
    const perTask = request.timeout_seconds + 65 + (validated ? 120 : 0);
    const signal = AbortSignal.any([parent, AbortSignal.timeout(Math.ceil(request.tasks.length / run.max_parallel) * perTask * 1000)]);
    const folder = join(store.root, "feature-work", run.id); mkdirSync(folder, { recursive: true, mode: 0o700 });
    const saveArtifact = (data: any, filename: string, manifest: Record<string, unknown> = {}) => store.artifact(p, Buffer.from(JSON.stringify(data, null, 2)), "application/json", run.model, run.revision, { filename, kind: "feature_proposal", batch_id: run.id, ...manifest }).artifact_id;
    try {
      run.status = "running"; run.stage = "parallel_features"; this.save(run);
      let next = 0;
      const worker = async () => {
        while (next < request.tasks.length) {
          signal.throwIfAborted(); store.model(p, run.model, "model:edit");
          const index = next++, task = request.tasks[index], item = run.tasks[index];
          let access: ReturnType<typeof issueWorkerAccess> | null = null;
          try {
            item.state = "routing"; this.save(run);
            const route = await this.deps.routeFeature({ orchestratorModel: request.orchestrator_model, worker: request.worker, task: { id: task.id, title: task.title, description: task.description, kind: task.kind, effort: task.effort, ownedFeatureIds: task.owned_feature_ids, evidence: task.evidence }, signal });
            item.model = route.model; item.effort = route.effort; if (route.effort_source) item.effort_source = route.effort_source; item.state = "running"; this.save(run);
            const directory = join(folder, task.id); mkdirSync(directory, { recursive: true, mode: 0o700 });
            const images: string[] = [];
            for (const imageId of [...task.reference_ids, ...task.render_ids]) {
              const info = inspection.images.find((i: any) => i.id === imageId), a = store.getArtifact(p, info.artifact_id), path = join(directory, imageId + ".png");
              const bytes = store.readBlob(a.hash); requireThat(bytes.length <= 16 * 1024 * 1024 && bytes.subarray(0,8).equals(Buffer.from([137,80,78,71,13,10,26,10])), "INTEGRITY_FAILURE", "The task image must be a bounded PNG capture.");
              writeFileSync(path, bytes, { mode: 0o600 }); images.push(path);
            }
            const relevant = closureOf(rev.ir, [...task.owned_feature_ids, ...task.context_feature_ids]);
            requireThat(relevant.size <= 256, "BUDGET_EXCEEDED", "The feature context is too large; form a smaller independent task.");
            const structure = run.lanes ? structureOf(rev.ir) : null;
            const packet = { task_id: task.id, base_revision: run.revision, model_id: run.model, task, construction_context: rev.ir.features.filter((f: any) => relevant.has(f.id)),
              ...(structure ? { lane: { parts: structure.parts.filter((part: any) => (task.owned_part_ids ?? []).includes(part.id)).map((part: any) => ({ ...part, outputs_hash: hash(part.outputs ?? []) })), frames: structure.frames, id_rules: `features ${task.id}-…, new parts pt-${task.id}-…, frames fr-${task.id}-…; set_part_outputs only on owned_part_ids or your new parts, own features only` } } : {}),
              instructions: validated
                ? `Build and validate your change as your own candidate (see the rules), then return exactly its operations. Never change global outputs/structure${run.lanes ? " beyond your lane parts" : ""}. Added IDs must begin with task_id + '-'. Include accurate expected values/hashes from the frozen context; state unresolved evidence. Prefer changing existing parameters over recreating geometry. For colour/finish-only work state concrete material recommendations in summary; parent applies colours. Use the original images and viewer details, not feature names, to decide shape/edge/inhomogeneous colour changes.`
                : "Return only a local CAD PatchOperation proposal. Never mutate the shared model, call MCP, spawn agents, or change global outputs/structure. Added IDs must begin with task_id + '-'. Include accurate expected values/hashes from the frozen context; state unresolved evidence. Prefer changing existing parameters over recreating geometry. For colour/finish-only work state concrete material recommendations in summary; parent applies colours. Use the original images and viewer details, not feature names, to decide shape/edge/inhomogeneous colour changes." };
            writeFileSync(join(directory, "packet.json"), JSON.stringify(packet), { mode: 0o600 });
            if (validated) access = issueWorkerAccess(this.service, p, { batch_id: run.id, task: { id: task.id, owned_feature_ids: task.owned_feature_ids, owned_part_ids: task.owned_part_ids }, model: run.model, base_revision: run.revision, base_ir: rev.ir, lanes: run.lanes, ttl_ms: (request.timeout_seconds + 120) * 1000 });
            const result = await this.deps.executeFeature({ route, packet, directory, images, signal, timeoutMs: request.timeout_seconds * 1000, ...(access && url ? { mcp: { url, token: access.token, tools: access.tools } } : {}) });
            const checked = validateProposal(task, result.proposal, run.revision, rev.ir, { lanes: run.lanes });
            const operations = checked.operations;
            let validation: any = { status: "not_requested" };
            let crops: any[] = [];
            if (access) {
              validation = this.verifyCandidate(p, run, rev, access.user, result.proposal.transaction_id ?? null, operations);
              if (validation.candidate_revision && validation.status !== "mismatch")
                crops = await this.crops(p, run, task, inspection, validation.candidate_revision, directory, signal).catch((e) => { validation.note = `crops unavailable: ${taskError(e)}`.slice(0, 300); return []; });
            }
            const parts = new Set<string>(operations.filter((op: any) => op.op === "set_part_outputs").map((op: any) => op.part_id).filter((part: string) => !checked.structure.includes(part)));
            run.merge.tasks[task.id] = { closure: [...relevant], created: checked.created, parts: [...parts] };
            const { operations: _raw, ...proposal } = result.proposal;
            const report = { model_id: run.model, frozen_base_revision: run.revision, batch_id: run.id, task_id: task.id, route, ...result, proposal, operations, validation, crops };
            item.proposal_artifact_id = saveArtifact(report, task.id + "-proposal.json", { task_ids: [task.id] });
            item.summary = result.proposal.summary.slice(0, 1000); item.elapsed_ms = result.elapsed_ms; item.operations = operations.length; item.state = "completed";
            if (access) item.validation = validation;
            if (crops.length) item.crops = crops;
            entries.push({ task_id: task.id, operations }); reports.push(report);
          } catch (e) { item.state = "failed"; item.error = taskError(e); }
          finally { if (access) { try { revokeWorkerAccess(this.service, run.model, access.user); } catch { /* grant already gone */ } } }
          this.save(run);
        }
      };
      await Promise.allSettled(Array.from({ length: run.max_parallel }, () => worker())); signal.throwIfAborted();
      store.model(p, run.model, "model:edit");
      const contractRow = store.get("SELECT contract FROM reference_contracts WHERE model=?", run.model), contract = contractRow && JSON.parse(contractRow.contract);
      if (store.model(p, run.model).head !== rev.id || !contract || hash(fingerprint(store, p, run.model, rev.id, contract.hash)) !== hash(run.binding)) { run.status = "stale"; run.error = "The model, material, references or viewer have changed in the meantime. Re-check the proposals before merging them."; }
      else if (run.tasks.some((t: any) => t.state === "failed")) { run.status = "failed"; run.error = "At least one subtask failed; valid individual proposals are kept."; }
      else run.status = "completed";
      // Merge by reference: task order, waves of <= 64 operations, one transaction each.
      const order = new Map<string, number>(request.tasks.map((t: any, i: number) => [t.id, i]));
      entries.sort((x, y) => order.get(x.task_id)! - order.get(y.task_id)!);
      const waves = packWaves(entries.filter((e) => e.operations.length), rev.ir);
      run.merge.waves = waves.map((w) => {
        const artifact = saveArtifact({ model_id: run.model, frozen_base_revision: run.revision, batch_id: run.id, wave: w.wave, of_waves: waves.length, task_ids: w.task_ids, operations: w.operations, applied: false,
          next_action: w.wave === 1 ? "cad_apply_patch {patch_ref: {artifact_id: <this>}, validate: true, commit: \"if_valid\"} on the frozen base" : `apply after wave ${w.wave - 1} is committed; the server re-checks that these tasks are untouched` }, `wave-${w.wave}.json`, { kind: "feature_wave", wave: w.wave, task_ids: w.task_ids });
        return { wave: w.wave, task_ids: w.task_ids, operations: w.operations.length, parts: w.parts, ...(w.model_outputs ? { model_outputs: w.model_outputs } : {}), artifact_id: artifact };
      });
      const single = waves.length === 1 ? waves[0].operations : waves.length === 0 ? [] : undefined;
      run.proposal_artifact_id = saveArtifact({ model_id: run.model, base_revision: rev.id, ...(single ? { operations: single } : {}), waves: run.merge.waves.map((w: any) => ({ wave: w.wave, task_ids: w.task_ids, operations: w.operations, artifact_id: w.artifact_id })), tasks: reports, status: run.status, applied: false,
        next_action: "Orchestrator reviews the proposals (validation, crops, material recommendations), then applies each wave by reference: cad_apply_patch {patch_ref: {artifact_id: <wave artifact>}, validate: true, commit: \"if_valid\"}, one transaction per wave, in order. Inspect affected views; full phase compare only for final review." }, "feature-batch-proposals.json", { task_ids: waves.length === 1 ? waves[0].task_ids : [] , ...(waves.length === 1 ? { kind: "feature_wave", wave: 1 } : {}) });
    } catch (error) { run.status = parent.aborted ? "interrupted" : "failed"; run.error = taskError(error); }
    finally { run.stage = run.status; run.finished_at = Date.now(); this.save(run); }
  }
  /** The worker's final candidate must be its own, on the frozen base, and built from
   *  exactly the returned operations (same IR); its validation state is reported. */
  private verifyCandidate(p: Principal, run: any, rev: any, user: string, transaction: string | null, operations: any[]) {
    if (!transaction) return { status: "not_validated", transaction_id: null, candidate_revision: null, note: "worker returned no candidate" };
    const tx = this.service.store.get("SELECT * FROM transactions WHERE id=?", transaction);
    if (!tx || tx.owner !== user || tx.model !== run.model || tx.base !== run.revision)
      return { status: "mismatch", transaction_id: transaction, candidate_revision: null, note: "transaction is not this worker's candidate on the frozen base" };
    let same = false;
    try {
      const plan = applyPatch(rev.ir, { model_id: run.model, base_revision: run.revision, idempotency_key: "feature-worker-check", operations } as any);
      same = hash(plan.ir) === hash(JSON.parse(tx.plan).ir);
    } catch { same = false; }
    if (!same) return { status: "mismatch", transaction_id: transaction, candidate_revision: tx.candidate, note: "returned operations do not rebuild the worker's candidate" };
    const result = tx.validation ? JSON.parse(tx.validation) : null;
    const failed = (result?.checks ?? []).filter((c: any) => c.status && !["passed", "ok", "pass"].includes(c.status)).slice(0, 8).map((c: any) => String(c.kind ?? c.method ?? c.name ?? c.id ?? "check").slice(0, 80));
    const status = tx.state === "validated" ? "passed" : tx.state === "validation_failed" ? "failed" : "not_validated";
    return { status, transaction_id: transaction, candidate_revision: tx.candidate, ...(result?.digest ? { digest: result.digest } : {}), ...(failed.length ? { failed_checks: failed } : {}) };
  }
  /** Before/after crops of a validated candidate in the task's inspection views: the
   *  "before" is the inspection render of the frozen base, the "after" a capture of the
   *  candidate with the same camera; both are cut to the changed region. */
  private async crops(p: Principal, run: any, task: any, inspection: any, candidate: string, directory: string, signal: AbortSignal) {
    if (/^(0|off|false)$/i.test(process.env.LLCAD_FEATURE_CROPS ?? "")) return [];
    const views = (task.render_ids as string[]).map((rid) => ({ image: inspection.images.find((i: any) => i.id === rid && i.kind === "render"), view: (inspection.views ?? []).find((v: any) => v.id === rid) })).filter((v) => v.image && v.view);
    if (!views.length) return [];
    const run1 = async () => {
      signal.throwIfAborted();
      const store = this.service.store;
      let preview: any = readStoredPreview(store, p, run.model, candidate);
      if (!preview) preview = await this.render(p, run.model, candidate, signal);
      const captured = await captureViews(this.service, p, run.model, candidate, preview, views.map((v) => ({ ...v.view, width: v.view.width ?? 1000, height: v.view.height ?? 1000 })), signal);
      const root = resolve(process.env.MATHFORGE_ROOT ?? process.cwd());
      const out: any[] = [];
      for (const image of captured.captures) {
        const view = views.find((v) => v.view.id === image.id)!;
        const before = join(directory, image.id + ".png"), after = join(directory, image.id + "-after.png"), prefix = join(directory, image.id + "-crop");
        writeFileSync(after, image.bytes, { mode: 0o600 });
        const { stdout } = await execute(imagePython(root), [join(root, "packages/feature-work/crops.py"), before, after, prefix], { signal, timeout: 30000, maxBuffer: 65536 });
        const info = JSON.parse(stdout);
        const save = (suffix: string) => store.artifact(p, readFileSync(`${prefix}-${suffix}.png`), "image/png", run.model, run.revision, { filename: `${task.id}-${image.id}-${suffix}.png`, kind: "feature_crop", batch_id: run.id, task_id: task.id, candidate_revision: candidate }).artifact_id;
        out.push({ view: view.view.id, before_artifact_id: save("before"), after_artifact_id: save("after"), pair_artifact_id: save("pair"), box_px: info.box_px, changed_fraction: info.changed_fraction });
      }
      return out;
    };
    const queued = this.captureQueue.then(run1, run1);
    this.captureQueue = queued.catch(() => undefined);
    return queued;
  }
  private async render(p: Principal, model: string, revision: string, signal: AbortSignal) {
    const started = await this.service.call(p, "cad_render", { model_id: model, revision, deflection: { value: "0.02", unit: "mm" }, idempotency_key: id("feature-crop-render") });
    requireThat(started.status !== "failed", "KERNEL_FAILURE", "Preview of the worker candidate failed.", { errors: started.errors });
    if (!started.job_id) return started;
    const deadline = Date.now() + 150_000;
    while (Date.now() < deadline) {
      signal.throwIfAborted();
      const job = this.service.jobs.get(p, started.job_id);
      if (job.status === "succeeded") return job.result;
      requireThat(!["failed", "cancelled"].includes(job.status), "KERNEL_FAILURE", "Preview of the worker candidate failed.");
      await new Promise((r) => setTimeout(r, 200));
    }
    throw new CadError("KERNEL_FAILURE", "Preview of the worker candidate took too long.");
  }
  async close() { for (const r of this.active.values()) r.controller.abort(); await Promise.allSettled([...this.active.values()].map(r => r.done)); }
}
