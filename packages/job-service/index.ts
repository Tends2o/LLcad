import { Store } from "../model-service/store.js";
import { Principal, authorize } from "../policy/index.js";
import { Gates } from "../../hooks/server-registry/index.js";
import { Worker } from "./worker.js";
import { id, hash } from "../semantic-ir/hash.js";
import { safeError, requireThat } from "../semantic-ir/errors.js";
import { LIMITS, REGISTRY_HASH } from "../compiler/index.js";
import { validate } from "../validation/index.js";
import { packGLB } from "../model-service/glb.js";
import { claimQueuedJob } from "./scheduler.js";
import { checkEquation } from "../compiler/constraints.js";
import { BUILD_HASH, currentBuildHash } from "../compiler/build.js";
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
  ) {
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
    this.store.run(
      "INSERT INTO outbox VALUES(?,?,?,0)",
      id("out"),
      "job_queued",
      JSON.stringify({ job_id: jid }),
    );
    setImmediate(() => void this.pump());
    return {
      status: "queued",
      job_id: jid,
      transaction_id: tx ?? null,
      committed: false,
      recommended_next_actions: ["cad_job_get"],
    };
  }
  get(p: Principal, jid: string) {
    const j = this.store.get("SELECT * FROM jobs WHERE id=?", jid);
    requireThat(j, "ACCESS_DENIED", "Job nicht zugänglich.");
    authorize(p, "model:read", j);
    return {
      job_id: j.id,
      model_id: j.model,
      transaction_id: j.tx,
      status: j.state,
      attempts: j.attempts,
      result: j.result ? JSON.parse(j.result) : null,
      error: j.error ? JSON.parse(j.error) : null,
    };
  }
  cancel(p: Principal, jid: string) {
    const j = this.store.get("SELECT * FROM jobs WHERE id=?", jid);
    requireThat(j, "ACCESS_DENIED", "Job nicht zugänglich.");
    authorize(p, "model:edit", j);
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
    if (this.activeID === jid) this.active?.cancel();
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
      const request = JSON.parse(job.request),
        p: Principal = {
          tenant: job.tenant,
          user: job.owner,
          scopes: ["model:read", "model:edit"],
        };
      let result: any;
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
        result = await this.active.run(this.store, job.tenant, {
          ...request,
          cache_owner: job.owner,
          cache_model: job.model,
        });
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
            const blob = result.blobs[f.cache_key + ".brep"];
            if (blob)
              this.store.run(
                "INSERT OR IGNORE INTO cache VALUES(?,?,?)",
                job.tenant,
                f.cache_key,
                blob,
              );
            const topology = result.blobs[f.cache_key + ".topology.json"];
            if (topology)
              this.store.run(
                "INSERT OR IGNORE INTO cache VALUES(?,?,?)",
                job.tenant,
                f.cache_key + ":topology",
                topology,
              );
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
            const preview = JSON.parse(
              this.store.readBlob(result.blobs["preview.json"]).toString(),
            );
            const glb = packGLB(preview);
            const manifest = {
              model_id: job.model,
              revision: request.revision,
              unit: "m",
              source_unit: "mm",
              quality: "preview_only",
              lost_semantics: ["parametric_history"],
              certified_surface_bound: null,
              roundtrip: glb.report,
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
              if (file.endsWith(".field.json")) continue;
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
          if (job.kind === "export")
            this.gates.run("after_export", {
              job_id: job.id,
              artifact_count: artifacts.length,
            });
          result = { status: "succeeded", artifacts, metrics: result.metrics };
        }
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
  async close() {
    this.closed = true;
    clearInterval(this.timer);
    this.active?.cancel();
    while (this.pumping) await new Promise((r) => setTimeout(r, 20));
  }
}
