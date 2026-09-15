import { Store } from "../model-service/store.js";
import { Principal } from "../policy/index.js";
import { Gates } from "../../hooks/server-registry/index.js";
import { Worker } from "./worker.js";
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
export class Jobs {
  private active: Worker | null = null;
  private activeID: string | null = null;
  private pumping = false;
  private closed = false;
  private timer: NodeJS.Timeout;
  constructor(
    public store: Store,
    public gates: Gates,
  ) {
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
    );
    const jid = id("job");
    this.store.run(
      "INSERT INTO jobs(id,tenant,owner,model,tx,kind,state,request,created) VALUES(?,?,?,?,?,?,?,?,?)",
      jid,
      p.tenant,
      p.user,
      model,
      tx ?? null,
      kind,
      "queued",
      JSON.stringify(request),
      new Date().toISOString(),
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
    const result = {
      job_id: j.id,
      model_id: j.model,
      transaction_id: j.tx,
      status: j.state,
      attempts: j.attempts,
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
      "UPDATE jobs SET state='cancelled',lease=NULL,lease_until=NULL WHERE id=?",
      jid,
    );
    if (j.tx)
      this.store.run(
        "UPDATE transactions SET state=? WHERE id=? AND state!=?",
        j.kind === "validate" ? "candidate_ready" : "aborted",
        j.tx,
        "committed",
      );
    this.store.afterCommit(() => {
      if (this.activeID === jid) this.active?.cancel();
    });
    this.store.audit("job_cancelled", { job_id: jid });
    return this.get(p, jid);
  }
  async pump() {
    if (this.closed || this.pumping) return;
    this.pumping = true;
    let job: any;
    const lease = id("lease");
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
      job = claimQueuedJob(this.store, lease);
      if (!job) return;
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
      const authorization = this.store.access.checkJob(job);
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
        this.active = new Worker();
        this.activeID = job.id;
        const nativeDeadline = Date.now() + authorization.seconds * 1000;
        const effectiveExpiry = Math.min(
          nativeDeadline,
          authorization.expires ?? Infinity,
        );
        result = await this.active.run(this.store, job.tenant, {
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
            const checked = await this.active.run(this.store, job.tenant, {
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
        this.store.access.checkJob(job);
        for (const f of request.plan?.features ?? []) {
          const blob = result.blobs?.[f.cache_key + ".field.json"];
          if (blob)
            this.store.run(
              "INSERT OR REPLACE INTO cache VALUES(?,?,?)",
              job.tenant,
              "field:" + hash([job.owner, job.model, f.id]),
              blob,
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
                  "INSERT OR IGNORE INTO cache VALUES(?,?,?)",
                  job.tenant,
                  key,
                  blob,
                );
              const topology = result.blobs[key + ".topology.json"];
              if (topology)
                this.store.run(
                  "INSERT OR IGNORE INTO cache VALUES(?,?,?)",
                  job.tenant,
                  key + ":topology",
                  topology,
                );
            }
          }
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
                certified_surface_bound: null,
                roundtrip: result.blobs["roundtrip.json"]
                  ? JSON.parse(
                      this.store
                        .readBlob(result.blobs["roundtrip.json"])
                        .toString(),
                    )
                  : null,
              };
              artifacts.push(
                this.store.artifact(
                  p,
                  this.store.readBlob(result.blobs[file]),
                  file.endsWith(".json")
                    ? "application/json"
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
          "UPDATE jobs SET state='succeeded',result=?,lease=NULL,lease_until=NULL WHERE id=? AND lease=?",
          JSON.stringify(result),
          job.id,
          lease,
        );
        this.store.run(
          "UPDATE outbox SET delivered=1 WHERE event='job_queued' AND json_extract(payload,'$.job_id')=?",
          job.id,
        );
      });
    } catch (error) {
      if (job && !this.closed)
        this.store.atomic(() => {
          const current = this.store.get(
            "SELECT state,lease FROM jobs WHERE id=?",
            job.id,
          );
          if (current?.state !== "running" || current.lease !== lease) return;
          this.store.run(
            "UPDATE jobs SET state='failed',error=?,lease=NULL,lease_until=NULL WHERE id=?",
            JSON.stringify(safeError(error)),
            job.id,
          );
          if (job.tx)
            this.store.run(
              "UPDATE transactions SET state=? WHERE id=? AND state!=?",
              job.kind === "validate" ? "validation_failed" : "failed",
              job.tx,
              "committed",
            );
          this.store.audit("job_failed", {
            job_id: job.id,
            code: safeError(error).code,
          });
        });
    } finally {
      this.active = null;
      this.activeID = null;
      this.pumping = false;
    }
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
    if (this.activeID && jobs.includes(this.activeID)) this.active?.cancel();
  }
  async close() {
    this.closed = true;
    clearInterval(this.timer);
    this.active?.cancel();
    while (this.pumping) await new Promise((r) => setTimeout(r, 20));
  }
}
