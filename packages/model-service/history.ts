import { REGISTRY_HASH } from "../compiler/index.js";
import { Principal } from "../policy/index.js";
import { requireThat } from "../semantic-ir/errors.js";
import { hash } from "../semantic-ir/hash.js";
import type { Store } from "./store.js";
/** The states a model has been in, and the way back to any of them.
 *
 *  Nothing is copied to make this work. Every state is the revision that was
 *  committed at the time: its construction as text, its measurements, and
 *  shapes that live once in the content-addressed store and are shared by every
 *  revision that still builds them the same way. Going back is a pointer, the
 *  way a branch is a pointer in git — no second model, no second geometry. */
const WINDOW = 25,
  DETAILED = 12,
  MEMORY = 400;
export type RevisionState = "instant" | "ready" | "rebuild_required";
type Change = {
  added: string[];
  removed: string[];
  changed: string[];
  structure: boolean;
};
/** Differences between two immutable revisions never change; remembering them
 *  keeps the list instant after the first look. */
const changes = new Map<string, Change>();
export interface HistoryProbe {
  /** True when this revision's geometry still belongs to the current build. */
  compatible(revision: any): boolean;
  /** True when its preview is already rendered and can be served at once. */
  warm(revision: string): boolean;
}
export function registryOf(store: Store, revision: string): string | null {
  return (
    store.get(
      "SELECT json_extract(plan,'$.registry_hash') AS registry FROM transactions WHERE committed_revision=? AND state='committed'",
      revision,
    )?.registry ?? null
  );
}
/** Cheap for the common case: the transaction that committed a revision recorded
 *  the build it was made with. Only rows without that record are compiled. */
function state(
  store: Store,
  p: Principal,
  model: string,
  row: any,
  probe: HistoryProbe,
): RevisionState {
  const registry = registryOf(store, row.id);
  const usable =
    registry !== null
      ? registry === REGISTRY_HASH
      : probe.compatible(store.revision(p, model, row.id));
  if (!usable) return "rebuild_required";
  return probe.warm(row.id) ? "instant" : "ready";
}
function named(ir: any, ids: string[]) {
  return ids.map((fid) => {
    const f = ir.features.find((x: any) => x.id === fid);
    return f?.semantic_name ?? fid;
  });
}
/** What one step of the history did, in the words of the construction itself.
 *  Revisions are immutable, so a difference is computed once; the two hashes
 *  identify it without reading either construction again. */
function difference(before: any, after: any): Change {
  // Text first, the canonical hash only where the text differs: identical
  // features are the rule, and comparing them must not cost anything.
  const old = new Map<string, any>(
    before.ir.features.map((f: any) => [f.id, f] as [string, any]),
  );
  const same = (a: any, b: any) =>
    JSON.stringify(a) === JSON.stringify(b) || hash(a) === hash(b);
  const change: Change = {
    added: [],
    removed: [],
    changed: [],
    structure:
      hash(before.ir.structure ?? null) !== hash(after.ir.structure ?? null),
  };
  for (const f of after.ir.features) {
    const previous = old.get(f.id);
    if (previous === undefined) change.added.push(f.id);
    else if (!same(previous, f)) change.changed.push(f.id);
    old.delete(f.id);
  }
  change.removed = [...old.keys()];
  const summary: Change = {
    added: named(after.ir, change.added).slice(0, 4),
    removed: named(before.ir, change.removed).slice(0, 4),
    changed: named(after.ir, change.changed).slice(0, 4),
    structure: change.structure,
  };
  // Counts stay exact even when only a few names are listed.
  (summary as any).counts = {
    added: change.added.length,
    removed: change.removed.length,
    changed: change.changed.length,
  };
  return summary;
}
/** The difference between a revision and its parent, read from memory when it
 *  has been asked for before — neither construction is parsed in that case. */
function step(store: Store, model: string, row: any, memo: Map<string, any>) {
  const parent = row.parent ? digest(store, model, row.parent, memo) : null;
  if (!parent) return null;
  const key = hash({ v: 1, before: parent.ir_hash, after: row.ir_hash }),
    known = changes.get(key);
  if (known) return known;
  const before = parsed(store, model, row.parent, memo),
    after = parsed(store, model, row.id, memo);
  if (!before || !after) return null;
  const summary = difference(before, after);
  if (changes.size >= MEMORY) changes.clear();
  changes.set(key, summary);
  return summary;
}
function digest(
  store: Store,
  model: string,
  revision: string,
  memo: Map<string, any>,
) {
  const key = "h:" + revision;
  if (!memo.has(key))
    memo.set(
      key,
      store.get(
        "SELECT ir_hash FROM revisions WHERE id=? AND model=?",
        revision,
        model,
      ),
    );
  return memo.get(key);
}
function parsed(
  store: Store,
  model: string,
  revision: string,
  memo: Map<string, any>,
) {
  const key = "p:" + revision;
  if (!memo.has(key)) {
    const row = store.get(
      "SELECT ir_hash,ir FROM revisions WHERE id=? AND model=?",
      revision,
      model,
    );
    memo.set(
      key,
      row ? { ir_hash: row.ir_hash, ir: JSON.parse(row.ir) } : null,
    );
  }
  return memo.get(key);
}
export function history(
  store: Store,
  p: Principal,
  model: string,
  probe: HistoryProbe,
  limit = WINDOW,
) {
  const m = store.model(p, model);
  const rows = store.all(
    `SELECT id,parent,created,quality,ir_hash,
            json_array_length(ir,'$.features') AS features,
            json_extract(geometry,'$.aggregate.volume') AS volume
       FROM revisions WHERE model=? AND id LIKE 'rev-%'
      ORDER BY created DESC LIMIT ?`,
    model,
    Math.max(1, Math.min(Number(limit) || WINDOW, WINDOW)),
  );
  const memo = new Map<string, any>();
  const revisions = rows.map((row: any, index: number) => {
    return {
      revision: row.id,
      parent: row.parent,
      created: row.created,
      quality: row.quality,
      features: row.features,
      volume_mm3: row.volume,
      is_head: row.id === m.head,
      state: state(store, p, model, row, probe),
      change: index < DETAILED ? step(store, model, row, memo) : null,
    };
  });
  return {
    status: "succeeded",
    model_id: m.id,
    head: m.head,
    registry_hash: REGISTRY_HASH,
    revisions,
  };
}
/** Make an earlier state the current one. The construction and its geometry
 *  already exist, so this writes one pointer and nothing else — no rebuild, no
 *  copy, and the state left behind stays exactly where it is. */
export function setHead(
  store: Store,
  p: Principal,
  model: string,
  revision: string,
  probe: HistoryProbe,
) {
  const m = store.model(p, model, "model:edit"),
    target = store.revision(p, model, revision);
  if (m.head === target.id)
    return {
      status: "unchanged",
      model_id: m.id,
      revision: target.id,
      previous_revision: target.id,
    };
  requireThat(
    target.quality === "checks_passed_within_profile",
    "VALIDATION_REQUIRED",
    "Nur geprüfte, übernommene Stände können der aktuelle Stand werden.",
  );
  const registry = registryOf(store, target.id);
  requireThat(
    registry === null ? probe.compatible(target) : registry === REGISTRY_HASH,
    "BUILD_MISMATCH",
    "Dieser Stand gehört zu einem früheren Geometrie-Build. Mit cad_rebuild neu berechnen, prüfen und übernehmen.",
    { target_registry_hash: REGISTRY_HASH, recommended_tool: "cad_rebuild" },
  );
  // A promise made in the current state is not dropped by going back to a state
  // that predates it: a protected parameter must still hold afterwards.
  const base = store.revision(p, model, m.head);
  for (const c of base.ir.constraints ?? [])
    if (c.kind === "protected_parameter") {
      const b = base.ir.features.find((f: any) => f.id === c.feature_id),
        t = target.ir.features.find((f: any) => f.id === c.feature_id);
      requireThat(
        b &&
          t &&
          hash(b.parameters[c.parameter]) === hash(t.parameters[c.parameter]),
        "OUT_OF_SCOPE",
        "Dieser Stand verletzt einen geschützten Parameter des aktuellen Standes.",
      );
    }
  const open = store.all(
    "SELECT id FROM transactions WHERE model=? AND state NOT IN ('committed','discarded','failed')",
    model,
  );
  store.atomic(() => {
    store.run("UPDATE models SET head=? WHERE id=?", target.id, m.id);
    store.audit("model_head_switched", {
      model_id: m.id,
      from_revision: m.head,
      to_revision: target.id,
      registry_hash: REGISTRY_HASH,
      superseded_candidates: open.map((t: any) => t.id),
    });
  });
  return {
    status: "switched",
    model_id: m.id,
    revision: target.id,
    previous_revision: m.head,
    superseded_candidates: open.length,
  };
}
