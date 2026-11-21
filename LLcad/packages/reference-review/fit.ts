import { mkdirSync, rmSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import type { ModelService } from "../model-service/index.js";
import type { Principal } from "../policy/index.js";
import { hash, id } from "../semantic-ir/hash.js";
import { imagePython } from "../semantic-ir/host.js";
import { CadError, requireThat, safeError } from "../semantic-ir/errors.js";
import { checkedToolResponse, failureResponse } from "../model-service/result-contracts.js";
import { readStoredPreview } from "../model-service/stored-preview.js";
import { Contract, isAbsolute } from "./schema.js";
import { FitObjective } from "./fit-schema.js";
import { limiter } from "./vision.js";

const execFileAsync = promisify(execFile);
/** Helpers run without a console window (the service itself has none on Windows). */
const execute = ((file: string, args: string[], options: any) =>
  execFileAsync(file, args, { windowsHide: true, ...options })) as typeof execFileAsync;
const PREVIEW_MIME = "model/x-mathforge-preview";

/* --- cad_fit_parameters ---------------------------------------------------------
Server-side fitting of declared model variables against registered reference views.
Tier 1: a 2D stand-in built from real candidate builds (the silhouette SDF at x0 and at
one finite-difference step per variable, linear in between; fit.py), searched with
bounded Powell. Tier 3: the optimum is built as a real candidate and measured. The
result is a proposal of set_variable operations with before/after metrics. Every
candidate the fit creates is discarded again; the fit never commits. */

type Variable = {
  name: string;
  min: number;
  max: number;
  step?: number;
  unit: string;
  current: { value: string; unit: string };
  x0: number;
};
const fmt = (x: number) => {
  const text = x.toFixed(6).replace(/(\.\d*?)0+$/, "$1").replace(/\.$/, "");
  return text === "-0" ? "0" : text;
};

export class ParameterFits {
  private running = new Map<string, { controller: AbortController; done: Promise<void> }>();
  /** Whether a fit is in progress. */
  busy() {
    return this.running.size > 0;
  }
  constructor(private service: ModelService) {
    const store = service.store;
    store.run(
      "CREATE TABLE IF NOT EXISTS fit_runs(id TEXT PRIMARY KEY,model TEXT NOT NULL REFERENCES models(id) ON DELETE CASCADE,tenant TEXT NOT NULL,owner TEXT NOT NULL,key TEXT NOT NULL,created TEXT NOT NULL,record TEXT NOT NULL)",
    );
    store.run("CREATE INDEX IF NOT EXISTS fit_runs_key ON fit_runs(model,key,created)");
    for (const row of store.all("SELECT id,record FROM fit_runs WHERE json_extract(record,'$.state')='running'")) {
      const run = JSON.parse(row.record);
      run.state = "failed";
      run.error = { code: "REVIEW_INTERRUPTED", message: "Parameter fit interrupted by a service restart." };
      this.save(run);
    }
  }
  private save(run: any) {
    this.service.store.run("UPDATE fit_runs SET record=? WHERE id=?", JSON.stringify(run), run.id);
  }
  private load(model: string, fitId: string) {
    const row = this.service.store.get("SELECT record FROM fit_runs WHERE id=? AND model=?", fitId, model);
    requireThat(row, "NOT_FOUND", "Parameter fit not found.");
    return JSON.parse(row.record);
  }
  /** Asynchronous like the viewer tools: the call waits (bounded) for the fit. */
  async call(p: Principal, a: any, trace: string) {
    try {
      const store = this.service.store;
      const m = store.model(p, a.model_id, "model:edit");
      let run: any;
      if (a.fit_id) run = this.load(a.model_id, a.fit_id);
      else {
        const base = store.revision(p, a.model_id, a.revision ?? m.head);
        requireThat(base.id === m.head, "STALE_REVISION", "Parameters are fitted on the current model state (revision = head).");
        const variables = this.variables(base, a.variables);
        const contractRow = store.get("SELECT contract FROM reference_contracts WHERE model=?", a.model_id);
        requireThat(contractRow, "REFERENCE_REGISTRATION_REQUIRED", "No reference contract: run cad_reference_setup with registered references first.");
        const contract = Contract.parse(JSON.parse(contractRow.contract));
        const views = a.views.map((rid: string) => {
          const ref = contract.references.find((r) => r.id === rid);
          requireThat(ref, "INVALID_SCHEMA", `Unknown reference ${rid}.`);
          requireThat(
            ref!.registered && isAbsolute(ref!.camera),
            "REFERENCE_REGISTRATION_REQUIRED",
            `Reference ${rid} needs a registered absolute camera (cad_reference_register).`,
          );
          const artifact = store.getArtifact(p, ref!.artifact_id);
          requireThat(artifact.hash === contract.reference_hashes[rid], "INTEGRITY_FAILURE", "The reference image was replaced.");
          return { reference_id: rid, camera: ref!.camera, artifact_id: ref!.artifact_id };
        });
        const objective = FitObjective.parse(a.objective ?? {});
        const features = new Set(base.ir.features.map((f: any) => f.id));
        requireThat(
          objective.landmarks.every((l) => features.has(l.feature_id) && a.views.includes(l.reference_id)) &&
            objective.exclusions.every((x) => a.views.includes(x.reference_id)),
          "INVALID_SCHEMA",
          "Landmarks and exclusions must refer to features of the revision and to selected views.",
        );
        const key = hash({
          revision: base.id,
          variables,
          views,
          objective,
          max_evals: a.max_evals,
          confirm: a.confirm,
          resolution: a.resolution_px,
          contract: contract.hash,
        });
        const previous = store.get(
          "SELECT record FROM fit_runs WHERE model=? AND key=? AND json_extract(record,'$.state') IN ('running','succeeded') ORDER BY created DESC LIMIT 1",
          a.model_id,
          key,
        );
        if (previous) run = { ...JSON.parse(previous.record), cached: true };
        else {
          requireThat(
            this.running.size < 1,
            "BUDGET_EXCEEDED",
            "A parameter fit is already running; query its state with fit_id.",
          );
          run = {
            id: id("fit"),
            model: a.model_id,
            revision: base.id,
            state: "running",
            stage: "queued",
            cached: false,
            created_at: new Date().toISOString(),
            variables,
            views,
            objective,
            max_evals: a.max_evals,
            confirm: a.confirm,
            resolution: a.resolution_px,
            warnings: [],
            samples: 0,
            iterations: 0,
          };
          store.run(
            "INSERT INTO fit_runs(id,model,tenant,owner,key,created,record) VALUES(?,?,?,?,?,?,?)",
            run.id,
            a.model_id,
            p.tenant,
            p.user,
            key,
            run.created_at,
            JSON.stringify(run),
          );
          const controller = new AbortController();
          const done = this.execute(p, run, controller.signal).finally(() => this.running.delete(run.id));
          this.running.set(run.id, { controller, done });
        }
      }
      const deadline = Date.now() + (a.wait_ms ?? 100000);
      while (run.state === "running" && Date.now() < deadline) {
        await new Promise((r) => setTimeout(r, 250));
        run = { ...this.load(a.model_id, run.id), cached: run.cached };
      }
      if (run.state === "failed")
        return failureResponse(new CadError(run.error?.code ?? "KERNEL_FAILURE", run.error?.message ?? "Parameter fit failed.", { fit_id: run.id }), trace);
      return checkedToolResponse("cad_fit_parameters" as any, this.payload(run), trace);
    } catch (error) {
      return failureResponse(error, trace);
    }
  }
  /** Declared variables against the IR's model variables (unit, bounds, start). */
  private variables(base: any, declared: any[]): Variable[] {
    const irVars = base.ir.variables ?? {};
    requireThat(new Set(declared.map((v) => v.name)).size === declared.length, "INVALID_SCHEMA", "Variables must be unique.");
    return declared.map((v) => {
      const current = irVars[v.name];
      requireThat(current, "INVALID_SCHEMA", `Model variable ${v.name} is missing; create it with set_variable first and use it in expressions.`);
      const x0 = Number(current.value);
      requireThat(
        Number.isFinite(x0) && v.min < v.max && (!v.step || v.step < v.max - v.min),
        "INVALID_SCHEMA",
        `Invalid bounds for ${v.name}.`,
      );
      return { name: v.name, min: v.min, max: v.max, ...(v.step ? { step: v.step } : {}), unit: current.unit, current, x0 };
    });
  }
  private payload(run: any) {
    const done = run.state === "succeeded";
    return {
      status: done ? "succeeded" : "running",
      model_id: run.model,
      revision: run.revision,
      fit_id: run.id,
      stage: run.stage,
      cached: !!run.cached,
      variables: run.variables.map((v: Variable, i: number) => ({
        name: v.name,
        unit: v.unit,
        min: v.min,
        max: v.max,
        before: v.x0,
        after: run.x_best ? run.x_best[i] : null,
      })),
      patch: done ? (run.patch ?? null) : null,
      metrics: {
        before: run.before ?? null,
        standin_after: run.standin_after ?? null,
        confirmed_after: run.confirmed_after ?? null,
      },
      tiers: {
        samples: run.samples ?? 0,
        standin: run.standin ?? null,
        confirmation: run.confirmation ?? null,
        iterations: run.iterations ?? 0,
      },
      elapsed_ms: run.elapsed_ms ?? Date.now() - Date.parse(run.created_at),
      committed: false as const,
      ...(run.warnings?.length ? { warnings: run.warnings } : {}),
    };
  }
  private async python(action: string, spec: any, folder: string, signal: AbortSignal) {
    const root = resolve(process.env.MATHFORGE_ROOT ?? process.cwd());
    const file = join(folder, `${action}-${id("spec")}.json`);
    writeFileSync(file, JSON.stringify(spec), { mode: 0o600 });
    const { stdout } = await execute(
      imagePython(root),
      [join(root, "packages/reference-review/fit.py"), action, file],
      { signal, timeout: 600000, maxBuffer: 4 * 1024 * 1024 },
    );
    return JSON.parse(stdout);
  }
  private async execute(p: Principal, run: any, parent: AbortSignal) {
    const started = Date.now();
    const signal = AbortSignal.any([parent, AbortSignal.timeout(1_800_000)]);
    const store = this.service.store;
    const folder = join(store.root, "fit-work", run.id);
    mkdirSync(folder, { recursive: true, mode: 0o700 });
    const stage = (s: string) => {
      signal.throwIfAborted();
      run.stage = s;
      this.save(run);
    };
    const tool = async (name: string, args: any) => {
      signal.throwIfAborted();
      const r = await this.service.call(p, name as any, args);
      if (r?.status === "failed") {
        const e = r.errors?.[0] ?? {};
        throw new CadError(e.code ?? "KERNEL_FAILURE", `${name}: ${e.message ?? "failed"}`.slice(0, 400));
      }
      return r;
    };
    const waitJob = async (jobId: string) => {
      const deadline = Date.now() + 900_000;
      for (;;) {
        signal.throwIfAborted();
        const job = this.service.jobs.get(p, jobId);
        if (job.status === "succeeded") return job.result;
        requireThat(!["failed", "cancelled"].includes(job.status), "KERNEL_FAILURE", `Candidate job ${job.status}.`, {
          job_id: jobId,
        });
        requireThat(Date.now() < deadline, "KERNEL_FAILURE", "The candidate job exceeded the time budget.");
        await new Promise((r) => setTimeout(r, 200));
      }
    };
    /** Preview mesh file of a revision (stored preview, else a render job). */
    const preview = async (revision: string, name: string) => {
      let result: any = readStoredPreview(store, p, run.model, revision);
      if (!result) {
        result = await tool("cad_render", {
          model_id: run.model,
          revision,
          idempotency_key: id("fit-render"),
        });
        if (result.job_id) result = await waitJob(result.job_id);
      }
      const artifacts: any[] = result.artifacts ?? [];
      const chosen =
        artifacts.find((x) => x.mime === PREVIEW_MIME) ??
        artifacts.find((x) => x.manifest?.filename === "preview.json");
      requireThat(chosen, "KERNEL_FAILURE", "Preview without a mesh.");
      const bytes = store.readBlob(store.getArtifact(p, chosen.artifact_id).hash);
      const file = join(folder, name + (chosen.mime === PREVIEW_MIME ? ".bin" : ".json"));
      writeFileSync(file, bytes, { mode: 0o600 });
      return file;
    };
    const anchors = (revision: string) => {
      const facts = store.revision(p, run.model, revision).geometry?.facts ?? {};
      return Object.fromEntries(
        [...new Set<string>(run.objective.landmarks.map((l: any) => l.feature_id))].flatMap((fid) => {
          const b = facts[fid]?.bounds;
          return Array.isArray(b) && b.length === 6 ? [[fid, [(b[0] + b[3]) / 2, (b[1] + b[4]) / 2, (b[2] + b[5]) / 2]]] : [];
        }),
      );
    };
    const vars: Variable[] = run.variables;
    /** One real candidate at x: build, measure its preview, discard it again. */
    const sample = async (x: number[], label: string) => {
      const operations = vars.map((v, i) => ({
        op: "set_variable",
        name: v.name,
        expected: v.current,
        value: { value: fmt(x[i]), unit: v.unit },
      }));
      const started = await tool("cad_apply_patch", {
        model_id: run.model,
        base_revision: run.revision,
        operations,
        idempotency_key: id("fit-candidate"),
      });
      try {
        if (started.job_id) await waitJob(started.job_id);
        const file = await preview(started.candidate_revision, label);
        return { x, preview: file, anchors: anchors(started.candidate_revision), revision: started.candidate_revision };
      } finally {
        try {
          await tool("cad_discard", {
            model_id: run.model,
            base_revision: run.revision,
            transaction_id: started.transaction_id,
            idempotency_key: id("fit-discard"),
          });
        } catch {
          run.warnings.push(`Candidate ${started.transaction_id} could not be discarded.`);
        }
      }
    };
    const viewsSpec = () =>
      run.views.map((v: any) => {
        const file = join(folder, "reference-" + v.reference_id);
        writeFileSync(file, store.readBlob(store.getArtifact(p, v.artifact_id).hash), { mode: 0o600 });
        return {
          reference_id: v.reference_id,
          camera: v.camera,
          source: file,
          exclusion_boxes: run.objective.exclusions.filter((x: any) => x.reference_id === v.reference_id).map((x: any) => x.box),
        };
      });
    try {
      stage("samples");
      const views = viewsSpec();
      const pool = limiter(Math.max(1, Math.min(2, this.service.jobs.concurrency)));
      const linearise = async (center: number[], tag: string, shrink = 1) => {
        const steps = vars.map((v) => {
          const d = (v.step ?? 0.08 * (v.max - v.min)) * shrink;
          return center[vars.indexOf(v)] + d > v.max ? -d : d;
        });
        const base =
          tag === "0"
            ? { x: center, preview: await preview(run.revision, "sample-0"), anchors: anchors(run.revision) }
            : await sample(center, `sample-${tag}-0`);
        const others = await Promise.all(
          vars.map((_, i) =>
            pool(() => {
              const x = [...center];
              x[i] += steps[i];
              return sample(x, `sample-${tag}-${i + 1}`);
            }),
          ),
        );
        run.samples += others.length + (tag === "0" ? 0 : 1);
        this.save(run);
        return [base, ...others];
      };
      let center = vars.map((v) => v.x0);
      let samples = await linearise(center, "0");
      const objective = {
        silhouette_weight: run.objective.silhouette_weight,
        chamfer_weight: run.objective.chamfer_weight,
        landmark_weight: run.objective.landmark_weight,
        landmarks: run.objective.landmarks,
      };
      let result: any = null;
      let best: any = null;
      for (let iteration = 1; iteration <= 2; iteration++) {
        run.iterations = iteration;
        stage("standin");
        result = await this.python(
          "standin",
          {
            views,
            samples: samples.map((s) => ({ x: s.x, preview: s.preview, anchors: s.anchors })),
            variables: vars.map((v, i) => ({ name: v.name, min: v.min, max: v.max, x0: center[i] })),
            objective,
            max_evals: run.max_evals,
            resolution: run.resolution,
          },
          folder,
          signal,
        );
        if (iteration === 1) run.before = result.before;
        run.x_best = result.x_best;
        run.standin_after = result.standin_after;
        run.standin = {
          method: result.method,
          evals: result.evals,
          seconds: result.seconds,
          extrapolation_steps: result.extrapolation_steps,
        };
        this.save(run);
        if (!run.confirm) break;
        stage("confirmation");
        const confirmed = await sample(result.x_best, `confirm-${iteration}`);
        const measured = await this.python(
          "measure",
          { views, preview: confirmed.preview, anchors: confirmed.anchors, objective, resolution: run.resolution },
          folder,
          signal,
        );
        run.samples += 1;
        const agrees = measured.objective <= result.standin_after.objective * 1.1 + 0.005;
        if (!best || measured.objective < best.measured.objective)
          best = {
            measured,
            x: result.x_best,
            standin_after: result.standin_after,
            confirmation: { candidate_revision: confirmed.revision, objective: measured.objective, agrees_with_standin: agrees },
          };
        run.x_best = best.x;
        run.standin_after = best.standin_after;
        run.confirmed_after = best.measured;
        run.confirmation = best.confirmation;
        this.save(run);
        // The stand-in is linear around its samples; far from them (or where it
        // disagrees with the real build) it drifts. One re-linearisation at the
        // optimum with half the step, then the better confirmed iterate wins.
        if (iteration === 2 || (agrees && result.extrapolation_steps <= 1)) break;
        stage("relinearise");
        center = result.x_best;
        samples = await linearise(center, String(iteration), 0.5);
      }
      const real = run.confirmed_after?.objective ?? null;
      const improved = real === null ? result.standin_after.objective < run.before.objective : real < run.before.objective - 1e-6;
      if (real === null)
        run.warnings.push("Tier 3 skipped (confirm false): the after metrics are stand-in predictions only.");
      if (!improved) run.warnings.push("No confirmed improvement over the current values; no patch is proposed.");
      run.patch = improved
        ? {
            operations: vars.map((v, i) => ({
              op: "set_variable",
              name: v.name,
              expected: v.current,
              value: { value: fmt(run.x_best[i]), unit: v.unit },
            })),
          }
        : null;
      run.state = "succeeded";
      run.stage = "complete";
      run.elapsed_ms = Date.now() - started;
      this.save(run);
      store.audit("parameters_fitted", {
        model_id: run.model,
        revision: run.revision,
        fit_id: run.id,
        before: run.before?.objective ?? null,
        after: real,
      });
    } catch (error) {
      run.state = "failed";
      run.stage = "failed";
      run.error = safeError(error);
      run.elapsed_ms = Date.now() - started;
      this.save(run);
    } finally {
      rmSync(folder, { recursive: true, force: true });
    }
  }
  async close() {
    for (const r of this.running.values()) r.controller.abort(new Error("Service closing"));
    await Promise.allSettled([...this.running.values()].map((r) => r.done));
  }
}
