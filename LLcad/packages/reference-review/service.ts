import { z } from "zod";
import { mkdirSync, readFileSync, writeFileSync, rmSync } from "node:fs";
import { join, resolve } from "node:path";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import type { ModelService } from "../model-service/index.js";
import type { Principal } from "../policy/index.js";
import { hash, id } from "../semantic-ir/hash.js";
import { imagePython } from "../semantic-ir/host.js";
import { requireThat, safeError } from "../semantic-ir/errors.js";
import {
  Contract,
  Start,
  VisionInventory,
  VisionComparison,
  comparisonSchema,
  type Summary,
  BASE_SCOPE,
} from "./schema.js";
import { fingerprint, nativeFingerprint, referenceState, adjudicate, acceptanceOf, judgeOf } from "./evidence.js";
import {
  inspectImages,
  visionConfigured,
  withRetry,
  limiter,
  costLedger,
  type ReviewImage,
} from "./vision.js";
import { captureViews, DEFAULT_VIEWS, type CaptureView } from "./capture.js";
import { readStoredPreview } from "../model-service/stored-preview.js";
import { ToolSchemas } from "../semantic-ir/tool-registry.js";
import {
  judgeAvailable,
  judgeCall,
  callKey,
  viewSchema,
  pairSchema,
  viewPrompt,
  pairPrompt,
  evaluateItem,
  pairwiseVerdict,
  adjudicateGatesJudge,
  type JudgeConfig,
  type PreparedView,
  type Evidence,
} from "./judge.js";

/** Parallel vision calls of the two-model route (1–3). */
const VISION_PARALLEL = Math.max(1, Math.min(3, Number(process.env.LLCAD_VISION_PARALLEL ?? 2) || 2));
const PNG_MAGIC = Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]);

const execFileAsync = promisify(execFile);
/** Helpers run without a console window (the service itself has none on Windows). */
const execute = ((file: string, args: string[], options: any) =>
  execFileAsync(file, args, { windowsHide: true, ...options })) as typeof execFileAsync;
export class ReferenceReviews {
  private running = new Map<
    string,
    { controller: AbortController; done: Promise<void> }
  >();
  /** Whether a review run is in progress. */
  busy() {
    return this.running.size > 0;
  }
  constructor(private service: ModelService) {
    const store = service.store;
    store.run(
      "CREATE TABLE IF NOT EXISTS reference_contracts(model TEXT PRIMARY KEY REFERENCES models(id) ON DELETE CASCADE,contract TEXT NOT NULL)",
    );
    store.run(
      "CREATE TABLE IF NOT EXISTS reference_runs(id TEXT PRIMARY KEY,model TEXT NOT NULL REFERENCES models(id) ON DELETE CASCADE,tenant TEXT NOT NULL,owner TEXT NOT NULL,phase TEXT NOT NULL,fingerprint TEXT NOT NULL,created TEXT NOT NULL,record TEXT NOT NULL)",
    );
    store.run(
      "CREATE INDEX IF NOT EXISTS reference_runs_binding ON reference_runs(model,fingerprint,phase,created)",
    );
    for (const row of store.all(
      "SELECT id,record FROM reference_runs WHERE json_extract(record,'$.state') IN ('queued','running')",
    )) {
      const run = JSON.parse(row.record);
      run.state = "failed";
      run.stage = "failed";
      run.finished_at = new Date().toISOString();
      run.error = {
        code: "REVIEW_INTERRUPTED",
        message:
          "Review interrupted by a service restart. No passed result.",
        details: {},
      };
      this.save(run);
    }
  }
  private save(run: any) {
    this.service.store.run(
      "UPDATE reference_runs SET record=? WHERE id=?",
      JSON.stringify(run),
      run.id,
    );
  }
  private contract(model: string): Contract | null {
    const row = this.service.store.get(
      "SELECT contract FROM reference_contracts WHERE model=?",
      model,
    );
    return row ? Contract.parse(JSON.parse(row.contract)) : null;
  }
  private freeze(body: any) {
    const { hash: _, ...rest } = body;
    return Contract.parse({ ...rest, hash: hash(rest) });
  }
  setup(p: Principal, a: any) {
    const store = this.service.store,
      m = store.model(p, a.model_id, "model:edit");
    requireThat(
      m.head === a.base_revision,
      "STALE_REVISION",
      "The reference contract must be set up on the current model state.",
    );
    const previous = this.contract(a.model_id);
    requireThat(
      (previous?.hash ?? null) === a.expected_contract_hash,
      "STALE_REVISION",
      "The reference contract has changed in the meantime.",
    );
    requireThat(
      new Set(a.references.map((r: any) => r.id)).size === a.references.length,
      "INVALID_SCHEMA",
      "Reference ids must be unique.",
    );
    requireThat(
      new Set(a.requirements.map((r: any) => r.id)).size ===
        a.requirements.length,
      "INVALID_SCHEMA",
      "Check criteria must have unique ids.",
    );
    requireThat(
      a.references.every(
        (r: any) =>
          r.id.length <= 80 &&
          !["front", "back", "left", "right", "top", "bottom"].includes(r.id) &&
          !r.id.startsWith("registered-") &&
          !r.id.startsWith("difference-") &&
          !r.id.startsWith("ids-") &&
          !r.id.startsWith("albedo-"),
      ),
      "INVALID_SCHEMA",
      "Reference ids must have at most 80 characters and must not use reserved capture ids.",
    );
    const requirementIds = new Set(a.requirements.map((r: any) => r.id));
    requireThat(
      (a.accepted_assumptions ?? []).every((x: any) => requirementIds.has(x.requirement_id)) &&
        new Set((a.accepted_assumptions ?? []).map((x: any) => x.requirement_id)).size ===
          (a.accepted_assumptions ?? []).length,
      "INVALID_SCHEMA",
      "Accepted assumptions must refer unambiguously to existing check criteria.",
    );
    requireThat(
      a.requirements.every((r: any) => !r.region || (r.reference_ids.includes(r.region.reference_id) && refsHave(r.region.reference_id))),
      "INVALID_SCHEMA",
      "A criterion region must lie in one of the criterion's references.",
    );
    function refsHave(id: string) {
      return a.references.some((r: any) => r.id === id);
    }
    requireThat(
      a.references.every((r: any) => !r.registered || !!r.camera),
      "INVALID_SCHEMA",
      "Registered pixel comparisons need an explicit camera.",
    );
    const refs = new Set(a.references.map((r: any) => r.id));
    requireThat(
      a.requirements.every((r: any) =>
        r.reference_ids.every((x: string) => refs.has(x)),
      ),
      "INVALID_SCHEMA",
      "A check criterion refers to an unknown reference.",
    );
    requireThat(
      new Set(a.models).size === a.models.length,
      "INVALID_SCHEMA",
      "Review models must differ.",
    );
    const reference_hashes = Object.fromEntries(
      a.references.map((r: any) => {
        const artifact = store.getArtifact(p, r.artifact_id);
        requireThat(
          artifact.size <= 16 * 1024 * 1024,
          "BUDGET_EXCEEDED",
          "The reference image is larger than 16 MiB.",
        );
        store.bindArtifact(p, r.artifact_id, a.model_id);
        return [r.id, artifact.hash];
      }),
    );
    const { base_revision, idempotency_key, expected_contract_hash, ...body } =
      a;
    const contract = this.freeze({
      ...body,
      schema_version: "1",
      reference_hashes,
      created_at: new Date().toISOString(),
      inventory_runs: [],
    });
    store.run(
      "INSERT INTO reference_contracts(model,contract) VALUES(?,?) ON CONFLICT(model) DO UPDATE SET contract=excluded.contract",
      a.model_id,
      JSON.stringify(contract),
    );
    store.audit("reference_contract_set", {
      model_id: a.model_id,
      contract_hash: contract.hash,
      reference_count: a.references.length,
    });
    return {
      model_id: a.model_id,
      contract_hash: contract.hash,
      reference: referenceState(store, p, a.model_id, m.head).summary,
    };
  }
  start(p: Principal, a: any) {
    a = Start.parse(a);
    const store = this.service.store;
    store.model(p, a.model_id, "model:edit");
    const r = store.revision(p, a.model_id, a.revision),
      contract = this.contract(a.model_id);
    requireThat(
      contract && contract.hash === a.contract_hash,
      "STALE_REVISION",
      "The reference contract does not match the request.",
    );
    requireThat(
      a.phase === "inspect" ||
        (a.phase === "compare" && acceptanceOf(contract) === "gates_judge"
          ? judgeAvailable(judgeOf(contract))
          : visionConfigured()),
      "VISION_NOT_CONFIGURED",
      a.phase === "compare" && acceptanceOf(contract) === "gates_judge"
        ? "The judge is not available (claude CLI or OpenRouter key)."
        : "The vision provider is not set up.",
    );
    if (a.phase === "compare") {
      requireThat(
        contract.requirements.length > 0,
        "REFERENCE_INVENTORY_REQUIRED",
        "Create the detail inventory before the comparison.",
      );
      const featureIds = new Set(r.ir.features.map((f: any) => f.id));
      const referencedIds = [
        ...contract.dimensions.flatMap((d: any) => d.feature_id ? [d.feature_id] : []),
        ...contract.contacts.flatMap((c: any) => [c.feature_id, c.other_feature_id]),
      ];
      const missing = [...new Set(referencedIds.filter((fid: string) => !featureIds.has(fid)))];
      requireThat(!missing.length, "AMBIGUOUS_SELECTION", "Native reference checks must refer to features of the reviewed revision; after a replacement, update the reference contract.", { missing_feature_ids: missing });
    }
    requireThat(
      this.running.size < 1,
      "BUDGET_EXCEEDED",
      "An image review is already running; cad_reference_report shows its progress.",
    );
    const views = a.phase === "inspect" ? (a.views ?? [DEFAULT_VIEWS[0]]).map((view: any) => ({
      ...view, width: view.width ?? 1000, height: view.height ?? 1000,
    })) : null;
    requireThat(!views || views.every((view: any) => !contract.references.some((ref) => ref.id === view.id)),
      "INVALID_SCHEMA", "Capture ids must not overwrite reference ids.");
    const binding = fingerprint(store, p, a.model_id, r.id, contract.hash),
      fp = a.phase === "inspect" ? hash({ binding, phase: a.phase, views }) : hash(binding);
    const old = store.get(
      "SELECT record FROM reference_runs WHERE model=? AND fingerprint=? AND phase=? AND json_extract(record,'$.state')='completed' ORDER BY created DESC LIMIT 1",
      a.model_id,
      fp,
      a.phase,
    );
    if (old) {
      const run = JSON.parse(old.record);
      return {
        status: "ok",
        model_id: a.model_id,
        revision: r.id,
        run_id: run.id,
        state: run.state,
        contract_hash: contract.hash,
        cached: true,
      };
    }
    const run = {
      id: id("review"),
      model: a.model_id,
      revision: r.id,
      phase: a.phase,
      state: "queued",
      binding,
      native_fingerprint: a.phase === "inspect" ? null : nativeFingerprint(store, p, a.model_id, r.id, contract),
      views,
      contract,
      fingerprint: fp,
      created_at: new Date().toISOString(),
      stage: "queued",
      usage: [],
      findings: [],
      native_checks: [],
      images: [],
      limitations: [],
      summary: null,
      report_artifact_id: null,
    };
    store.run(
      "INSERT INTO reference_runs(id,model,tenant,owner,phase,fingerprint,created,record) VALUES(?,?,?,?,?,?,?,?)",
      run.id,
      a.model_id,
      p.tenant,
      p.user,
      a.phase,
      fp,
      run.created_at,
      JSON.stringify(run),
    );
    const controller = new AbortController();
    // Work starts after the tool's atomic idempotency record has been persisted.
    const done = new Promise<void>((resolve) =>
      setImmediate(() => {
        if (!store.get("SELECT id FROM reference_runs WHERE id=?", run.id)) {
          resolve();
          return;
        }
        void this.execute(p, contract, run, controller.signal).finally(resolve);
      }),
    );
    this.running.set(run.id, { controller, done });
    void done.finally(() => this.running.delete(run.id));
    return {
      status: "queued",
      model_id: a.model_id,
      revision: r.id,
      run_id: run.id,
      state: run.state,
      contract_hash: contract.hash,
      cached: false,
    };
  }
  report(p: Principal, a: any) {
    const store = this.service.store;
    store.model(p, a.model_id);
    const r = store.revision(p, a.model_id, a.revision),
      state = referenceState(store, p, a.model_id, r.id);
    let run = state.run;
    if (!a.run_id) {
      const active = store.get(
        "SELECT record FROM reference_runs WHERE model=? AND json_extract(record,'$.state') IN ('queued','running') ORDER BY created DESC LIMIT 1",
        a.model_id,
      );
      const latest =
        active ??
        (!run
          ? store.get(
              "SELECT record FROM reference_runs WHERE model=? ORDER BY created DESC LIMIT 1",
              a.model_id,
            )
          : null);
      if (latest) run = JSON.parse(latest.record);
    }
    if (a.run_id) {
      const row = store.get(
        "SELECT record FROM reference_runs WHERE id=? AND model=?",
        a.run_id,
        a.model_id,
      );
      requireThat(row, "NOT_FOUND", "Review run not found.");
      run = JSON.parse(row.record);
    }
    const checkedHash =
      run?.created_contract_hash ?? run?.binding?.contract_hash;
    const checkedContract =
      run?.created_contract ??
      (run?.created_contract_hash ? null : run?.contract) ??
      (!run || checkedHash === state.contract?.hash ? state.contract : null);
    const entries = run?.findings ?? [],
      requirements = checkedContract?.requirements ?? [],
      extra = (run?.reviews ?? []).flatMap((r: any) =>
        r.result.additional_issues.map((x: any) => ({ ...x, model: r.model })),
      );
    const page = (items: any[]) => items.slice(a.offset, a.offset + a.limit);
    const judged = run?.judge_findings ?? [],
      issues = run?.judge_issues ?? [],
      pairwise = run?.pairwise ?? [],
      assumptions = run?.assumptions ?? [];
    const total = Math.max(
      judged.length,
      issues.length,
      pairwise.length,
      assumptions.length,
      entries.length,
      requirements.length,
      checkedContract?.references.length ?? 0,
      checkedContract?.dimensions.length ?? 0,
      checkedContract?.contacts.length ?? 0,
      extra.length,
      run?.native_checks?.length ?? 0,
      run?.images?.length ?? 0,
      run?.limitations?.length ?? 0,
    );
    return {
      model_id: a.model_id,
      revision: r.id,
      reference: state.summary,
      checked_summary: run?.summary ?? null,
      report_artifact_id: run?.report_artifact_id ?? null,
      run_id: run?.id ?? null,
      state: run?.state ?? null,
      phase: run?.phase ?? null,
      views: run?.views ?? null,
      elapsed_ms: run?.elapsed_ms ?? null,
      stage: run?.stage ?? null,
      checked_revision: run?.revision ?? null,
      contract_hash: checkedHash ?? checkedContract?.hash ?? null,
      references: page(checkedContract?.references ?? []),
      dimensions: page(checkedContract?.dimensions ?? []),
      contacts: page(checkedContract?.contacts ?? []),
      configuration: checkedContract
        ? Object.fromEntries(
            [
              "models",
              "maximum_cost_usd",
              "minimum_confidence",
              "image_pixel_limit",
              "silhouette_iou_min",
              "edge_f1_min",
              "scope",
              // Present only on contracts frozen with the acceptance policy.
              ...["acceptance", "judge", "accepted_assumptions"].filter(
                (k) => checkedContract[k] !== undefined,
              ),
            ].map((k) => [k, checkedContract[k]]),
          )
        : null,
      requirements: page(requirements),
      findings: page(entries).map((f: any) => ({
        ...f,
        observations: f.observations.map((o: any) => ({
          ...o,
          evidence: o.evidence.slice(0, 500),
          correction: o.correction.slice(0, 300),
        })),
      })),
      additional_issues: page(extra),
      native_checks: page(run?.native_checks ?? []),
      images: page(run?.images ?? []),
      usage: run?.usage ?? [],
      limitations: page(run?.limitations ?? []),
      error: run?.error ? { details: {}, ...run.error } : null,
      next_offset: total > a.offset + a.limit ? a.offset + a.limit : null,
      total_items: total,
      vision_configured: visionConfigured(),
      ...(run?.acceptance === "gates_judge"
        ? {
            gates: run.gates ?? null,
            judge: run.judge ?? null,
            judge_findings: page(judged),
            judge_issues: page(issues),
            pairwise: page(pairwise),
            assumptions: page(assumptions),
          }
        : {}),
    };
  }
  private async tool(
    p: Principal,
    name: string,
    args: any,
    signal: AbortSignal,
  ) {
    signal.throwIfAborted();
    const result = await this.service.call(p, name as any, args);
    requireThat(
      result.status !== "failed",
      "REFERENCE_NATIVE_CHECK_FAILED",
      "The native CAD tool cancelled the check.",
      { tool: name, errors: result.errors },
    );
    if (!result.job_id) return result;
    try {
      const deadline = Date.now() + 150_000;
      while (Date.now() < deadline) {
        signal.throwIfAborted();
        const job = this.service.jobs.get(p, result.job_id);
        if (job.status === "succeeded") return job.result;
        requireThat(
          !["failed", "cancelled"].includes(job.status),
          "REFERENCE_NATIVE_CHECK_FAILED",
          "Native check job failed.",
        );
        await new Promise((r) => setTimeout(r, 150));
      }
      throw new Error("Native check timeout");
    } catch (error) {
      this.service.jobs.cancel(p, result.job_id);
      throw error;
    }
  }
  private async python(args: string[], signal: AbortSignal) {
    const root = resolve(process.env.MATHFORGE_ROOT ?? process.cwd());
    const { stdout } = await execute(
      imagePython(root),
      [join(root, "packages/reference-review/images.py"), ...args],
      { signal, timeout: 30000, maxBuffer: 65536 },
    );
    return JSON.parse(stdout);
  }
  private async execute(
    p: Principal,
    contract: Contract,
    run: any,
    parent: AbortSignal,
  ) {
    const startedAt = Date.now();
    const acceptance = run.phase === "compare" ? acceptanceOf(contract) : null;
    if (acceptance) run.acceptance = acceptance;
    // The judge route adds capture channels and judge calls: 20 instead of 10 minutes.
    const signal = AbortSignal.any([
        parent,
        AbortSignal.timeout(acceptance === "gates_judge" ? 1_200_000 : 600_000),
      ]),
      store = this.service.store;
    const folder = join(store.root, "reference-work", run.id);
    mkdirSync(folder, { recursive: true, mode: 0o700 });
    const stage = (s: string) => {
      signal.throwIfAborted();
      store.model(p, run.model, "model:edit");
      requireThat(
        this.contract(run.model)?.hash === contract.hash,
        "STALE_REVISION",
        "The reference contract was changed during the review.",
      );
      run.state = "running";
      run.stage = s;
      this.save(run);
    };
    const artifact = (data: Buffer, mime: string, name: string, kind: string) =>
      store.artifact(p, data, mime, run.model, run.revision, {
        filename: name,
        kind,
        reference_contract_hash: contract.hash,
        review_run: run.id,
      });
    try {
      stage("reference_images");
      const references: ReviewImage[] = [];
      for (const ref of contract.references) {
        const source = store.getArtifact(p, ref.artifact_id);
        requireThat(
          source.hash === contract.reference_hashes[ref.id],
          "INTEGRITY_FAILURE",
          "The reference image was replaced.",
        );
        const src = join(folder, ref.id + ".source"),
          dst = join(folder, ref.id + ".png");
        writeFileSync(src, store.readBlob(source.hash), { mode: 0o600 });
        const meta = await this.python(
          ["normalize", src, dst, String(contract.image_pixel_limit)],
          signal,
        );
        const bytes = readFileSync(dst);
        references.push({ id: ref.id, label: ref.label, bytes, ...meta });
        const a = artifact(
          bytes,
          "image/png",
          ref.id + ".png",
          "reference_image",
        );
        run.images.push({
          id: ref.id,
          label: ref.label,
          artifact_id: a.artifact_id,
          kind: "reference",
        });
      }
      const spent = () => run.usage.reduce((n: number, u: any) => n + u.cost_usd, 0);
      const remaining = () => contract.maximum_cost_usd - spent();
      // Parallel calls reserve their preflight cost here, so together they stay in budget.
      const ledger = costLedger(contract.maximum_cost_usd, spent);
      const onUsage = (usage: any) => {
        run.usage.push(usage);
        this.save(run);
      };
      if (run.phase === "inspect") {
        stage("viewer_capture");
        let preview: any = readStoredPreview(store, p, run.model, run.revision);
        if (!preview) preview = await this.tool(p, "cad_render", {
          model_id: run.model, revision: run.revision,
          deflection: { value: "0.02", unit: "mm" },
          idempotency_key: id("inspect-render"),
        }, signal);
        const captured = await captureViews(this.service, p, run.model, run.revision, preview, run.views, signal);
        for (const image of captured.captures) {
          requireThat(image.bytes.length >= 24 && image.bytes.subarray(0, 8).equals(Buffer.from([137,80,78,71,13,10,26,10])) &&
            image.bytes.readUInt32BE(16) === image.width && image.bytes.readUInt32BE(20) === image.height,
            "INTEGRITY_FAILURE", "The viewer capture is not a PNG with the requested dimensions.");
          const saved = artifact(image.bytes, "image/png", image.id + ".png", "viewer_capture");
          run.images.push({ id: image.id, label: image.label, artifact_id: saved.artifact_id, kind: "render" });
        }
        run.limitations.push("Draft visual inspection only. No vision verdict or native dimension/contact checks; this is not a complete reference review, export proof, or replacement for final review.");
        stage("inspection_complete");
        requireThat(hash({ binding: fingerprint(store, p, run.model, run.revision, contract.hash), phase: "inspect", views: run.views }) === run.fingerprint,
          "STALE_REVISION", "Geometry, material appearance or viewer code changed during the capture.");
      } else if (run.phase === "inventory") {
        const merged = [...contract.requirements];
        const inventories = [];
        const batches = Math.ceil(references.length / 3);
        const perBatch = Math.floor(
          (128 - merged.length) / (batches * contract.models.length),
        );
        requireThat(
          perBatch >= 4,
          "BUDGET_EXCEEDED",
          "Too few free features for an independent detail inventory; create a new reference contract.",
        );
        // Independent batches run in parallel (bounded), each retried once when
        // its answer is incomplete or invalid; results merge in a fixed order.
        const tasks = contract.models.flatMap((model, index) => {
          const list = [];
          for (let offset = 0; offset < references.length; offset += 3)
            list.push({ model, index, offset, batch: references.slice(offset, offset + 3) });
          return list;
        });
        const run2 = limiter(VISION_PARALLEL);
        const answers = await Promise.all(
          tasks.map((task) =>
            run2(async () => {
              stage("inventory:" + task.model + ":" + task.offset);
              return withRetry(
                () =>
                  inspectImages({
                    model: task.model,
                    schema: VisionInventory,
                    images: task.batch,
                    remainingUsd: ledger.remaining(),
                    onReserve: ledger.reserve,
                    signal,
                    onUsage,
                    prompt:
                      `Return at most ${perBatch} detailed requirements for this image batch, with descriptions under 250 characters and short IDs. Cover visible features systematically within that bound; record omitted or unresolvable evidence in limitations. ` +
                      "Build a visible-detail inventory BEFORE CAD modelling. Inspect the original reference images independently, including contours, curvature changes, seams, text, textures, optical appearance, openings, flushness, small controls and their relative placement. Split compound requirements into testable details. Include every category supported by these images. Requirement reference_ids must name the supplied image IDs. Do not infer hidden engineering. Include limitations for unseen sides or illegible markings. Required checks should be true. Scope: " +
                      contract.scope,
                  }),
                { signal, onRetry: (code) => (run.retries ??= []).push({ stage: "inventory:" + task.model + ":" + task.offset, code }) },
              );
            }),
          ),
        );
        for (const [n, task] of tasks.entries()) {
          const answer = answers[n];
          requireThat(
            answer.result.requirements.length <= perBatch,
            "VISION_INVALID_RESULT",
            "The inventory exceeds the given partial budget.",
          );
          inventories.push({ model: task.model, ...answer });
          for (const item of answer.result.requirements) {
            requireThat(
              item.reference_ids.every((ref) =>
                references.some((r) => r.id === ref),
              ),
              "VISION_INVALID_RESULT",
              "The inventory refers to unknown images.",
            );
            merged.push({
              ...item,
              id: `inventory-${task.index}-${task.offset}-${item.id}`,
            });
          }
          run.limitations.push(...answer.result.limitations);
        }
        this.save(run);
        requireThat(
          merged.length <= 128 &&
            new Set(merged.map((x) => x.id)).size === merged.length,
          "VISION_INVALID_RESULT",
          "The detail inventory exceeds the limit or contains duplicate ids.",
        );
        stage("inventory_complete");
        const updated = this.freeze({
          ...contract,
          requirements: merged,
          inventory_runs: [...contract.inventory_runs, run.id],
        });
        store.run(
          "UPDATE reference_contracts SET contract=? WHERE model=?",
          JSON.stringify(updated),
          run.model,
        );
        run.inventory = inventories;
        run.created_contract_hash = updated.hash;
        run.created_contract = updated;
      } else {
        const rev = store.revision(p, run.model, run.revision);
        const expectedNativeIds = new Set(["native-brep", ...contract.dimensions.map((x) => x.id), ...contract.contacts.map((x) => x.id)]);
        const reusable = store.all("SELECT record FROM reference_runs WHERE model=? AND phase='compare' AND id<>? ORDER BY created DESC", run.model, run.id)
          .map((row: any) => JSON.parse(row.record))
          .find((previous: any) => {
            let sameNative = false;
            try { sameNative = (previous.contract && nativeFingerprint(store, p, run.model, previous.revision, previous.contract)) === run.native_fingerprint; } catch { /* A missing historical revision is a cache miss. */ }
            return sameNative && previous.native_checks?.length >= expectedNativeIds.size &&
            [...expectedNativeIds].every((id) => previous.native_checks.filter((c: any) => c.id === id).length === 1);
          });
        if (reusable) {
          run.native_checks = reusable.native_checks.filter((c: any) => expectedNativeIds.has(c.id));
          run.reused_native_run = reusable.id;
          stage("native_geometry_reused");
        } else {
        stage("native_geometry");
        const valid = rev.geometry?.aggregate?.valid === true;
        run.native_checks.push({
          id: "native-brep",
          status: valid ? "passed" : "failed",
          method: "OCCT shape validity",
          measured: valid,
          expected: true,
          unit: "boolean",
          reason: "Geometry validity does not prove reference fidelity.",
        });
        for (const rule of contract.dimensions) {
          const facts = rule.feature_id
            ? rev.geometry?.facts?.[rule.feature_id]
            : rev.geometry?.aggregate;
          const bounds = facts?.bounds;
          const axis = { x: 0, y: 1, z: 2 }[rule.axis];
          const measured = Array.isArray(bounds)
            ? bounds[axis + 3] - bounds[axis]
            : null;
          run.native_checks.push({
            id: rule.id,
            status:
              measured === null
                ? "unknown"
                : Math.abs(measured - rule.value_mm) <= rule.tolerance_mm
                  ? "passed"
                  : "failed",
            method: "native bounds",
            measured,
            expected: rule.value_mm,
            unit: "mm",
            reason: rule.source,
          });
        }
        for (const rule of contract.contacts) {
          stage("native_contact:" + rule.id);
          const result = await this.tool(
            p,
            "cad_measure",
            {
              model_id: run.model,
              revision: run.revision,
              metric: "distance",
              feature_id: rule.feature_id,
              other_feature_id: rule.other_feature_id,
              idempotency_key: id("review-contact"),
            },
            signal,
          );
          const measured =
            typeof result.measurements === "number"
              ? result.measurements
              : (result.measurements?.distance_mm ??
                result.measurements?.distance ??
                result.distance_mm ??
                null);
          run.native_checks.push({
            id: rule.id,
            status:
              typeof measured !== "number"
                ? "unknown"
                : measured <= rule.maximum_gap_mm
                  ? "passed"
                  : "failed",
            method: "native minimum distance",
            measured: typeof measured === "number" ? measured : null,
            expected: rule.maximum_gap_mm,
            unit: "mm",
            reason:
              "Declared mating/contact relationship; not a global assembly certificate.",
          });
        }
        }
        stage("viewer_capture");
        let preview: any = readStoredPreview(store, p, run.model, run.revision);
        if (!preview)
          preview = await this.tool(
            p,
            "cad_render",
            {
              model_id: run.model,
              revision: run.revision,
              deflection: { value: "0.02", unit: "mm" },
              idempotency_key: id("review-render"),
            },
            signal,
          );
        const cameraRefs = contract.references.filter((r) => r.camera);
        const sized = (refId: string) => {
          const image = references.find((x) => x.id === refId)!;
          return { width: image.width, height: image.height };
        };
        const views: CaptureView[] = [
          ...DEFAULT_VIEWS,
          ...cameraRefs.map((r) => ({
            id: "registered-" + r.id,
            label: r.label,
            camera: r.camera!,
            ...sized(r.id),
          })),
          // The judge route also needs the part-id and flat-colour channels of
          // every camera: set-of-mark labels, projections and colour probes.
          ...(acceptance === "gates_judge"
            ? cameraRefs.flatMap((r) => [
                { id: "ids-" + r.id, label: r.label + " (ids)", camera: r.camera!, ...sized(r.id), channel: "ids" as const },
                { id: "albedo-" + r.id, label: r.label + " (albedo)", camera: r.camera!, ...sized(r.id), channel: "albedo" as const },
              ])
            : []),
        ];
        // Captures depend on geometry, appearance, renderer and cameras only, not
        // on the contract: unchanged bindings reuse the exact stored PNGs.
        const { contract_hash: _contractHash, ...captureBinding } = run.binding;
        run.capture_key = hash({
          ...captureBinding,
          views: views.map((v) => ({ id: v.id, camera: v.camera, width: v.width, height: v.height, channel: v.channel ?? "lit" })),
        });
        const storedOf = (r: any) => [
          ...(r.images ?? []).filter((i: any) => i.kind === "render").map((i: any) => ({ ...i, channel: "lit" })),
          ...(r.channel_images ?? []),
        ];
        const complete = (r: any) => {
          const stored = storedOf(r);
          return stored.length === views.length && views.every((v) => stored.filter((i: any) => i.id === v.id).length === 1);
        };
        const captureSource =
          store
            .all("SELECT record FROM reference_runs WHERE model=? AND phase='compare' AND id<>? ORDER BY created DESC LIMIT 20", run.model, run.id)
            .map((row: any) => JSON.parse(row.record))
            .find((r: any) => r.capture_key === run.capture_key && complete(r)) ??
          (reusable?.fingerprint === run.fingerprint && complete(reusable) ? reusable : null);
        const captured = captureSource
          ? {
              captures: storedOf(captureSource).map((i: any) => {
                const saved = store.getArtifact(p, i.artifact_id),
                  bytes = store.readBlob(saved.hash);
                requireThat(bytes.subarray(0, 8).equals(PNG_MAGIC), "INTEGRITY_FAILURE", "The stored viewer capture is not a PNG.");
                return { id: i.id, label: i.label, bytes, width: bytes.readUInt32BE(16), height: bytes.readUInt32BE(20), channel: i.channel ?? "lit", ids: i.ids ?? [] };
              }),
            }
          : await captureViews(this.service, p, run.model, run.revision, preview, views, signal);
        if (captureSource) run.reused_capture_run = captureSource.id;
        const renders: ReviewImage[] = [];
        const channels = new Map<string, { ids: any[] }>();
        run.channel_images = [];
        for (const image of captured.captures) {
          const a = artifact(image.bytes, "image/png", image.id + ".png", "viewer_capture");
          writeFileSync(join(folder, image.id + ".png"), image.bytes, { mode: 0o600 });
          if ((image.channel ?? "lit") === "lit") {
            run.images.push({ id: image.id, label: image.label, artifact_id: a.artifact_id, kind: "render" });
            renders.push(image);
          } else {
            run.channel_images.push({ id: image.id, label: image.label, artifact_id: a.artifact_id, channel: image.channel, ids: image.ids ?? [] });
            channels.set(image.id, { ids: image.ids ?? [] });
          }
        }
        for (const ref of contract.references.filter((r) => r.registered)) {
          requireThat(
            ref.camera,
            "REFERENCE_REGISTRATION_REQUIRED",
            "Pixel comparison needs an explicit camera.",
          );
          const file = join(folder, ref.id + ".difference.png");
          const metrics = await this.python(
            [
              "compare",
              join(folder, ref.id + ".png"),
              join(folder, "registered-" + ref.id + ".png"),
              file,
            ],
            signal,
          );
          const a = artifact(
            readFileSync(file),
            "image/png",
            ref.id + ".difference.png",
            "reference_difference",
          );
          run.images.push({
            id: "difference-" + ref.id,
            label: ref.label,
            artifact_id: a.artifact_id,
            kind: "difference",
          });
          for (const [metric, threshold] of [
            ["silhouette_iou", contract.silhouette_iou_min],
            ["edge_f1", contract.edge_f1_min],
          ] as const)
            run.native_checks.push({
              id: ref.id + "-" + metric,
              status: metrics[metric] >= threshold ? "passed" : "failed",
              method: metric,
              measured: metrics[metric],
              expected: threshold,
              unit: "ratio",
              reason: metrics.scope,
            });
        }
        if (acceptance === "gates_judge")
          await this.judgeReview(p, contract, run, {
            folder,
            channels,
            signal,
            stage,
            onUsage,
          });
        else {
        const reuseCapture = !!captureSource && captureSource.id === reusable?.id;
        const reviews: any[] = [];
        run.reviews = reviews;
        const tasks: { model: string; offset: number; batch: any[]; combined: any }[] = [];
        for (const model of contract.models) {
          const priorReview = reuseCapture && reusable.reviews?.find((r: any) => r.model === model);
          if (priorReview && contract.requirements.every((req) => priorReview.result.findings.filter((f: any) => f.requirement_id === req.id).length === 1)) {
            reviews.push(structuredClone(priorReview));
            (run.reused_vision ??= []).push({model, run_id: reusable.id, request_ids: reusable.usage.filter((u: any) => u.model === model).map((u: any) => u.request_id)});
            stage("vision_reused:" + model);
            continue;
          }
          const combined: any = priorReview ? structuredClone(priorReview.result) : {
            findings: [],
            additional_issues: [],
            limitations: [],
          };
          reviews.push({ model, result: combined });
          for (
            let offset = 0;
            offset < contract.requirements.length;
            offset += 16
          ) {
            const batch = contract.requirements.slice(offset, offset + 16);
            if (priorReview && batch.every((req) => combined.findings.filter((f: any) => f.requirement_id === req.id).length === 1)) {
              (run.reused_vision_batches ??= []).push({ model, offset, run_id: reusable.id, requirement_ids: batch.map((r) => r.id) });
              continue;
            }
            const batchIds = new Set(batch.map((r) => r.id));
            combined.findings = combined.findings.filter((f: any) => !batchIds.has(f.requirement_id));
            tasks.push({ model, offset, batch, combined });
          }
        }
        this.save(run);
        // Model/batch calls run in parallel (bounded) with one retry each for an
        // incomplete or invalid answer; a failed call still fails the whole run
        // after the others have settled (validated partial batches are kept).
        const pool = limiter(VISION_PARALLEL);
        const settled = await Promise.allSettled(
          tasks.map((task) =>
            pool(async () => {
              stage("vision_compare:" + task.model + ":" + task.offset);
              const { batch, combined, model } = task;
              const answer = await withRetry(
                () =>
                  inspectImages({
                    model,
                    schema: comparisonSchema(batch.map((r) => r.id), references.map((r) => r.id), renders.map((r) => r.id)),
                    images: [...references, ...renders],
                    remainingUsd: ledger.remaining(),
                    onReserve: ledger.reserve,
                    signal,
                    onUsage,
                    prompt:
                      "Compare original reference photographs against the captured CAD viewer images. Return exactly one finding for EVERY requirement ID below. FIRST verify whether the requirement itself is actually supported by the original reference images: reference_support must be supported, contradicted, or uncertain. Inventories can contain incorrect OCR, mistaken counts, orientations or invented details. If the requirement is contradicted or uncertain in the reference, report verdict uncertain and explain the inventory correction or missing source evidence; never demand changing correct geometry to match an invented requirement. Inspect each detail independently at the available resolution. Match viewpoints before judging: the six named orientation views are supplementary and do not establish a calibrated photo match. Identify floating/disconnected elements, omitted or misplaced features, inaccurate curves, label layouts, finish/texture and lenses that look like empty holes, opaque disks or unconvincing glass. Additional visible defects must be reported even if absent from the inventory. Every finding names actual reference/render image IDs and normalized regions where discernible; region may be null if unknown. Do not reward CAD part names or previous approvals. Return uncertain/not_visible for insufficient or occluded evidence; never treat absent evidence as a match. Scope: " +
                      contract.scope +
                      "\nFrozen requirements:\n" +
                      JSON.stringify(batch),
                  }),
                { signal, onRetry: (code) => (run.retries ??= []).push({ stage: "vision_compare:" + model + ":" + task.offset, code }) },
              );
              const known = new Set(batch.map((r) => r.id));
              for (const f of answer.result.findings)
                requireThat(
                  known.has(f.requirement_id) &&
                    references.some((r) => r.id === f.reference_id) &&
                    (!f.render_id || renders.some((r) => r.id === f.render_id)),
                  "VISION_INVALID_RESULT",
                  "The finding contains unknown criterion or image references.",
                );
              for (const issue of answer.result.additional_issues)
                requireThat(
                  references.some((r) => r.id === issue.reference_id) &&
                    (!issue.render_id ||
                      renders.some((r) => r.id === issue.render_id)),
                  "VISION_INVALID_RESULT",
                  "An additional finding refers to unknown images.",
                );
              combined.findings.push(...answer.result.findings);
              combined.additional_issues.push(...answer.result.additional_issues);
              combined.limitations.push(...answer.result.limitations);
              run.limitations.push(...answer.result.limitations);
              run.findings = adjudicate(contract.requirements, reviews, run.native_checks, reviews.reduce((n, r) => n + r.result.additional_issues.length, 0), contract.minimum_confidence).findings;
              this.save(run);
            }),
          ),
        );
        const failure = settled.find((x) => x.status === "rejected") as PromiseRejectedResult | undefined;
        if (failure) throw failure.reason;
        stage("adjudication");
        const result = adjudicate(
          contract.requirements,
          reviews,
          run.native_checks,
          reviews.reduce((n, r) => n + r.result.additional_issues.length, 0),
          contract.minimum_confidence,
        );
        run.findings = result.findings;
        run.reviews = reviews;
        run.summary = {
          ...result.counts,
          contract_hash: contract.hash,
          run_id: run.id,
          scope: contract.scope,
          report_artifact_id: null,
          ...(contract.acceptance ? { acceptance: "two_vlm" } : {}),
        };
        run.limitations.push(
          BASE_SCOPE,
          "Two vision judgments are evidence within the declared checks, not a probability of a perfect reconstruction.",
        );
        }
        requireThat(
          hash(
            fingerprint(store, p, run.model, run.revision, contract.hash),
          ) === run.fingerprint,
          "STALE_REVISION",
          "Geometry, material appearance or viewer code changed during the review.",
        );
      }
      run.state = "completed";
      run.stage = "complete";
      run.finished_at = new Date().toISOString();
      run.elapsed_ms = Date.now() - startedAt;
      const saved = artifact(
        Buffer.from(JSON.stringify(run, null, 2)),
        "application/json",
        "reference-review.json",
        "reference_review_report",
      );
      run.report_artifact_id = saved.artifact_id;
      if (run.summary) run.summary.report_artifact_id = saved.artifact_id;
      this.save(run);
      store.audit("reference_review_completed", {
        model_id: run.model,
        revision: run.revision,
        run_id: run.id,
        phase: run.phase,
        status: run.summary?.status ?? (run.phase === "inspect" ? "inspection_complete" : "inventory_created"),
      });
    } catch (error) {
      run.state = "failed";
      run.stage = "failed";
      run.error = safeError(error);
      run.elapsed_ms = Date.now() - startedAt;
      run.finished_at = new Date().toISOString();
      this.save(run);
    } finally {
      rmSync(folder, { recursive: true, force: true });
    }
  }
  /** crops.py (judge inputs and deterministic probes) in the image venv. */
  private async crops(action: "prepare" | "pair" | "probe", spec: any, folder: string, signal: AbortSignal) {
    const root = resolve(process.env.MATHFORGE_ROOT ?? process.cwd());
    const file = join(folder, `crops-${action}-${id("spec")}.json`);
    writeFileSync(file, JSON.stringify(spec), { mode: 0o600 });
    const { stdout } = await execute(
      imagePython(root),
      [join(root, "packages/reference-review/crops.py"), action, file],
      { signal, timeout: 120000, maxBuffer: 4 * 1024 * 1024 },
    );
    return JSON.parse(stdout);
  }
  /** The deterministic floor of the reviewed revision (Ga's cad_gates). A build
   *  without cad_gates, or a failing gate call, yields floor null: the review
   *  then cannot pass (insufficient_evidence), never a silent pass. */
  private async gatesFor(p: Principal, run: any, contract: Contract, signal: AbortSignal) {
    const none = (error: string) => ({
      source: "unavailable" as const,
      floor_passed: null,
      residual_count: 0,
      top: [] as any[],
      error: error.slice(0, 300),
      residuals: [] as any[],
    });
    if (!Object.hasOwn(ToolSchemas, "cad_gates")) return none("cad_gates is not available in this build");
    try {
      let result: any = await this.service.call(p, "cad_gates" as any, {
        model_id: run.model,
        revision: run.revision,
        contract_hash: contract.hash,
      });
      if (result?.status === "failed")
        return { ...none(String(result.errors?.[0]?.message ?? "cad_gates failed")), source: "cad_gates" as const };
      if (result?.job_id && result.floor_passed === undefined) {
        const deadline = Date.now() + 300_000;
        let job = this.service.jobs.get(p, result.job_id);
        while (!["succeeded", "failed", "cancelled"].includes(job.status) && Date.now() < deadline) {
          signal.throwIfAborted();
          await new Promise((r) => setTimeout(r, 200));
          job = this.service.jobs.get(p, result.job_id);
        }
        if (job.status !== "succeeded")
          return { ...none("cad_gates job " + job.status), source: "cad_gates" as const };
        result = job.result;
      }
      const payload = result?.floor_passed === undefined && result?.result ? result.result : result;
      const residuals: any[] = Array.isArray(payload?.residuals) ? payload.residuals : [];
      const compact = (r: any) =>
        Object.fromEntries(
          Object.entries(r ?? {}).map(([k, v]) => [k, typeof v === "string" ? v.slice(0, 200) : JSON.stringify(v ?? null).length > 200 ? JSON.stringify(v).slice(0, 200) : v]),
        );
      return {
        source: "cad_gates" as const,
        floor_passed: typeof payload?.floor_passed === "boolean" ? payload.floor_passed : null,
        residual_count: residuals.length,
        top: residuals.slice(0, 5).map(compact),
        error: typeof payload?.floor_passed === "boolean" ? null : "cad_gates returned no floor_passed",
        residuals,
      };
    } catch (error) {
      if (signal.aborted) throw error;
      return { ...none(safeError(error).message ?? "cad_gates failed"), source: "cad_gates" as const };
    }
  }
  /** Acceptance gates_judge: cad_gates floor + one advisory judge on registered
   *  crop triples, with deterministic confirmation of its candidate defects. */
  private async judgeReview(
    p: Principal,
    contract: Contract,
    run: any,
    ctx: {
      folder: string;
      channels: Map<string, { ids: any[] }>;
      signal: AbortSignal;
      stage: (s: string) => void;
      onUsage: (usage: any) => void;
    },
  ) {
    const { folder, signal, stage } = ctx,
      store = this.service.store,
      config: JudgeConfig = judgeOf(contract),
      started = Date.now();
    stage("gates");
    const { residuals, ...gates } = await this.gatesFor(p, run, contract, signal);
    run.gates = gates;
    this.save(run);
    // Unchanged requests (same prompt, same image bytes, same judge) are reused.
    const prior = new Map<string, any>();
    for (const row of store.all("SELECT record FROM reference_runs WHERE model=? AND phase='compare' AND id<>? ORDER BY created DESC LIMIT 20", run.model, run.id)) {
      const r = JSON.parse(row.record);
      for (const c of r.judge_calls ?? []) if (!prior.has(c.key)) prior.set(c.key, { ...c, run_id: r.id });
    }
    run.judge_calls = [];
    let spentJudge = 0;
    const ledger = costLedger(config.max_cost_usd, () => spentJudge);
    const stats = { calls: 0, retries: 0, failed: 0, reused: 0 };
    const pool = limiter(config.max_parallel);
    const call = <T,>(label: string, prompt: string, images: ReviewImage[], schema: z.ZodType<T>) =>
      pool(async (): Promise<T | null> => {
        const key = callKey(config, prompt, images);
        const previous = prior.get(key);
        if (previous) {
          const parsed = schema.safeParse(previous.result);
          if (parsed.success) {
            stats.reused++;
            run.judge_calls.push({ key, label, result: previous.result, reused_from: previous.run_id });
            return parsed.data;
          }
        }
        stage("judge:" + label);
        try {
          const answer = await withRetry(
            () =>
              judgeCall({
                config,
                prompt,
                schema,
                images,
                remainingUsd: ledger.remaining(),
                onReserve: ledger.reserve,
                onUsage: (usage) => {
                  spentJudge += usage.cost_usd;
                  ctx.onUsage(usage);
                },
                signal,
              }),
            { signal, onRetry: () => stats.retries++ },
          );
          stats.calls++;
          run.judge_calls.push({ key, label, result: answer.result });
          this.save(run);
          return answer.result;
        } catch (error) {
          if (signal.aborted) throw error;
          stats.failed++;
          run.limitations.push(`Judge call ${label} failed: ${safeError(error).code}`);
          return null;
        }
      });
    const image = (id: string, label: string, path: string): ReviewImage => {
      const bytes = readFileSync(path);
      return { id, label, bytes, width: bytes.readUInt32BE(16), height: bytes.readUInt32BE(20) };
    };
    // A previous judged revision of this model gives the pairwise before/after.
    const previousRun = config.pairwise
      ? store
          .all("SELECT record FROM reference_runs WHERE model=? AND phase='compare' AND id<>? ORDER BY created DESC LIMIT 20", run.model, run.id)
          .map((row: any) => JSON.parse(row.record))
          .find((r: any) => r.state === "completed" && r.acceptance === "gates_judge" && r.revision !== run.revision)
      : null;
    const cameraRefs = contract.references.filter((r) => r.camera);
    const findings: ReturnType<typeof evaluateItem>[] = [];
    const issues: any[] = [];
    const pairwise: any[] = [];
    const evidence: Evidence = {
      gates: residuals,
      failedChecks: run.native_checks.filter((c: any) => c.status === "failed"),
      probes: new Map(),
    };
    const accepted = new Set((contract.accepted_assumptions ?? []).map((a) => a.requirement_id));
    await Promise.all(
      cameraRefs.map(async (ref) => {
        const reqs = contract.requirements.filter((r) => r.reference_ids.includes(ref.id));
        const idsCapture = ctx.channels.get("ids-" + ref.id);
        const base = {
          view_id: ref.id,
          source: join(folder, ref.id + ".png"),
          render: join(folder, "registered-" + ref.id + ".png"),
          ids: idsCapture ? join(folder, "ids-" + ref.id + ".png") : null,
          ids_map: idsCapture?.ids ?? [],
          albedo: ctx.channels.has("albedo-" + ref.id) ? join(folder, "albedo-" + ref.id + ".png") : null,
          registered: ref.registered,
        };
        const chunks = reqs.length ? Array.from({ length: Math.ceil(reqs.length / 8) }, (_, i) => reqs.slice(i * 8, i * 8 + 8)) : [[]];
        let previousRender: string | null = null;
        const prevImage = previousRun?.images?.find((i: any) => i.id === "registered-" + ref.id && i.kind === "render");
        if (prevImage) {
          try {
            const bytes = store.readBlob(store.getArtifact(p, prevImage.artifact_id).hash);
            previousRender = join(folder, "previous-registered-" + ref.id + ".png");
            writeFileSync(previousRender, bytes, { mode: 0o600 });
          } catch {
            previousRender = null;
          }
        }
        await Promise.all(
          chunks.map(async (chunk, ci) => {
            const prepared: PreparedView = await this.crops(
              "prepare",
              {
                ...base,
                items: chunk.map((r) => ({
                  id: r.id,
                  feature_ids: r.feature_ids ?? null,
                  region_box: r.region?.reference_id === ref.id ? r.region.box : null,
                  category: r.category,
                })),
                out_dir: join(folder, "judge", `${ref.id}-${ci}`),
              },
              folder,
              signal,
            );
            const images = [
              ...(ci === 0 ? [image("overview", `${ref.label}: whole view`, prepared.overview.path)] : []),
              ...prepared.items.map((it) => image(it.id, `item ${it.id}`, it.composite.path)),
            ];
            if (!images.length) return;
            const ids = chunk.map((r) => r.id);
            const answer = ids.length || ci === 0
              ? await call(
                  `${ref.id}:${ci}`,
                  viewPrompt(prepared, chunk, contract.scope),
                  images,
                  viewSchema(ids.length ? ids : ["none"]),
                )
              : null;
            // Deterministic probes where the judge sees a defect.
            const regions: any[] = [];
            for (const it of prepared.items) {
              const a = answer?.items.find((x) => x.requirement_id === it.id);
              const no = a?.answers.filter((x) => x.answer === "no") ?? [];
              if (!no.length) continue;
              const boxes = no.map((x) => x.region).filter(Boolean) as number[][];
              const region = boxes.length
                ? [Math.min(...boxes.map((b) => b[0])), Math.min(...boxes.map((b) => b[1])), Math.max(...boxes.map((b) => b[2])), Math.max(...boxes.map((b) => b[3]))]
                : (a?.region ?? [0, 0, 1, 1]);
              regions.push({ key: `${ref.id}|${it.id}`, crop: it.crop, region });
            }
            (answer?.additional_issues ?? []).forEach((issue, n) =>
              regions.push({ key: `${ref.id}|issue-${ci}-${n}`, crop: [0, 0, prepared.width, prepared.height], region: issue.region ?? [0, 0, 1, 1] }),
            );
            if (regions.length) {
              const probed = await this.crops("probe", { ...base, regions }, folder, signal);
              for (const r of probed.regions) evidence.probes.set(r.key, r);
            }
            for (const it of prepared.items) {
              const req = chunk.find((r) => r.id === it.id)!;
              findings.push(
                evaluateItem({
                  req,
                  view: { ...prepared, view_id: ref.id },
                  item: it,
                  answer: answer?.items.find((x) => x.requirement_id === it.id),
                  evidence,
                  accepted,
                }),
              );
            }
            (answer?.additional_issues ?? []).forEach((issue, n) => {
              const probe = evidence.probes.get(`${ref.id}|issue-${ci}-${n}`);
              const confirmed =
                (probe?.residual_share ?? 0) > 0.1 || (probe?.delta_e00 ?? 0) > 10;
              issues.push({
                view_id: ref.id,
                description: issue.description.slice(0, 600),
                severity: issue.severity,
                region: issue.region,
                confirmation: confirmed ? "confirmed" : "unconfirmed",
              });
            });
            // Pairwise before/after, the image order swapped; counted only when
            // both orders agree.
            if (previousRender && prepared.items.length) {
              const tag = `${ref.id}-${ci}`;
              const specItems = prepared.items.map((it) => ({ id: it.id, crop: it.crop }));
              const ab = await this.crops("pair", { source: base.source, a: previousRender, b: base.render, items: specItems, tag: tag + "-ab", out_dir: join(folder, "judge", tag + "-pair") }, folder, signal);
              const ba = await this.crops("pair", { source: base.source, a: base.render, b: previousRender, items: specItems, tag: tag + "-ba", out_dir: join(folder, "judge", tag + "-pair") }, folder, signal);
              const prompt = pairPrompt(prepared, chunk);
              const [first, second] = await Promise.all([
                call(`${tag}:pair-ab`, prompt, ab.items.map((x: any) => image(x.id, `item ${x.id}`, x.composite.path)), pairSchema(ids)),
                call(`${tag}:pair-ba`, prompt, ba.items.map((x: any) => image(x.id, `item ${x.id}`, x.composite.path)), pairSchema(ids)),
              ]);
              for (const id of ids)
                pairwise.push({
                  requirement_id: id,
                  view_id: ref.id,
                  previous_revision: previousRun.revision,
                  preferred: pairwiseVerdict(
                    first?.items.find((x) => x.requirement_id === id)?.better,
                    second?.items.find((x) => x.requirement_id === id)?.better,
                  ),
                });
            }
          }),
        );
      }),
    );
    stage("adjudication");
    const order = new Map(contract.requirements.map((r, i) => [r.id, i]));
    findings.sort((a, b) => order.get(a.requirement_id)! - order.get(b.requirement_id)! || a.view_id.localeCompare(b.view_id));
    const issueCritical = issues.filter((i) => i.severity === "critical" && i.confirmation === "confirmed").length;
    const counts = adjudicateGatesJudge({
      requirements: contract.requirements,
      findings,
      native: run.native_checks,
      floorPassed: gates.floor_passed,
      judgeComplete: stats.failed === 0,
    });
    const status = issueCritical && counts.status !== "needs_correction" ? "needs_correction" : counts.status;
    const notes = new Map((contract.accepted_assumptions ?? []).map((a) => [a.requirement_id, a.note]));
    const assumptions: any[] = [];
    for (const f of findings) {
      const text = `${f.requirement_id} in ${f.view_id}: ${f.largest_difference || "judge answered no"}`;
      if (f.critical && f.confirmation === "unconfirmed")
        assumptions.push({
          requirement_id: f.requirement_id,
          view_id: f.view_id,
          text: (f.accepted ? `Accepted: ${notes.get(f.requirement_id)} | ` : "Unconfirmed judge finding; fix it or accept it via accepted_assumptions | ") + text,
          state: f.accepted ? "accepted" : "open",
        });
      else if (f.outcome === "candidate_defect" && f.confirmation !== "confirmed")
        assumptions.push({ requirement_id: f.requirement_id, view_id: f.view_id, text: `Judge (${f.confirmation}${f.critical ? "" : ", not required"}): ${text}`, state: "informational" });
      else if (f.outcome === "uncertain")
        assumptions.push({ requirement_id: f.requirement_id, view_id: f.view_id, text: `Judge could not confirm (${f.region_check === "inconsistent" ? "region disagrees with the registered projection" : "cannot tell"}): ${text}`, state: "informational" });
    }
    const judgedIds = new Set(findings.map((f) => f.requirement_id));
    for (const r of contract.requirements.filter((r) => !judgedIds.has(r.id)))
      assumptions.push({ requirement_id: r.id, view_id: null, text: `${r.id}: not judged, none of its references has a camera (${r.reference_ids.join(", ")}); covered by the deterministic gates only.`, state: "informational" });
    for (const pw of pairwise.filter((x) => x.preferred === "previous"))
      assumptions.push({ requirement_id: pw.requirement_id, view_id: pw.view_id, text: `Pairwise (both orders): ${pw.previous_revision} matched ${pw.requirement_id} better than this revision (possible regression).`, state: "informational" });
    for (const issue of issues.filter((i) => i.confirmation !== "confirmed"))
      assumptions.push({ requirement_id: null, view_id: issue.view_id, text: `Judge issue (${issue.severity}, unconfirmed): ${issue.description}`, state: "informational" });
    run.judge_findings = findings;
    run.judge_issues = issues;
    run.pairwise = pairwise;
    run.assumptions = assumptions.slice(0, 256);
    run.judge = {
      provider: config.provider,
      model: config.model,
      effort: config.effort,
      calls: stats.calls,
      retries: stats.retries,
      failed_calls: stats.failed,
      reused_calls: stats.reused,
      elapsed_ms: Date.now() - started,
      total_findings: findings.length,
      open_critical: counts.openCritical,
      confirmed_critical: counts.confirmedCritical + issueCritical,
    };
    // The viewer lists findings per requirement: one entry per item, one
    // observation per judged view (no confidence: the judge gives none).
    run.findings = contract.requirements.map((req) => {
      const own = findings.filter((f) => f.requirement_id === req.id);
      const verdict = own.some((f) => f.critical && f.confirmation === "confirmed")
        ? "mismatch"
        : own.length && own.every((f) => f.outcome === "match")
          ? "match"
          : "uncertain";
      return {
        id: req.id,
        description: req.description,
        category: req.category,
        required: req.required,
        verdict,
        observations: own.map((f) => ({
          requirement_id: req.id,
          reference_support: "supported",
          verdict: f.outcome === "match" ? "match" : f.outcome === "candidate_defect" ? "mismatch" : "uncertain",
          confidence: 0,
          evidence: (`${f.largest_difference || f.outcome} [${f.confirmation}${f.confirmed_by.length ? ": " + f.confirmed_by.join("; ") : ""}]`).slice(0, 1500),
          reference_id: f.view_id,
          render_id: "registered-" + f.view_id,
          reference_region: null,
          render_region: null,
          correction: "",
          model: config.model,
        })),
      };
    });
    run.summary = {
      status,
      required: counts.required,
      matched: counts.matched,
      mismatched: counts.mismatched,
      unknown: counts.unknown,
      exact_replica_certified: false,
      contract_hash: contract.hash,
      run_id: run.id,
      scope: contract.scope,
      report_artifact_id: null,
      acceptance: "gates_judge",
      floor_passed: gates.floor_passed,
      open_findings: counts.openCritical,
    };
    run.limitations.push(
      BASE_SCOPE,
      "Acceptance gates_judge: the deterministic cad_gates floor on this revision decides; one advisory judge adds candidate defects that block only when a deterministic probe confirms them or while an unconfirmed critical one is not accepted as an assumption.",
    );
    if (gates.floor_passed === null)
      run.limitations.push(`No deterministic floor: ${gates.error ?? "cad_gates unavailable"}. The review cannot pass without it.`);
  }
  async close() {
    for (const r of this.running.values())
      r.controller.abort(new Error("Service closing"));
    await Promise.allSettled([...this.running.values()].map((r) => r.done));
  }
}
