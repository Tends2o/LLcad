import { Store } from "../model-service/store.js";
import { Principal } from "../policy/index.js";
import { Gates } from "../../hooks/server-registry/index.js";
import { Worker } from "./worker.js";
import { Metrics } from "./metrics.js";
import { id, hash } from "../semantic-ir/hash.js";
import { safeError, requireThat } from "../semantic-ir/errors.js";
import { LIMITS, REGISTRY_HASH } from "../compiler/index.js";
import { validate } from "../validation/index.js";
import { packGLB } from "../model-service/glb.js";
import {
  packMesh,
  packPreview,
  PREVIEW_BINARY_MIME,
  type PackedMesh,
} from "./preview-binary.js";
import { exportPackage } from "../model-service/export-package.js";
import { claimQueuedJob } from "./scheduler.js";
import { checkEquation } from "../compiler/constraints.js";
import { BUILD_HASH, currentBuildHash } from "../compiler/build.js";
import {
  assertJobResult,
  assertJobView,
  assertValidation,
  assertResult,
} from "../model-service/result-contracts.js";
import { NativeWorkerResult } from "../semantic-ir/results.js";
import { MeshQuality } from "../semantic-ir/mesh.js";
import { NATIVE_MESH_SOURCE_HASH } from "../compiler/native-build.js";
/** A build may take many turns of the time budget, but not forever. */
const MAX_CONTINUATIONS = 120;
const HEARTBEAT_MS = 5000;
export class Jobs {
  /** Most recently started native worker; tests and revocation may cancel it directly. */
  active: Worker | null = null;
  activeID: string | null = null;
  private workers = new Map<string, Worker>();
  private running = new Set<Promise<void>>();
  private pumping = false;
  private closed = false;
  private timer: NodeJS.Timeout;
  readonly concurrency = Math.max(
    1,
    Math.min(4, Number(process.env.MATHFORGE_WORKERS ?? "2") || 1),
  );
  metrics: Metrics;
  get isClosed() {
    return this.closed;
  }
  /** Registered by the model service: a candidate revision is previewed ahead of the viewer. */
  evaluated: ((p: Principal, model: string, revision: string) => void) | null =
    null;
  /** Registered by the model service: turns a probe report into the follow-up candidate. */
  continuation: ((p: Principal, request: any, report: any) => any) | null =
    null;
  constructor(
    public store: Store,
    public gates: Gates,
  ) {
    this.metrics = new Metrics(store);
    this.timer = setInterval(() => void this.pump(), 300);
    this.timer.unref();
    // The first job after a start should not pay for a cold sandbox either.
    setTimeout(() => Worker.ensurePool(), 1000).unref();
  }
  enqueue(
    p: Principal,
    model: string,
    kind: string,
    request: any,
    tx?: string,
  ): any {
    if (!this.store.db.isTransaction)
      return this.store.atomic(() => this.enqueue(p, model, kind, request, tx));
    const count = this.store.get(
      "SELECT COUNT(*) AS n FROM jobs WHERE tenant=? AND owner=? AND state IN ('queued','running')",
      p.tenant,
      p.user,
    ).n;
    requireThat(
      count < LIMITS.max_queued_per_user,
      "BUDGET_EXCEEDED",
      "Zu viele offene Jobs.",
      {
        alternatives: [
          {
            action: "wait_for_running_jobs",
            maximum_open_jobs: LIMITS.max_queued_per_user,
          },
          { action: "cancel_unneeded_jobs", tool: "cad_job_cancel" },
        ],
      },
    );
    const jid = id("job");
    this.store.run(
      "INSERT INTO jobs(id,tenant,owner,model,tx,kind,state,request,created,phase) VALUES(?,?,?,?,?,?,?,?,?,?)",
      jid,
      p.tenant,
      p.user,
      model,
      tx ?? null,
      kind,
      "queued",
      JSON.stringify(request),
      new Date().toISOString(),
      "queued",
    );
    this.store.access.reserveJob(p, model, jid, kind);
    this.store.run(
      "INSERT INTO outbox VALUES(?,?,?,0)",
      id("out"),
      "job_queued",
      JSON.stringify({ job_id: jid }),
    );
    this.store.afterCommit(() => {
      setImmediate(() => void this.pump());
    });
    return {
      status: "queued",
      model_id: model,
      job_id: jid,
      transaction_id: tx ?? null,
      committed: false,
      recommended_next_actions: ["cad_job_get"],
    };
  }
  authorize(p: Principal, jid: string, cancel = false) {
    const j = this.store.get("SELECT * FROM jobs WHERE id=?", jid);
    requireThat(j, "ACCESS_DENIED", "Job nicht zugänglich.");
    const m = this.store.model(
      p,
      j.model,
      cancel ? "model:edit" : "model:read",
    );
    if (cancel)
      requireThat(
        j.owner === p.user || m.owner === p.user,
        "ACCESS_DENIED",
        "Nur Ersteller oder Projekteigentümer können diesen Job abbrechen.",
      );
    return j;
  }
  get(p: Principal, jid: string) {
    const j = this.authorize(p, jid);
    const budget =
      j.budget_seconds ??
      this.store.get("SELECT seconds FROM job_authorizations WHERE job=?", jid)
        ?.seconds ??
      null;
    const result = {
      job_id: j.id,
      model_id: j.model,
      transaction_id: j.tx,
      status: j.state,
      attempts: j.attempts,
      phase: j.phase ?? null,
      budget_seconds: budget,
      started_at: j.started ? new Date(j.started).toISOString() : null,
      heartbeat_at: j.heartbeat ? new Date(j.heartbeat).toISOString() : null,
      finished_at: j.finished ? new Date(j.finished).toISOString() : null,
      elapsed_seconds: j.started
        ? Math.max(0, ((j.finished ?? Date.now()) - j.started) / 1000)
        : null,
      result: j.result ? JSON.parse(j.result) : null,
      error: j.error ? JSON.parse(j.error) : null,
    };
    assertJobView(result, j.kind);
    if (j.kind === "validate" && result.result) {
      const tx = this.store.transaction(p, j.tx);
      assertValidation(result.result, {
        candidate: tx.candidate,
        ir_hash: hash(tx.plan.ir),
        facts: tx.result.facts,
      });
    }
    return result;
  }
  cancel(p: Principal, jid: string) {
    const j = this.authorize(p, jid, true);
    if (["succeeded", "failed", "cancelled"].includes(j.state))
      return this.get(p, jid);
    this.store.run(
      "UPDATE jobs SET state='cancelled',lease=NULL,lease_until=NULL,phase='cancelled',finished=? WHERE id=?",
      Date.now(),
      jid,
    );
    if (j.tx)
      this.store.run(
        "UPDATE transactions SET state=? WHERE id=? AND state!=?",
        j.kind === "validate" ? "candidate_ready" : "aborted",
        j.tx,
        "committed",
      );
    this.gates.run("on_cancel", { job_id: jid, transaction_id: j.tx ?? null });
    this.metrics.increment("jobs_cancelled");
    this.store.afterCommit(() => {
      this.workers.get(jid)?.cancel();
      if (this.activeID === jid) this.active?.cancel();
    });
    this.store.audit("job_cancelled", { job_id: jid });
    return this.get(p, jid);
  }
  /** Claim queued jobs up to the configured concurrency; each runs in its own isolated worker. */
  async pump() {
    if (this.closed || this.pumping) return;
    this.pumping = true;
    try {
      for (const event of this.store.all(
        "SELECT * FROM outbox WHERE delivered=0 AND event='revision_committed' LIMIT 16",
      )) {
        try {
          this.store.atomic(() => {
            this.gates.run("after_commit", JSON.parse(event.payload));
            this.store.run(
              "UPDATE outbox SET delivered=1 WHERE id=?",
              event.id,
            );
          });
        } catch {
          /* Retry outbox; an existing commit remains successful. */
        }
      }
      while (this.running.size < this.concurrency && !this.closed) {
        const lease = id("lease");
        const job = claimQueuedJob(this.store, lease);
        if (!job) break;
        const task: Promise<void> = this.execute(job, lease).finally(() => {
          this.running.delete(task);
          if (!this.closed) setImmediate(() => void this.pump());
        });
        this.running.add(task);
      }
    } finally {
      this.pumping = false;
    }
  }
  private async execute(job: any, lease: string) {
    let worker: Worker | null = null;
    let heartbeat: NodeJS.Timeout | null = null;
    try {
      requireThat(
        currentBuildHash() === BUILD_HASH,
        "BUILD_MISMATCH",
        "Quellstand wurde während des Betriebs geändert; Dienst mit dem geprüften Build neu starten.",
      );
      requireThat(
        job.attempts < 3,
        "KERNEL_FAILURE",
        "Wiederholungsbudget für Worker ausgeschöpft.",
      );
      if (job.attempts > 1) this.metrics.increment("job_retries");
      const authorization = this.store.access.checkJob(job);
      this.store.run(
        "UPDATE jobs SET phase=?,started=COALESCE(started,?),heartbeat=?,budget_seconds=? WHERE id=? AND lease=?",
        job.kind === "validate" ? "validating" : "native_execution",
        Date.now(),
        Date.now(),
        authorization.seconds,
        job.id,
        lease,
      );
      const request = JSON.parse(job.request),
        p = authorization.principal;
      let result: any,
        preparedGLB: ReturnType<typeof packGLB> | null = null;
      let glbMeshReport: any = null;
      let executed = request;
      let assembledSummary: any = null,
        previewBinary: Buffer | null = null;
      if (job.kind === "render" && request.render_key) {
        // A preview rendered before answers from the cache, without a worker;
        // one being rendered right now by another job is waited for, not repeated.
        let hit = this.renderCacheHit(job.tenant, request.render_key);
        const deadline = Date.now() + authorization.seconds * 1000;
        while (
          !hit &&
          Date.now() < deadline &&
          this.store.get(
            "SELECT 1 FROM jobs WHERE state='running' AND kind='render' AND tenant=? AND id!=? AND json_extract(request,'$.render_key')=?",
            job.tenant,
            job.id,
            request.render_key,
          )
        ) {
          await new Promise((r) => setTimeout(r, 100));
          this.store.run(
            "UPDATE jobs SET heartbeat=?,lease_until=? WHERE id=? AND lease=? AND state='running'",
            Date.now(),
            Date.now() + 60000,
            job.id,
            lease,
          );
          hit = this.renderCacheHit(job.tenant, request.render_key);
        }
        if (hit) {
          this.metrics.increment("render_cache_hits");
          this.store.atomic(() => {
            const current = this.store.get(
              "SELECT state,lease FROM jobs WHERE id=?",
              job.id,
            );
            if (current?.state !== "running" || current.lease !== lease) return;
            this.store.run(
              "UPDATE jobs SET state='succeeded',result=?,lease=NULL,lease_until=NULL,phase='succeeded',finished=? WHERE id=? AND lease=?",
              JSON.stringify(hit),
              Date.now(),
              job.id,
              lease,
            );
            this.store.run(
              "UPDATE outbox SET delivered=1 WHERE event='job_queued' AND json_extract(payload,'$.job_id')=?",
              job.id,
            );
            this.metrics.increment("jobs_succeeded", 1, job.kind);
          });
          return;
        }
      }
      if (job.kind === "validate") {
        const tx = this.store.transaction(p, job.tx);
        requireThat(
          tx.plan.registry_hash === REGISTRY_HASH,
          "BUILD_MISMATCH",
          "Prüfjob benötigt den ursprünglichen Kandidaten-Build.",
        );
        requireThat(
          tx.result &&
            ["candidate_ready", "validated", "validation_failed"].includes(
              tx.state,
            ),
          "CONSTRAINT_CONFLICT",
          "Kandidat ist noch nicht prüfbar.",
        );
        this.gates.run("before_validate", { transaction_id: tx.id });
        const base = this.store.revision(p, tx.model, tx.base);
        result = validate(
          tx.plan.ir,
          tx.result,
          base.ir,
          base.geometry,
          tx.candidate,
          tx.plan.refinement_reports ?? [],
        );
        this.gates.run("after_validate", {
          transaction_id: tx.id,
          digest: result.digest,
        });
      } else {
        requireThat(
          request.plan.registry_hash === REGISTRY_HASH,
          "BUILD_MISMATCH",
          "Berechnungsjob benötigt den ursprünglichen Worker-Build.",
        );
        this.gates.run("before_execute", {
          job_id: job.id,
          input_hash: hash(request),
        });
        const nativeDeadline = Date.now() + authorization.seconds * 1000;
        const effectiveExpiry = Math.min(
          nativeDeadline,
          authorization.expires ?? Infinity,
        );
        const preview =
          job.kind === "render" ? this.planPreview(job.tenant, request) : null;
        if (preview) executed = preview.request;
        if (executed) {
          worker = new Worker();
          this.workers.set(job.id, worker);
          this.active = worker;
          this.activeID = job.id;
          heartbeat = setInterval(() => {
            try {
              this.store.run(
                "UPDATE jobs SET heartbeat=?,lease_until=? WHERE id=? AND lease=? AND state='running'",
                Date.now(),
                Date.now() + 60000,
                job.id,
                lease,
              );
            } catch {
              /* heartbeat is telemetry; fencing still decides publication */
            }
          }, HEARTBEAT_MS);
          heartbeat.unref();
          result = await worker.run(this.store, job.tenant, {
            ...executed,
            cache_owner: job.owner,
            cache_model: job.model,
            policy_budget_seconds: authorization.seconds,
            policy_expires_at: effectiveExpiry,
          });
          assertResult(
            NativeWorkerResult,
            result,
            "native_worker_result",
            LIMITS.request_bytes * 8,
          );
          this.recordWorkerMetrics(result.metrics);
        }
        if (preview) {
          const assembled = preview.assemble(result);
          result = assembled.result;
          assembledSummary = assembled.summary;
          previewBinary = assembled.binary;
        }
        if (
          (job.kind === "export" || job.kind === "render") &&
          request.format === "glb"
        ) {
          const preview = JSON.parse(
            this.store.readBlob(result.blobs["preview.json"]).toString(),
          );
          preparedGLB = packGLB(preview);
          if (request.plan.profile === "watertight_solid") {
            this.store.access.checkJob(job);
            requireThat(
              Date.now() < nativeDeadline,
              "BUDGET_EXCEEDED",
              "Gemeinsames Exportprüfbudget ist erschöpft.",
            );
            const check = new Worker();
            this.workers.set(job.id, check);
            this.active = check;
            const checked = await check.run(this.store, job.tenant, {
              action: "mesh_check",
              plan: { ...request.plan, features: [] },
              meshes: preparedGLB.restored_meshes,
              original_meshes: preview.meshes,
              tolerance: request.plan.tolerance,
              policy_budget_seconds: authorization.seconds,
              policy_expires_at: effectiveExpiry,
            });
            assertResult(
              NativeWorkerResult,
              checked,
              "mesh_roundtrip_worker_result",
              LIMITS.request_bytes * 8,
            );
            const q = MeshQuality.parse(checked.aggregate.mesh_quality);
            requireThat(
              q.native.source_hash === NATIVE_MESH_SOURCE_HASH &&
                q.watertight_solid &&
                Object.values(q.checks).every(Boolean),
              "GEOMETRY_INVALID",
              "Exportierte GLB-Geometrie erfüllt das Meshkörperprofil nicht.",
            );
            glbMeshReport = checked.aggregate;
          }
        }
        requireThat(
          currentBuildHash() === BUILD_HASH,
          "BUILD_MISMATCH",
          "Quellstand änderte sich während der Berechnung; Ergebnis wurde verworfen.",
        );
        this.gates.run("after_execute", {
          job_id: job.id,
          engine_build: result.engine_build,
        });
      }
      this.store.atomic(() => {
        const current = this.store.get(
          "SELECT state,lease FROM jobs WHERE id=?",
          job.id,
        );
        if (current?.state !== "running" || current.lease !== lease) return;
        this.store.run("UPDATE jobs SET phase='persisting' WHERE id=?", job.id);
        this.store.access.checkJob(job);
        for (const f of request.plan?.features ?? []) {
          const blob = result.blobs?.[f.cache_key + ".field.json"];
          if (blob)
            this.store.run(
              "INSERT OR REPLACE INTO cache(tenant,key,blob,created) VALUES(?,?,?,?)",
              job.tenant,
              "field:" + hash([job.owner, job.model, f.id]),
              blob,
              Date.now(),
            );
        }
        if (job.kind === "evaluate") {
          const tx = this.store.transaction(p, job.tx);
          requireThat(
            tx.state === "executing",
            "CANCELLED",
            "Kandidat wurde verworfen.",
          );
          this.store.run(
            "INSERT INTO revisions VALUES(?,?,?,?,?,?,?,?)",
            tx.candidate,
            tx.model,
            tx.base,
            JSON.stringify(tx.plan.ir),
            hash(tx.plan.ir),
            JSON.stringify(result),
            "preview_only",
            new Date().toISOString(),
          );
          this.store.run(
            "UPDATE transactions SET state='candidate_ready',result=? WHERE id=?",
            JSON.stringify(result),
            tx.id,
          );
          for (const f of request.plan.features) {
            for (const key of new Set<string>([
              f.cache_key,
              f.local_cache_key ?? f.cache_key,
            ])) {
              const blob = result.blobs[key + ".brep"];
              if (blob)
                this.store.run(
                  "INSERT OR IGNORE INTO cache(tenant,key,blob,created) VALUES(?,?,?,?)",
                  job.tenant,
                  key,
                  blob,
                  Date.now(),
                );
              const topology = result.blobs[key + ".topology.json"];
              if (topology)
                this.store.run(
                  "INSERT OR IGNORE INTO cache(tenant,key,blob,created) VALUES(?,?,?,?)",
                  job.tenant,
                  key + ":topology",
                  topology,
                  Date.now(),
                );
            }
            // The measurements of a feature belong to its key as much as its
            // shape does; kept here, an unchanged feature is never measured again.
            const measured = result.blobs[f.cache_key + ".facts.json"];
            if (measured)
              this.store.run(
                "INSERT OR IGNORE INTO cache(tenant,key,blob,created) VALUES(?,?,?,?)",
                job.tenant,
                f.cache_key + ":facts",
                measured,
                Date.now(),
              );
          }
          this.metrics.observe(
            "dirty_features",
            (tx.plan.dirty_features ?? []).length,
          );
          this.metrics.observe("total_features", request.plan.features.length);
          this.store.afterCommit(() =>
            this.evaluated?.(p, tx.model, tx.candidate),
          );
          result = {
            status: "candidate_ready",
            transaction_id: tx.id,
            candidate_revision: tx.candidate,
            model_id: tx.model,
            changed_features: tx.plan.changed_features ?? [],
            dependent_features: tx.plan.dependent_features ?? [],
            measurements: result.aggregate,
            metrics: result.metrics,
            validation_status: "required",
            committed: false,
            recommended_next_actions: ["cad_validate"],
          };
        } else if (job.kind === "validate") {
          const tx = this.store.transaction(p, job.tx);
          assertValidation(result, {
            candidate: tx.candidate,
            ir_hash: hash(tx.plan.ir),
            facts: tx.result.facts,
          });
          this.store.run(
            "UPDATE transactions SET validation=?,state=? WHERE id=?",
            JSON.stringify(result),
            result.status === "checks_passed_within_profile"
              ? "validated"
              : "validation_failed",
            job.tx,
          );
          if (result.status !== "checks_passed_within_profile")
            this.metrics.increment("invalid_candidates");
          for (const check of result.checks)
            if (
              check.method === "interval_arithmetic_gradient_flow_certificate"
            )
              this.metrics.increment(
                check.status === "passed"
                  ? "certificates_certified"
                  : "certificates_refused",
              );
          this.store.audit("candidate_validated", {
            transaction_id: job.tx,
            digest: result.digest,
            status: result.status,
          });
        } else if (job.kind === "solve") {
          const solution = result.aggregate;
          const variables = request.solver.variables;
          const values = Object.fromEntries(
            variables.map((v: any) => [
              v.name,
              {
                value: Number(solution.values[v.name]).toFixed(12),
                unit: v.unit,
              },
            ]),
          );
          for (const equation of request.problem.equations)
            requireThat(
              checkEquation(equation, values).passed,
              "CONSTRAINT_CONFLICT",
              "Gerundete Solverparameter erfüllen die Nebenbedingungen nicht.",
            );
          const bindings = Object.fromEntries(
            variables.map((v: any) => [
              v.name,
              { feature_id: v.feature_id, parameter: v.parameter },
            ]),
          );
          result = {
            status: "succeeded",
            model_id: job.model,
            base_revision: request.revision,
            solver: solution,
            committed: false,
            operations: [
              ...variables.map((v: any) => ({
                op: "set_parameter",
                feature_id: v.feature_id,
                parameter: v.parameter,
                expected: v.expected,
                value: values[v.name],
              })),
              ...request.problem.equations.map((e: any) => ({
                op: "add_constraint",
                constraint: {
                  ...e,
                  kind: "equation",
                  feature_id: variables[0].feature_id,
                  bindings,
                },
              })),
            ],
            recommended_next_actions: [
              "cad_plan_edit",
              "cad_apply_patch",
              "cad_validate",
              "cad_commit",
            ],
          };
        } else if (job.kind === "analysis") {
          result = {
            status: "succeeded",
            model_id: job.model,
            revision: request.revision,
            metric: request.metric,
            metrics: result.metrics,
            ...result.aggregate,
          };
        } else if (job.kind === "measure") {
          result = {
            status: "succeeded",
            model_id: job.model,
            revision: request.revision,
            measurements: result.aggregate,
            unit: "mm",
            method: "OCCT_BRepExtrema_DistShapeShape",
            coverage: "two_static_BRep_features",
          };
        } else if (job.kind === "probe") {
          let continuation: any;
          try {
            requireThat(
              this.continuation,
              "OUT_OF_SCOPE",
              "Keine Fortsetzung registriert.",
            );
            continuation = this.store.savepoint(() =>
              this.continuation!(p, request, result.aggregate),
            );
          } catch (error) {
            continuation = { status: "failed", error: safeError(error) };
          }
          result = {
            status: "succeeded",
            model_id: job.model,
            base_revision: request.import.base_revision,
            structure: result.aggregate,
            continuation,
            metrics: result.metrics,
          };
        } else if (job.kind === "render" || job.kind === "export") {
          const artifacts = [];
          if (request.format === "glb") {
            requireThat(
              preparedGLB,
              "INTEGRITY_FAILURE",
              "GLB-Ausgabeprüfung fehlt.",
            );
            const glb = preparedGLB;
            const manifest = {
              filename: "model.glb",
              model_id: job.model,
              revision: request.revision,
              unit: "m",
              source_unit: "mm",
              quality: glbMeshReport
                ? "checks_passed_within_profile"
                : "preview_only",
              lost_semantics: ["parametric_history"],
              certified_surface_bound: null,
              requested_deflection: request.deflection,
              roundtrip: {
                ...glb.report,
                ...(glbMeshReport
                  ? {
                      restored_mesh_quality: glbMeshReport.mesh_quality,
                      measured_vertex_error_bound_mm:
                        glbMeshReport.measured_vertex_error_bound_mm,
                      corresponding_triangle_surface_bound_mm:
                        glbMeshReport.measured_vertex_error_bound_mm,
                      bound_domain:
                        "authoritative_mesh_to_decoded_GLB_binary64_world_coordinates",
                    }
                  : {}),
              },
            };
            this.metrics.observe(
              "export_loss_mm",
              glb.report.max_coordinate_error_mm,
            );
            artifacts.push(
              this.store.artifact(
                p,
                glb.buffer,
                "model/gltf-binary",
                job.model,
                request.revision,
                manifest,
              ),
            );
          } else {
            let previewData: any = null;
            for (const file of result.files) {
              if (/\.(field|mesh(\.[0-9a-z]+)?)\.json$/.test(file)) continue;
              if (
                file === "model.brep" &&
                (job.kind === "render" || request.format !== "brep")
              )
                continue;
              const previewSummary =
                file === "preview.json"
                  ? (assembledSummary ??
                    JSON.parse(
                      this.store.readBlob(result.blobs[file]).toString(),
                    ))
                  : null;
              const viewReport =
                file.startsWith("view.") && result.blobs["view.json"]
                  ? JSON.parse(
                      this.store.readBlob(result.blobs["view.json"]).toString(),
                    )
                  : null;
              if (previewSummary)
                this.metrics.observe(
                  "preview_triangles",
                  previewSummary.triangle_count ??
                    previewSummary.meshes.reduce(
                      (n: number, m: any) => n + m.triangles.length,
                      0,
                    ),
                );
              const roundtrip = result.blobs["roundtrip.json"]
                ? JSON.parse(
                    this.store
                      .readBlob(result.blobs["roundtrip.json"])
                      .toString(),
                  )
                : null;
              if (typeof roundtrip?.measured_vertex_error_bound_mm === "number")
                this.metrics.observe(
                  "export_loss_mm",
                  roundtrip.measured_vertex_error_bound_mm,
                );
              const manifest = {
                model_id: job.model,
                revision: request.revision,
                source_geometry_hash: hash(result.facts),
                engine_build: result.engine_build,
                unit: "mm",
                quality:
                  job.kind === "render"
                    ? "preview_only"
                    : "checks_passed_within_profile",
                requested_deflection: request.deflection,
                ...(previewSummary?.resolution
                  ? { resolution: previewSummary.resolution }
                  : {}),
                ...(previewSummary?.clip ? { clip: previewSummary.clip } : {}),
                ...(viewReport ? { diagnostic_view: viewReport } : {}),
                certified_surface_bound: null,
                roundtrip,
              };
              artifacts.push(
                this.store.artifact(
                  p,
                  this.store.readBlob(result.blobs[file]),
                  file.endsWith(".json")
                    ? "application/json"
                    : file.endsWith(".svg")
                      ? "image/svg+xml"
                      : file.endsWith(".step")
                        ? "model/step"
                        : file.endsWith(".stl")
                          ? "model/stl"
                          : "application/octet-stream",
                  job.model,
                  request.revision,
                  { ...manifest, filename: file },
                ),
              );
              if (file === "preview.json")
                previewData = { summary: previewSummary, manifest };
            }
            // The viewer reads the compact twin of the preview; the JSON stays
            // the authoritative artifact for every client that asked for one.
            // A preview rendered ahead carries the binary alone.
            if (previewBinary) {
              const summary = previewData?.summary ?? assembledSummary;
              if (!previewData && typeof summary?.triangle_count === "number")
                this.metrics.observe(
                  "preview_triangles",
                  summary.triangle_count,
                );
              artifacts.push(
                this.store.artifact(
                  p,
                  previewBinary,
                  PREVIEW_BINARY_MIME,
                  job.model,
                  request.revision,
                  {
                    ...(previewData?.manifest ?? {
                      model_id: job.model,
                      revision: request.revision,
                      source_geometry_hash: hash(result.facts),
                      engine_build: result.engine_build,
                      unit: "mm",
                      quality: "preview_only",
                      requested_deflection: request.deflection,
                      ...(summary?.resolution
                        ? { resolution: summary.resolution }
                        : {}),
                      ...(summary?.clip ? { clip: summary.clip } : {}),
                      certified_surface_bound: null,
                      roundtrip: null,
                    }),
                    filename: "preview.bin",
                    derived_from: previewData
                      ? "preview.json"
                      : "tessellation_cache",
                    encoding: "mathforge-preview-binary-1",
                  },
                ),
              );
            }
          }
          const exported =
            job.kind === "export"
              ? exportPackage(
                  this.store,
                  p,
                  this.store.revision(p, job.model, request.revision),
                  artifacts,
                  request.format,
                )
              : { artifacts };
          if (job.kind === "export")
            this.gates.run("after_export", {
              job_id: job.id,
              artifact_count: exported.artifacts.length,
            });
          result = {
            status: "succeeded",
            ...exported,
            ...(job.kind === "export" ? { result_schema_version: "1" } : {}),
            metrics: result.metrics,
          };
          if (job.kind === "render" && request.render_key)
            this.store.run(
              "INSERT OR REPLACE INTO render_cache(tenant,key,result,artifacts,created) VALUES(?,?,?,?,?)",
              job.tenant,
              request.render_key,
              JSON.stringify(result),
              JSON.stringify(exported.artifacts.map((a: any) => a.artifact_id)),
              Date.now(),
            );
        }
        assertJobResult(job.kind, result);
        this.store.run(
          "UPDATE jobs SET state='succeeded',result=?,lease=NULL,lease_until=NULL,phase='succeeded',finished=? WHERE id=? AND lease=?",
          JSON.stringify(result),
          Date.now(),
          job.id,
          lease,
        );
        this.store.run(
          "UPDATE outbox SET delivered=1 WHERE event='job_queued' AND json_extract(payload,'$.job_id')=?",
          job.id,
        );
        this.metrics.increment("jobs_succeeded", 1, job.kind);
      });
    } catch (error) {
      const failure = safeError(error);
      // Operator diagnostics only; nothing from here is persisted or returned to clients.
      if (process.env.MATHFORGE_DEBUG_ERRORS)
        console.error("[job-service]", error);
      // A build that ran out of time but got further than the last attempt is
      // not a failure: its finished features are in the cache, so the job goes
      // back into the queue and carries on from there. That is what keeps a
      // fixed time budget from putting a ceiling on how complex a model may be.
      if (
        job &&
        !this.closed &&
        ["BUDGET_EXCEEDED", "KERNEL_FAILURE"].includes(failure.code) &&
        this.continue_(job, lease)
      )
        return;
      if (job && !this.closed)
        this.store.atomic(() => {
          const current = this.store.get(
            "SELECT state,lease FROM jobs WHERE id=?",
            job.id,
          );
          if (current?.state !== "running" || current.lease !== lease) return;
          this.store.run(
            "UPDATE jobs SET state='failed',error=?,lease=NULL,lease_until=NULL,phase='failed',finished=? WHERE id=?",
            JSON.stringify(failure),
            Date.now(),
            job.id,
          );
          if (job.tx)
            this.store.run(
              "UPDATE transactions SET state=? WHERE id=? AND state!=?",
              job.kind === "validate" ? "validation_failed" : "failed",
              job.tx,
              "committed",
            );
          // on_failure: the job ends, limits are released and the base revision stays untouched.
          this.gates.run("on_failure", {
            job_id: job.id,
            code: failure.code,
            transaction_id: job.tx ?? null,
          });
          this.metrics.increment("jobs_failed", 1, failure.code);
          this.store.audit("job_failed", {
            job_id: job.id,
            code: failure.code,
          });
        });
    } finally {
      if (heartbeat) clearInterval(heartbeat);
      this.workers.delete(job.id);
      if (this.activeID === job.id) {
        this.active = null;
        this.activeID = null;
      }
    }
  }
  /** How many of a plan's features already have their shape in the cache. */
  private built(tenant: string, plan: any) {
    const keys = (plan?.features ?? []).map((f: any) => f.cache_key);
    let done = 0;
    for (let i = 0; i < keys.length; i += 200) {
      const chunk = keys.slice(i, i + 200);
      done += this.store.get(
        `SELECT COUNT(*) AS n FROM cache WHERE tenant=? AND key IN (${chunk.map(() => "?").join(",")})`,
        tenant,
        ...chunk,
      ).n;
    }
    return { done, total: keys.length };
  }
  /** Put a build that made progress back into the queue. Whether the run was
   *  stopped by the wall clock or by the kernel's own processor limit does not
   *  matter — what counts is that it left finished features behind. A round
   *  that adds nothing ends the attempt for good. */
  private continue_(job: any, lease: string) {
    const request = JSON.parse(job.request);
    if (job.kind !== "evaluate" || !request.plan?.features?.length)
      return false;
    const progress = this.built(job.tenant, request.plan),
      previous = request.built ?? 0,
      round = (request.continuation ?? 0) + 1;
    if (progress.done <= previous || round > MAX_CONTINUATIONS) return false;
    return this.store.atomic(() => {
      const current = this.store.get(
        "SELECT state,lease FROM jobs WHERE id=?",
        job.id,
      );
      if (current?.state !== "running" || current.lease !== lease) return false;
      this.store.run(
        "UPDATE jobs SET state='queued',lease=NULL,lease_until=NULL,attempts=0,phase='queued',request=? WHERE id=?",
        JSON.stringify({
          ...request,
          continuation: round,
          built: progress.done,
        }),
        job.id,
      );
      this.metrics.increment("job_continuations");
      this.store.audit("job_continued", {
        job_id: job.id,
        round,
        built: progress.done,
        of: progress.total,
      });
      this.store.afterCommit(() => setImmediate(() => void this.pump()));
      return true;
    });
  }
  /** A preview that has already been rendered, for callers that can answer
   *  with it directly instead of queueing a job that would only find it here. */
  cachedRender(tenant: string, key: string) {
    const hit = this.renderCacheHit(tenant, key);
    if (hit) this.metrics.increment("render_cache_hits");
    return hit;
  }
  /** A cached preview is only served while every artifact it lists still exists. */
  private renderCacheHit(tenant: string, key: string) {
    const row = this.store.get(
      "SELECT result,artifacts FROM render_cache WHERE tenant=? AND key=?",
      tenant,
      key,
    );
    if (!row) return null;
    const alive = (JSON.parse(row.artifacts) as string[]).every((aid) =>
      this.store.get("SELECT 1 FROM artifacts WHERE id=?", aid),
    );
    if (alive) return JSON.parse(row.result);
    this.store.run(
      "DELETE FROM render_cache WHERE tenant=? AND key=?",
      tenant,
      key,
    );
    return null;
  }
  /** What a preview still has to compute. A target whose tessellation is
   *  cached is served from the cache. The rest is tessellated from its cached
   *  B-Rep by the preview worker, or — when a target has no cached shape —
   *  evaluated by the full worker together with everything it depends on.
   *  Restoring every feature of a large model for every preview was where a
   *  render's time went; now the sandbox sees only what it draws, and every
   *  tessellation it produces is kept as JSON, as binary and as a header
   *  entry, so the next preview of that shape is concatenation, not work. */
  private planPreview(tenant: string, request: any) {
    if (request.view) return null;
    const plan = request.plan,
      byID = new Map<string, any>(plan.features.map((f: any) => [f.id, f])),
      targets: string[] = request.feature_id
        ? [request.feature_id]
        : plan.outputs,
      row = (key: string) =>
        this.store.get(
          "SELECT blob FROM cache WHERE tenant=? AND key=?",
          tenant,
          key,
        ),
      tag =
        request.adaptive || request.clip_region
          ? null
          : String(request.deflection).replace(".", "p").replace("-", "m"),
      meshKey = (f: any) =>
        tag && f.authoritative_representation === "brep"
          ? `${f.cache_key}:mesh:${tag}`
          : null;
    type Part = { json: string; bin: string; meta: any; chunk?: Buffer };
    const parts = new Map<string, Part>(),
      pending: string[] = [];
    for (const fid of targets) {
      const f = byID.get(fid);
      requireThat(f, "OUT_OF_SCOPE", "Vorschauziel fehlt im Plan.");
      const key = meshKey(f),
        json = key ? row(key) : null,
        bin = key ? row(key + ":bin") : null,
        meta = key ? row(key + ":meta") : null;
      if (json && bin && meta)
        parts.set(fid, {
          json: json.blob,
          bin: bin.blob,
          meta: JSON.parse(this.store.readBlob(meta.blob).toString("utf8")),
        });
      else pending.push(fid);
    }
    const shapeCached = (f: any) =>
      f.authoritative_representation === "brep" &&
      !!row(f.cache_key) &&
      !!row(f.cache_key + ":topology");
    let executed: any = null;
    if (pending.length && pending.every((fid) => shapeCached(byID.get(fid))))
      executed = {
        ...request,
        script: "preview",
        plan: {
          ...plan,
          features: pending.map((fid) => byID.get(fid)),
          outputs: pending,
        },
      };
    else if (pending.length) {
      const needed = new Set<string>();
      const visit = (fid: string) => {
        if (needed.has(fid)) return;
        needed.add(fid);
        for (const d of byID.get(fid)?.depends_on ?? []) visit(d);
      };
      pending.forEach(visit);
      executed = {
        ...request,
        plan: {
          ...plan,
          features: plan.features.filter((f: any) => needed.has(f.id)),
          outputs: plan.outputs.filter((fid: string) => pending.includes(fid)),
        },
      };
    }
    const keep = (fid: string, part: Part) => {
      const key = meshKey(byID.get(fid));
      if (!key) return;
      for (const [suffix, blob] of [
        ["", part.json],
        [":bin", part.bin],
        [":meta", this.store.blob(JSON.stringify(part.meta))],
      ])
        this.store.run(
          "INSERT OR IGNORE INTO cache(tenant,key,blob,created) VALUES(?,?,?,?)",
          tenant,
          key + suffix,
          blob,
          Date.now(),
        );
    };
    const fresh = (fid: string, m: any, json: string) => {
      const packed = packMesh(m),
        part: Part = {
          json,
          bin: this.store.blob(packed.chunk),
          meta: packed.meta,
          chunk: packed.chunk,
        };
      parts.set(fid, part);
      keep(fid, part);
    };
    const assemble = (result: any) => {
      let summary: any = null;
      if (result && executed.script === "preview")
        for (const fid of pending) {
          const blob = result.blobs[`${byID.get(fid).cache_key}.mesh.json`];
          requireThat(blob, "KERNEL_FAILURE", "Vorschau unvollständig.");
          fresh(
            fid,
            JSON.parse(this.store.readBlob(blob).toString("utf8")),
            blob,
          );
        }
      else if (result) {
        summary = JSON.parse(
          this.store.readBlob(result.blobs["preview.json"]).toString("utf8"),
        );
        // The full worker answers in target order; imported meshes carry no id.
        const order: string[] = request.feature_id
            ? [request.feature_id]
            : executed.plan.outputs,
          produced = new Map<string, any>(
            (summary.meshes ?? []).map((m: any, i: number) => [
              m.feature_id ?? order[i],
              m,
            ]),
          );
        for (const fid of pending) {
          const m = produced.get(fid);
          requireThat(m, "KERNEL_FAILURE", "Vorschau unvollständig.");
          const mesh = {
            ...m,
            geometry_hash: result.facts[fid]?.geometry_hash ?? null,
            engine_build: result.engine_build,
          };
          fresh(fid, mesh, this.store.blob(JSON.stringify(mesh)));
        }
      }
      const ordered = targets.map((fid) => {
        const part = parts.get(fid);
        requireThat(part, "KERNEL_FAILURE", "Vorschau unvollständig.");
        return part;
      });
      const resolutions = ordered.map((p) => p.meta.resolution).filter(Boolean);
      const extra: Record<string, unknown> = {
        ...(summary?.resolution
          ? { resolution: summary.resolution }
          : resolutions.length
            ? {
                resolution: {
                  absolute_resolution_mm: Math.max(
                    ...resolutions.map((r: any) => r.absolute_resolution_mm),
                  ),
                  finest_deflection_mm: Math.min(
                    ...resolutions.map((r: any) => r.finest_deflection_mm),
                  ),
                  reference_scale_mm: Math.max(
                    ...resolutions.map((r: any) => r.reference_scale_mm),
                  ),
                  minimum_feature_resolved_mm: Math.max(
                    ...resolutions.map(
                      (r: any) => r.minimum_feature_resolved_mm,
                    ),
                  ),
                  method: resolutions[0].method,
                  policy: resolutions[0].policy,
                  certified_surface_bound: null,
                },
              }
            : {}),
        ...(summary?.clip
          ? { clip: summary.clip }
          : request.clip_region
            ? { clip: request.clip_region }
            : {}),
      };
      const tail =
        '],"unit":"mm","quality":"preview_only"' +
        (Object.keys(extra).length
          ? "," + JSON.stringify(extra).slice(1, -1)
          : "") +
        "}";
      // A preview rendered ahead is a convenience nobody is waiting for: it
      // keeps the compact binary the viewer reads and leaves the JSON twin —
      // four times the size, written, hashed and kept for a week — unbuilt.
      const compact = request.ahead === true;
      const pieces: Buffer[] = compact ? [] : [Buffer.from('{"meshes":[')];
      if (!compact) {
        ordered.forEach((part, i) => {
          if (i) pieces.push(Buffer.from(","));
          pieces.push(this.store.readBlob(part.json));
        });
        pieces.push(Buffer.from(tail));
      }
      const packed: PackedMesh[] = ordered.map((part) => ({
          meta: part.meta,
          chunk: part.chunk ?? this.store.readBlob(part.bin),
        })),
        facts = Object.fromEntries(
          targets.map((fid) => [
            fid,
            {
              geometry_hash: parts.get(fid)!.meta.geometry_hash ?? null,
              cache_key: byID.get(fid).cache_key,
            },
          ]),
        ),
        triangle_count = ordered.reduce(
          (n, part) => n + (part.meta.triangle_count as number),
          0,
        );
      return {
        result: {
          status: "succeeded",
          facts,
          aggregate: null,
          files: compact ? [] : ["preview.json"],
          engine_build:
            result?.engine_build ?? ordered[0]?.meta.engine_build ?? "",
          metrics: {
            ...(result?.metrics ?? { seconds: 0, warm_start: true }),
            cache_hits:
              parts.size - pending.length + (result?.metrics?.cache_hits ?? 0),
            total_features: targets.length,
            tessellations_cached: targets.length - pending.length,
            tessellations_computed: pending.length,
          },
          blobs: {
            ...(result?.blobs ?? {}),
            ...(compact
              ? {}
              : { "preview.json": this.store.blob(Buffer.concat(pieces)) }),
          },
        },
        summary: {
          ...extra,
          unit: "mm",
          quality: "preview_only",
          triangle_count,
        },
        binary: packPreview(
          { unit: "mm", quality: "preview_only", ...extra },
          packed,
        ),
      };
    };
    return { request: executed, assemble };
  }
  private recordWorkerMetrics(metrics: any) {
    if (!metrics || typeof metrics !== "object") return;
    if (typeof metrics.seconds === "number")
      this.metrics.observe("worker_seconds", metrics.seconds);
    if (typeof metrics.peak_rss_kib === "number")
      this.metrics.observe("worker_peak_rss_kib", metrics.peak_rss_kib);
    if (typeof metrics.cache_hits === "number")
      this.metrics.observe("worker_cache_hits", metrics.cache_hits);
    this.metrics.increment(
      metrics.warm_start ? "worker_warm_starts" : "worker_cold_starts",
    );
    for (const sample of Object.values<any>(metrics.field_samples ?? {}))
      if (typeof sample?.retained_samples === "number")
        this.metrics.observe("field_active_cells", sample.retained_samples);
  }
  async wait(p: Principal, id: string, timeout = 60000): Promise<any> {
    const start = Date.now();
    while (Date.now() - start < timeout) {
      const j = this.get(p, id);
      if (["succeeded", "failed", "cancelled"].includes(j.status)) return j;
      await new Promise((r) => setTimeout(r, 30));
    }
    throw new Error("Job wait timeout");
  }
  cancelRevoked(jobs: string[]) {
    for (const jid of jobs) this.workers.get(jid)?.cancel();
    if (this.activeID && jobs.includes(this.activeID)) this.active?.cancel();
  }
  async close() {
    this.closed = true;
    clearInterval(this.timer);
    for (const worker of this.workers.values()) worker.cancel();
    this.active?.cancel();
    while (this.pumping) await new Promise((r) => setTimeout(r, 20));
    await Promise.allSettled([...this.running]);
    Worker.drainPool();
  }
}
