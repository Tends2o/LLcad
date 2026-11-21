/** Lanes helper for the llcad-modelling skill (parallel lanes are opt-in).
 *
 *   node <LLCAD_ROOT>/dist/packages/feature-work/lanes-cli.js merge --out DIR
 *        [--run .llcad/run.json] [--base-ir IR.json] LANE.json...
 *
 * Merges the patch files the lanes wrote against the frozen skeleton head into waves
 * of at most 64 operations (one transaction each) and writes DIR/wave-N.json for
 * cad_apply_patch {patch_ref: {path}, validate: true, commit: "if_valid"}. Each input
 * file is one lane ({operations} or a bare array; optional "lane" and "prefix").
 * Checks: run.json lanes must be set (opt-in); every new feature id of a lane carries
 * its prefix; no feature, part or frame is touched by two lanes. With --base-ir (the
 * head IR) output edits of several lanes on one part/the model outputs are combined;
 * without it they must belong to one lane. Prints a JSON summary, exit 2 on conflicts. */
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { basename, join, resolve } from "node:path";
import { packWaves, WAVE_LIMIT } from "./merge.js";

type Lane = { lane: string; prefix: string | null; operations: any[] };

export function mergeLanes(files: { path: string; doc: any }[], opts: { lanes?: Record<string, string>; baseIR?: any; max?: number }) {
  const conflicts: string[] = [];
  const prefixes = Object.values(opts.lanes ?? {});
  const lanes: Lane[] = files.map(({ path, doc }) => {
    const operations = Array.isArray(doc) ? doc : doc?.operations;
    if (!Array.isArray(operations)) throw new Error(`${path}: no operations array`);
    return { lane: String(doc?.lane ?? basename(path).replace(/\.json$/i, "")), prefix: doc?.prefix ?? null, operations };
  });
  const touched = new Map<string, string>();
  const claim = (kind: string, key: string, lane: string) => {
    const owner = touched.get(kind + ":" + key);
    if (owner && owner !== lane) conflicts.push(`${kind} ${key}: lanes ${owner} and ${lane}`);
    touched.set(kind + ":" + key, lane);
  };
  for (const lane of lanes) {
    const created = lane.operations.filter((op) => op?.op === "add_feature").map((op) => String(op.feature?.id ?? ""));
    if (!lane.prefix && created.length) lane.prefix = prefixes.find((p) => created[0].startsWith(p)) ?? created[0].replace(/-[^-]*$/, "-");
    for (const fid of created) {
      if (lane.prefix && !fid.startsWith(lane.prefix)) conflicts.push(`${lane.lane}: ${fid} outside the lane prefix ${lane.prefix}`);
      if (prefixes.length && !prefixes.some((p) => fid.startsWith(p))) conflicts.push(`${lane.lane}: ${fid} matches no run.json lanes prefix`);
      claim("feature", fid, lane.lane);
    }
    for (const op of lane.operations) {
      if (op?.feature_id) claim("feature", op.feature_id, lane.lane);
      for (const entity of [op?.part, op?.frame, op?.assembly]) if (entity?.id) claim("structure", entity.id, lane.lane);
      if (!opts.baseIR && op?.op === "set_part_outputs") claim("part outputs", op.part_id, lane.lane);
      if (!opts.baseIR && op?.op === "set_outputs") claim("model", "outputs", lane.lane);
    }
  }
  const base = opts.baseIR ?? { features: [], outputs: [], structure: { project: { id: "project", semantic_name: "project" }, frames: [], assemblies: [], parts: [] } };
  const waves = conflicts.length ? [] : packWaves(lanes.map((l) => ({ task_id: l.lane, operations: l.operations })), base, opts.max ?? WAVE_LIMIT, !opts.baseIR);
  return { conflicts, waves, lanes: lanes.map((l) => ({ lane: l.lane, prefix: l.prefix, operations: l.operations.length })) };
}

function main(argv: string[]) {
  const [command, ...rest] = argv;
  const flags: Record<string, string> = {}, inputs: string[] = [];
  for (let i = 0; i < rest.length; i++) {
    if (rest[i].startsWith("--")) flags[rest[i].slice(2)] = rest[++i];
    else inputs.push(rest[i]);
  }
  if (command !== "merge" || !flags.out || !inputs.length) {
    process.stderr.write("usage: lanes-cli.js merge --out DIR [--run .llcad/run.json] [--base-ir IR.json] [--max 64] LANE.json...\n");
    return 64;
  }
  let lanes: Record<string, string> | undefined;
  if (flags.run) {
    const run = JSON.parse(readFileSync(flags.run, "utf8"));
    lanes = run.lanes ?? {};
    if (!Object.keys(lanes!).length) {
      process.stdout.write(JSON.stringify({ status: "refused", reason: "lanes are opt-in: run.json lanes is empty" }) + "\n");
      return 2;
    }
  }
  const baseDoc = flags["base-ir"] ? JSON.parse(readFileSync(flags["base-ir"], "utf8")) : undefined;
  const baseIR = baseDoc?.ir ?? baseDoc;
  const files = inputs.map((path) => ({ path, doc: JSON.parse(readFileSync(path, "utf8")) }));
  const merged = mergeLanes(files, { lanes, baseIR, max: flags.max ? Number(flags.max) : undefined });
  if (merged.conflicts.length) {
    process.stdout.write(JSON.stringify({ status: "conflict", conflicts: merged.conflicts.slice(0, 32), lanes: merged.lanes }) + "\n");
    return 2;
  }
  const out = resolve(flags.out);
  mkdirSync(out, { recursive: true });
  const waves = merged.waves.map((w) => {
    const path = join(out, `wave-${w.wave}.json`);
    writeFileSync(path, JSON.stringify({ operations: w.operations, lanes: w.task_ids, wave: w.wave, of_waves: merged.waves.length }, null, 1));
    return { wave: w.wave, lanes: w.task_ids, operations: w.operations.length, path,
      apply: `cad_apply_patch {model_id, base_revision: <head>, idempotency_key: <new>, patch_ref: {path: "${path}"}, validate: true, commit: "if_valid", wait_ms: 100000}` };
  });
  process.stdout.write(JSON.stringify({ status: "ok", lanes: merged.lanes, waves, next: "apply the waves in order (one commit each), then look at the changed views and run the gates" }) + "\n");
  return 0;
}

if (process.argv[1] && resolve(process.argv[1]).replace(/\.ts$/, ".js").endsWith(join("feature-work", "lanes-cli.js")))
  process.exitCode = main(process.argv.slice(2));
