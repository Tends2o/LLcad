import { hash } from "../semantic-ir/hash.js";
import { structureOf } from "../compiler/structure.js";

/** Merge by reference: the proposals of one batch become patch documents ("waves") of at
 *  most 64 operations each, one transaction per wave, applied in order with
 *  cad_apply_patch {patch_ref: {artifact_id}}. Output edits of several tasks on the same
 *  existing part (lanes) are combined into one set_part_outputs per wave whose
 *  expected_hash is the part's outputs after the earlier waves. */
export const WAVE_LIMIT = 64;

export interface TaskInfo {
  /** Existing features the task read or changed: owned, context and their ancestors. */
  closure: string[];
  /** Features the task adds. */
  created: string[];
  /** Existing parts whose outputs it changes. */
  parts: string[];
}
export interface WaveEntry {
  task_id: string;
  operations: any[];
}
export interface Wave {
  wave: number;
  task_ids: string[];
  operations: any[];
  /** Existing parts: outputs hash expected before the wave and outputs after it. */
  parts: Record<string, { expected_hash: string; outputs: string[] }>;
  model_outputs?: { before: string; outputs: string[] };
}

/** Owned + context features and every ancestor in the frozen base. */
export function closureOf(ir: any, ids: string[]) {
  const byId = new Map<string, any>(ir.features.map((f: any) => [f.id, f]));
  const seen = new Set<string>();
  const visit = (fid: string) => {
    if (seen.has(fid)) return;
    seen.add(fid);
    for (const dep of byId.get(fid)?.depends_on ?? []) visit(dep);
  };
  ids.forEach(visit);
  return seen;
}

function applyDelta(before: string[], removed: Set<string>, added: string[]) {
  const after = before.filter((x) => !removed.has(x));
  for (const x of added) if (!after.includes(x)) after.push(x);
  return after;
}
function delta(before: string[], after: string[]) {
  return {
    removed: new Set(before.filter((x) => !after.includes(x))),
    added: after.filter((x) => !before.includes(x)),
  };
}

/** Task-level view of a proposal: plain operations, and the output deltas on existing
 *  parts and on the model outputs (task-created parts stay inside the plain list). */
function split(entry: WaveEntry, baseParts: Map<string, string[]>, baseOutputs: string[], keepOutputs = false) {
  const plain: any[] = [];
  const createdParts = new Set<string>();
  const parts = new Map<string, { removed: Set<string>; added: string[] }[]>();
  const working = new Map<string, string[]>();
  let modelBefore = baseOutputs;
  const model: { removed: Set<string>; added: string[] }[] = [];
  for (const op of entry.operations) {
    if (op.op === "add_part") createdParts.add(op.part.id);
    if (op.op === "set_part_outputs" && !createdParts.has(op.part_id) && baseParts.has(op.part_id)) {
      const before = working.get(op.part_id) ?? baseParts.get(op.part_id)!;
      if (!parts.has(op.part_id)) parts.set(op.part_id, []);
      parts.get(op.part_id)!.push(delta(before, op.outputs));
      working.set(op.part_id, [...op.outputs]);
    } else if (op.op === "set_outputs" && !keepOutputs) {
      model.push(delta(modelBefore, op.outputs));
      modelBefore = [...op.outputs];
    } else plain.push(op);
  }
  return { plain, parts, model };
}

/** keepOutputs: leave set_outputs as written (no base IR to compute deltas against). */
export function packWaves(entries: WaveEntry[], baseIR: any, max = WAVE_LIMIT, keepOutputs = false): Wave[] {
  const baseParts = new Map<string, string[]>(
    structureOf(baseIR).parts.map((p: any) => [p.id, [...(p.outputs ?? [])]]),
  );
  const baseOutputs: string[] = [...(baseIR.outputs ?? [])];
  const groups: { entries: ReturnType<typeof split>[]; task_ids: string[]; count: number; parts: Set<string>; model: boolean }[] = [];
  let open: (typeof groups)[number] | null = null;
  for (const entry of entries) {
    const s = split(entry, baseParts, baseOutputs, keepOutputs);
    const cost = (g: typeof open) =>
      (g?.count ?? 0) +
      s.plain.length +
      [...s.parts.keys()].filter((part) => !g?.parts.has(part)).length +
      (s.model.length && !g?.model ? 1 : 0);
    if (open && cost(open) > max) open = null;
    if (!open) {
      open = { entries: [], task_ids: [], count: 0, parts: new Set(), model: false };
      groups.push(open);
    }
    open.count = cost(open);
    open.entries.push(s);
    open.task_ids.push(entry.task_id);
    s.parts.forEach((_, part) => open!.parts.add(part));
    if (s.model.length) open.model = true;
  }
  // Outputs after the earlier waves: each wave expects exactly that state.
  const current = new Map(baseParts);
  let currentOutputs = baseOutputs;
  return groups.map((g, index) => {
    const operations = g.entries.flatMap((s) => s.plain);
    const parts: Wave["parts"] = {};
    for (const part of g.parts) {
      const before = current.get(part)!;
      let after = before;
      for (const s of g.entries)
        for (const d of s.parts.get(part) ?? []) after = applyDelta(after, d.removed, d.added);
      parts[part] = { expected_hash: hash(before), outputs: after };
      operations.push({ op: "set_part_outputs", part_id: part, expected_hash: hash(before), outputs: after });
      current.set(part, after);
    }
    let model_outputs: Wave["model_outputs"];
    if (g.model) {
      let after = currentOutputs;
      for (const s of g.entries) for (const d of s.model) after = applyDelta(after, d.removed, d.added);
      model_outputs = { before: hash(currentOutputs), outputs: after };
      operations.push({ op: "set_outputs", outputs: after });
      currentOutputs = after;
    }
    return { wave: index + 1, task_ids: g.task_ids, operations, parts, ...(model_outputs ? { model_outputs } : {}) };
  });
}

/** Features added, removed or changed between two IRs. */
export function changedFeatures(before: any, after: any) {
  const old = new Map<string, string>(before.features.map((f: any) => [f.id, hash(f)]));
  const changed = new Set<string>();
  for (const f of after.features) {
    if (old.get(f.id) !== hash(f)) changed.add(f.id);
    old.delete(f.id);
  }
  for (const fid of old.keys()) changed.add(fid);
  return changed;
}

/** Why the given tasks (one wave or one task artifact) no longer apply to `headIR`:
 *  a feature they read or change was edited since the frozen base, their new features
 *  exist already (applied twice), or a part's outputs differ from what the wave expects. */
export function waveConflicts(batch: any, taskIds: string[], baseIR: any, headIR: any, wave: number | null) {
  const conflicts: string[] = [];
  const info: Record<string, TaskInfo> = batch.merge?.tasks ?? {};
  const changed = changedFeatures(baseIR, headIR);
  const present = new Set<string>(headIR.features.map((f: any) => f.id));
  const headParts = new Map<string, string[]>(
    structureOf(headIR).parts.map((p: any) => [p.id, [...(p.outputs ?? [])]]),
  );
  const baseParts = new Map<string, string[]>(
    structureOf(baseIR).parts.map((p: any) => [p.id, [...(p.outputs ?? [])]]),
  );
  for (const task of taskIds) {
    const t = info[task];
    if (!t) {
      conflicts.push(`${task}: unknown task of batch ${batch.id}`);
      continue;
    }
    const hit = t.closure.filter((fid) => changed.has(fid));
    if (hit.length) conflicts.push(`${task}: changed since the frozen base: ${hit.slice(0, 8).join(", ")}`);
    const twice = t.created.filter((fid) => present.has(fid));
    if (twice.length) conflicts.push(`${task}: already present (applied before?): ${twice.slice(0, 8).join(", ")}`);
    if (wave === null)
      for (const part of t.parts)
        if (hash(headParts.get(part) ?? null) !== hash(baseParts.get(part) ?? null))
          conflicts.push(`${task}: outputs of part ${part} changed since the frozen base`);
  }
  if (wave !== null) {
    const w = (batch.merge?.waves ?? []).find((x: any) => x.wave === wave);
    for (const [part, expected] of Object.entries<any>(w?.parts ?? {}))
      if (hash(headParts.get(part) ?? null) !== expected.expected_hash)
        conflicts.push(`wave ${wave}: outputs of part ${part} differ from the state this wave expects (apply the waves in order)`);
    if (w?.model_outputs && hash(headIR.outputs ?? []) !== w.model_outputs.before)
      conflicts.push(`wave ${wave}: model outputs differ from the state this wave expects`);
  }
  return conflicts;
}
