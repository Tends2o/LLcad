/** Fixture minimiser (Bauplan 24.2): shrink a failing IR to a 1-minimal reproduction.
 *
 * Usage: tsx scripts/minimize-fixture.ts <fixture.json> --expect <ERROR_CODE> [--oracle compile|evaluate] [--out minimized.json]
 *
 * The oracle decides whether a candidate IR still fails with the expected code: `compile` runs the
 * strict compiler only (fast, no worker); `evaluate` runs the native evaluation through a private
 * temporary service. Features are removed together with their dependents and referencing
 * constraints, then constraints and assumptions individually, until no single removal keeps the
 * failure. Nothing is uploaded or shared; the result is written locally.
 */
import { readFileSync, writeFileSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { compile } from "../packages/compiler/index.js";

export type Oracle = (ir: any) => Promise<string | null> | string | null;

/** Error code of the strict compiler for this IR, or null when it compiles. */
export function compileOracle(ir: any): string | null {
  try {
    compile(ir);
    return null;
  } catch (error: any) {
    return error?.code ?? "UNKNOWN";
  }
}

function dependents(ir: any, removed: Set<string>) {
  let changed = true;
  while (changed) {
    changed = false;
    for (const f of ir.features)
      if (
        !removed.has(f.id) &&
        (f.depends_on ?? []).some((d: string) => removed.has(d))
      ) {
        removed.add(f.id);
        changed = true;
      }
  }
  return removed;
}

function withoutFeature(ir: any, fid: string) {
  const removed = dependents(ir, new Set([fid]));
  const features = ir.features.filter((f: any) => !removed.has(f.id));
  const constraints = (ir.constraints ?? []).filter(
    (c: any) =>
      !removed.has(c.feature_id) &&
      !removed.has(c.neighbor_feature_id) &&
      !removed.has(c.other_feature_id),
  );
  const outputs = ir.outputs.filter((o: string) => !removed.has(o));
  const leaves = features
    .filter(
      (f: any) =>
        !features.some((g: any) => (g.depends_on ?? []).includes(f.id)),
    )
    .map((f: any) => f.id);
  const structure = ir.structure
    ? {
        ...ir.structure,
        parts: ir.structure.parts.map((p: any) => ({
          ...p,
          outputs: p.outputs.filter((o: string) => !removed.has(o)),
        })),
      }
    : undefined;
  return {
    ...ir,
    features,
    constraints,
    outputs: outputs.length ? outputs : leaves,
    ...(structure ? { structure } : {}),
  };
}

export async function minimize(
  ir: any,
  expected: string,
  oracle: Oracle,
  log: (line: string) => void = () => {},
) {
  let current = structuredClone(ir);
  const initial = await oracle(current);
  if (initial !== expected)
    throw new Error(
      `Die Eingabe erzeugt ${initial ?? "keinen Fehler"}, erwartet wurde ${expected}.`,
    );
  let steps = 0;
  let progress = true;
  while (progress) {
    progress = false;
    for (const f of [...current.features]) {
      const candidate = withoutFeature(current, f.id);
      if (!candidate.features.length) continue;
      steps++;
      if ((await oracle(candidate)) === expected) {
        log(
          `Feature ${f.id} entfernt (${candidate.features.length} verbleiben).`,
        );
        current = candidate;
        progress = true;
        break;
      }
    }
    if (progress) continue;
    for (let i = 0; i < (current.constraints ?? []).length; i++) {
      const candidate = {
        ...current,
        constraints: current.constraints.filter((_: any, j: number) => j !== i),
      };
      steps++;
      if ((await oracle(candidate)) === expected) {
        log(`Constraint ${current.constraints[i].id} entfernt.`);
        current = candidate;
        progress = true;
        break;
      }
    }
    if (progress) continue;
    if ((current.assumptions ?? []).length) {
      const candidate = { ...current, assumptions: [] };
      steps++;
      if ((await oracle(candidate)) === expected) {
        current = candidate;
        progress = true;
      }
    }
  }
  return {
    ir: current,
    steps,
    features: current.features.length,
    constraints: (current.constraints ?? []).length,
  };
}

async function evaluateOracle(): Promise<{
  oracle: Oracle;
  close: () => Promise<void>;
}> {
  const { ModelService } = await import("../packages/model-service/index.js");
  const { SCOPES } = await import("../packages/policy/index.js");
  const { id } = await import("../packages/semantic-ir/hash.js");
  const dir = mkdtempSync(join(tmpdir(), "mathforge-minimize-"));
  const service = new ModelService(dir);
  const principal = { tenant: "minimizer", user: "minimizer", scopes: SCOPES };
  const oracle: Oracle = async (ir) => {
    const compiled = compileOracle(ir);
    if (compiled) return compiled;
    const m = service.call(principal, "cad_create_model", {
      name: "minimize",
      profile: ir.profile,
      idempotency_key: id("create"),
    });
    const upload = service.store.artifact(
      principal,
      JSON.stringify(ir),
      "application/json",
      m.model_id,
      m.revision,
      {},
    );
    const draft = service.call(principal, "cad_import", {
      model_id: m.model_id,
      base_revision: m.revision,
      artifact_id: upload.artifact_id,
      format: "ir",
      source_unit: "mm",
      idempotency_key: id("import"),
    });
    if (draft.status === "failed") return draft.errors[0].code;
    const job = await service.jobs.wait(principal, draft.job_id);
    return job.status === "failed" ? job.error.code : null;
  };
  return {
    oracle,
    close: async () => {
      await service.close();
      rmSync(dir, { recursive: true, force: true });
    },
  };
}

if (process.argv[1] && process.argv[1].endsWith("minimize-fixture.ts")) {
  const args = process.argv.slice(2);
  const option = (name: string) => {
    const index = args.indexOf(name);
    return index >= 0 ? args[index + 1] : undefined;
  };
  const path = args.find(
    (a) =>
      !a.startsWith("--") &&
      !["compile", "evaluate"].includes(a) &&
      a !== option("--expect") &&
      a !== option("--out"),
  );
  const expected = option("--expect");
  if (!path || !expected) {
    console.error(
      "Usage: tsx scripts/minimize-fixture.ts <fixture.json> --expect <ERROR_CODE> [--oracle compile|evaluate] [--out file]",
    );
    process.exit(2);
  }
  const ir = JSON.parse(readFileSync(path, "utf8"));
  const mode = option("--oracle") ?? "compile";
  const runner =
    mode === "evaluate"
      ? await evaluateOracle()
      : { oracle: compileOracle, close: async () => {} };
  try {
    const result = await minimize(ir, expected, runner.oracle, (line) =>
      console.error(line),
    );
    const out =
      option("--out") ?? path.replace(/\.json$/, "") + ".minimized.json";
    writeFileSync(out, JSON.stringify(result.ir, null, 2) + "\n");
    console.log(
      JSON.stringify({
        out,
        steps: result.steps,
        features: result.features,
        constraints: result.constraints,
      }),
    );
  } finally {
    await runner.close();
  }
}
