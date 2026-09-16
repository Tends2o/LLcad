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
        const nativeDeadline = Date.now() + authorization.seconds * 1000;
        const effectiveExpiry = Math.min(
          nativeDeadline,
          authorization.expires ?? Infinity,
        );
        result = await worker.run(this.store, job.tenant, {
          ...request,
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
          }
          this.metrics.observe(
            "dirty_features",
            (tx.plan.dirty_features ?? []).length,
          );
          this.metrics.observe("total_features", request.plan.features.length);
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
          } else
            for (const file of result.files) {
              if (file.endsWith(".field.json") || file.endsWith(".mesh.json"))
                continue;
              if (
                file === "model.brep" &&
                (job.kind === "render" || request.format !== "brep")
              )
                continue;
              const previewSummary =
                file === "preview.json"
                  ? JSON.parse(
                      this.store.readBlob(result.blobs[file]).toString(),
                    )
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
