import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { readFileSync, writeFileSync, mkdirSync } from "node:fs";
import { resolve, join } from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import { ModelService } from "../model-service/index.js";
import { type Principal } from "../policy/index.js";
import {
  type ModelIR,
  type PatchOperation,
} from "../semantic-ir/schema.js";
import { type ToolName } from "../semantic-ir/tool-registry.js";
import { hash, bytesHash, id } from "../semantic-ir/hash.js";
import { imagePython } from "../semantic-ir/host.js";
import { body, wheel, label } from "./fixture.js";
import type { Adapter, Inspection, Issue } from "./loop.js";
import type { z } from "zod";

const execFileAsync = promisify(execFile);
/** Helpers run without a console window (the service itself has none on Windows). */
const execute = ((file: string, args: string[], options: any) =>
  execFileAsync(file, args, { windowsHide: true, ...options })) as typeof execFileAsync;
export type State = {
  model_id: string;
  revision: string;
  transaction_id?: string;
  validation_digest?: string;
};
export const save = (file: string, data: unknown) =>
  writeFileSync(file, JSON.stringify(data, null, 2) + "\n");

/** Local in-process use of the same tool dispatcher, not an HTTP MCP test. */
export class CadSession {
  readonly events: {
    tool: string;
    elapsed_ms: number;
    status: string;
    metrics?: unknown;
  }[] = [];
  constructor(
    readonly service: ModelService,
    readonly principal: Principal,
  ) {}
  async call(tool: ToolName, args: unknown, signal: AbortSignal) {
    signal.throwIfAborted();
    const started = performance.now();
    const result = await this.service.call(this.principal, tool, args);
    this.events.push({
      tool,
      elapsed_ms: performance.now() - started,
      status: result.status ?? "ok",
    });
    if (result.status === "failed")
      throw new Error(`${tool}: ${JSON.stringify(result.errors)}`);
    return result;
  }
  async job(tool: ToolName, args: unknown, signal: AbortSignal) {
    const started = performance.now();
    const queued = await this.call(tool, args, signal);
    try {
      while (true) {
        signal.throwIfAborted();
        const result = this.service.jobs.get(this.principal, queued.job_id);
        if (result.status === "succeeded") {
          this.events.push({
            tool: tool + ":job",
            elapsed_ms: performance.now() - started,
            status: result.status,
            metrics: result.result.metrics,
          });
          return {
            ...result.result,
            transaction_id:
              queued.transaction_id ?? result.result.transaction_id,
          };
        }
        if (["failed", "cancelled"].includes(result.status))
          throw new Error(`${tool}: ${JSON.stringify(result)}`);
        await delay(25, undefined, { signal });
      }
    } catch (error) {
      this.service.jobs.cancel(this.principal, queued.job_id);
      this.events.push({
        tool: tool + ":job",
        elapsed_ms: performance.now() - started,
        status: "failed_or_cancelled",
      });
      throw error;
    }
  }
  revision(state: State) {
    return this.service.store.revision(
      this.principal,
      state.model_id,
      state.revision,
    );
  }
  async importModel(ir: ModelIR, signal: AbortSignal): Promise<State> {
    const created = await this.call(
      "cad_create_model",
      {
        name: "Reference quality pilot",
        profile: ir.profile,
        idempotency_key: id("create"),
      },
      signal,
    );
    const artifact = this.service.store.artifact(
      this.principal,
      JSON.stringify(ir),
      "application/json",
      created.model_id,
      created.revision,
      { source: "synthetic reference-quality pilot" },
    );
    const binding = {
      model_id: created.model_id,
      base_revision: created.revision,
    };
    const candidate = await this.job(
      "cad_import",
      {
        ...binding,
        artifact_id: artifact.artifact_id,
        format: "ir",
        source_unit: "mm",
        idempotency_key: id("import"),
      },
      signal,
    );
    const validation = await this.job(
      "cad_validate",
      {
        ...binding,
        transaction_id: candidate.transaction_id,
        idempotency_key: id("validate"),
      },
      signal,
    );
    if (validation.status !== "checks_passed_within_profile")
      throw new Error("fixture_geometry_failed: " + JSON.stringify(validation));
    const commit = await this.call(
      "cad_commit",
      {
        ...binding,
        transaction_id: candidate.transaction_id,
        validation_digest: validation.digest,
        idempotency_key: id("commit"),
      },
      signal,
    );
    return {
      model_id: commit.model_id,
      revision: commit.revision,
      validation_digest: validation.digest,
    };
  }
  async render(
    state: State,
    output: string,
    contract: string,
    signal: AbortSignal,
  ) {
    const result = await this.job(
      "cad_render",
      {
        model_id: state.model_id,
        revision: state.revision,
        deflection: { value: "0.05", unit: "mm" },
        idempotency_key: id("render"),
      },
      signal,
    );
    let preview: unknown;
    for (const artifact of result.artifacts) {
      if (artifact.mime !== "application/json") continue;
      const row = this.service.store.getArtifact(
        this.principal,
        artifact.artifact_id,
      );
      const data = JSON.parse(this.service.store.readBlob(row.hash).toString());
      if (Array.isArray(data.meshes)) {
        preview = data;
        break;
      }
    }
    if (!preview) throw new Error("missing_rendered_meshes");
    const meshFile = output + ".meshes.json";
    save(meshFile, preview);
    await imageCommand(["render", contract, meshFile, output], signal);
  }
}

export async function imageCommand(args: string[], signal: AbortSignal) {
  const { stdout } = await execute(
    imagePython(resolve(".")),
    [resolve("packages/reference-quality/image_check.py"), ...args],
    { signal, timeout: 60_000, maxBuffer: 2_000_000 },
  );
  return stdout.trim() ? JSON.parse(stdout) : null;
}

/** Narrow corrector for the public fixture specification, never arbitrary CAD. */
export class FixtureAdapter implements Adapter<State> {
  private pending: State | undefined;
  private repairIds = new Set<string>();
  private sequence = 0;
  private readonly referenceHash: string;
  private readonly contractHash: string;
  constructor(
    readonly cad: CadSession,
    readonly base: State,
    readonly referencePng: string,
    readonly contractFile: string,
    readonly folder: string,
  ) {
    mkdirSync(folder, { recursive: true });
    this.referenceHash = bytesHash(readFileSync(referencePng));
    this.contractHash = bytesHash(readFileSync(contractFile));
  }

  private checkFrozenInputs() {
    if (
      bytesHash(readFileSync(this.referencePng)) !== this.referenceHash ||
      bytesHash(readFileSync(this.contractFile)) !== this.contractHash
    )
      throw new Error("reference_or_contract_changed_during_run");
  }

  async inspect(state: State, signal: AbortSignal): Promise<Inspection> {
    this.checkFrozenInputs();
    const prefix = join(this.folder, `check-${this.sequence++}`);
    let geometryPassed =
      this.cad.revision(state).quality === "checks_passed_within_profile";
    if (state.transaction_id) {
      const validation = await this.cad.job(
        "cad_validate",
        {
          model_id: state.model_id,
          base_revision: this.base.revision,
          transaction_id: state.transaction_id,
          idempotency_key: id("validate"),
        },
        signal,
      );
      save(prefix + ".geometry.json", validation);
      geometryPassed = validation.status === "checks_passed_within_profile";
      state.validation_digest = validation.digest;
    }
    if (!geometryPassed)
      return {
        complete: false,
        geometry_passed: false,
        issues: [],
        evidence: prefix + ".geometry.json",
      };
    await this.cad.render(state, prefix + ".png", this.contractFile, signal);
    this.checkFrozenInputs();
    const checked = await imageCommand(
      [
        "check",
        this.contractFile,
        this.referencePng,
        prefix + ".png",
        prefix + ".diff.png",
      ],
      signal,
    );
    save(prefix + ".json", {
      ...checked,
      model_id: state.model_id,
      revision: state.revision,
      validation_digest: state.validation_digest,
      geometry_passed: geometryPassed,
    });
    return {
      complete: checked.complete === true,
      geometry_passed: geometryPassed,
      issues: checked.issues,
      evidence: prefix + ".json",
    };
  }

  async correct(
    _state: State,
    issues: Issue[],
    signal: AbortSignal,
  ): Promise<State> {
    for (const issue of issues) {
      if (["housing-width", "wheel", "label"].includes(issue.id))
        this.repairIds.add(issue.id);
      else if (issue.id !== "whole-view")
        throw new Error("unsupported_issue:" + issue.id);
    }
    const baseIR: ModelIR = this.cad.revision(this.base).ir;
    const operations: z.infer<typeof PatchOperation>[] = [];
    const outputs = new Set(baseIR.outputs);
    for (const issue of this.repairIds) {
      const target =
        issue === "housing-width" ? body : issue === "wheel" ? wheel : label;
      const actual = baseIR.features.find((f) => f.id === target.id);
      if (!actual) operations.push({ op: "add_feature", feature: target });
      else if (issue === "label") {
        if (hash(actual.construction) !== hash(target.construction))
          operations.push({
            op: "set_construction",
            feature_id: target.id,
            expected_hash: hash(actual.construction),
            construction: target.construction,
          });
      } else {
        const keys =
          issue === "housing-width"
            ? ["width"]
            : Object.keys(target.parameters);
        for (const key of keys) {
          if (!actual.parameters[key])
            throw new Error("unsupported_missing_parameter:" + key);
          if (hash(actual.parameters[key]) !== hash(target.parameters[key]))
            operations.push({
              op: "set_parameter",
              feature_id: target.id,
              parameter: key,
              expected: actual.parameters[key],
              value: target.parameters[key],
            });
        }
      }
      outputs.add(target.id);
    }
    if (outputs.size !== baseIR.outputs.length)
      operations.push({ op: "set_outputs", outputs: [...outputs] });
    if (!operations.length)
      throw new Error("no_supported_correction_for_visual_difference");
    await this.cleanup();
    const args = {
      model_id: this.base.model_id,
      base_revision: this.base.revision,
      operations,
      idempotency_key: id("correct"),
    };
    // Preserve the planned transaction for cancellation even if the CAD job fails.
    const planned = await this.cad.call(
      "cad_plan_edit",
      { ...args, idempotency_key: id("plan") },
      signal,
    );
    save(join(this.folder, `patch-${this.sequence}.json`), {
      operations,
      plan: planned,
    });
    const draft = await this.cad.call("cad_apply_patch", args, signal);
    const jobStarted = performance.now();
    this.pending = {
      model_id: this.base.model_id,
      revision: this.base.revision,
      transaction_id: draft.transaction_id,
    };
    try {
      while (true) {
        signal.throwIfAborted();
        const job = this.cad.service.jobs.get(this.cad.principal, draft.job_id);
        if (job.status === "succeeded") {
          this.cad.events.push({
            tool: "cad_apply_patch:job",
            elapsed_ms: performance.now() - jobStarted,
            status: job.status,
            metrics: job.result.metrics,
          });
          this.pending.revision = job.result.candidate_revision;
          return { ...this.pending };
        }
        if (["failed", "cancelled"].includes(job.status))
          throw new Error("correction_job_failed:" + JSON.stringify(job));
        await delay(25, undefined, { signal });
      }
    } catch (error) {
      this.cad.service.jobs.cancel(this.cad.principal, draft.job_id);
      this.cad.events.push({
        tool: "cad_apply_patch:job",
        elapsed_ms: performance.now() - jobStarted,
        status: "failed_or_cancelled",
      });
      throw error;
    }
  }

  async accept(state: State, signal: AbortSignal): Promise<State> {
    signal.throwIfAborted();
    this.checkFrozenInputs();
    if (!state.transaction_id) {
      if (
        this.cad.service.store.model(this.cad.principal, state.model_id)
          .head !== state.revision
      )
        throw new Error("stale_revision");
      return state;
    }
    const before = this.cad.revision(state);
    const committed = await this.cad.call(
      "cad_commit",
      {
        model_id: state.model_id,
        base_revision: this.base.revision,
        transaction_id: state.transaction_id,
        validation_digest: state.validation_digest,
        idempotency_key: id("commit"),
      },
      signal,
    );
    this.pending = undefined;
    const result = {
      model_id: state.model_id,
      revision: committed.revision,
      validation_digest: state.validation_digest,
    };
    const after = this.cad.revision(result);
    if (
      before.ir_hash !== after.ir_hash ||
      hash(before.geometry.facts) !== hash(after.geometry.facts)
    )
      throw new Error("committed_revision_differs_from_checked_candidate");
    save(join(this.folder, "accepted.json"), {
      ...result,
      checked_candidate: state.revision,
      ir_hash: after.ir_hash,
      geometry_hash: hash(after.geometry.facts),
    });
    return result;
  }

  async cleanup() {
    if (this.pending) {
      const result = await this.cad.service.call(
        this.cad.principal,
        "cad_discard",
        {
          model_id: this.base.model_id,
          base_revision: this.base.revision,
          transaction_id: this.pending.transaction_id,
          idempotency_key: id("discard"),
        },
      );
      if (result.status === "failed")
        throw new Error("discard_failed:" + JSON.stringify(result.errors));
      this.pending = undefined;
    }
  }
}
