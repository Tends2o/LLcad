import { backup, DatabaseSync } from "node:sqlite";
import {
  mkdirSync,
  readdirSync,
  copyFileSync,
  existsSync,
  writeFileSync,
  readFileSync,
  statSync,
  unlinkSync,
} from "node:fs";
import { join, resolve } from "node:path";
import { Store } from "./store.js";
import { bytesHash, hash } from "../semantic-ir/hash.js";
import { requireThat } from "../semantic-ir/errors.js";
import {
  syncFile,
  syncDirectory,
  syncCreatedDirectories,
} from "./durable-files.js";
export async function backupStore(store: Store, destination: string) {
  const target = resolve(destination);
  requireThat(!existsSync(target), "OUT_OF_SCOPE", "Backupziel muss neu sein.");
  const firstCreated = mkdirSync(join(target, "blobs"), {
    recursive: true,
    mode: 0o700,
  });
  await backup(store.db, join(target, "models.sqlite"));
  syncFile(join(target, "models.sqlite"));
  const blobs = [];
  for (const name of readdirSync(join(store.root, "blobs"))) {
    requireThat(
      /^[a-f0-9]{64}$/.test(name),
      "INTEGRITY_FAILURE",
      "Unerwartete Blobdatei.",
    );
    const bytes = store.readBlob(name);
    copyFileSync(join(store.root, "blobs", name), join(target, "blobs", name));
    syncFile(join(target, "blobs", name));
    blobs.push({ hash: name, bytes: bytes.length });
  }
  const manifest = {
    version: 1,
    created: new Date().toISOString(),
    database_hash: bytesHash(readFileSync(join(target, "models.sqlite"))),
    blobs,
    credentials_included: false,
  };
  writeFileSync(
    join(target, "backup-manifest.json"),
    JSON.stringify(manifest, null, 2),
    { mode: 0o600 },
  );
  syncFile(join(target, "backup-manifest.json"));
  syncDirectory(join(target, "blobs"));
  syncCreatedDirectories(target, firstCreated);
  return manifest;
}
export function restoreStore(source: string, destination: string) {
  const target = resolve(destination);
  requireThat(
    !existsSync(target),
    "OUT_OF_SCOPE",
    "Wiederherstellungsziel muss neu sein.",
  );
  const manifest = JSON.parse(
    readFileSync(join(source, "backup-manifest.json"), "utf8"),
  );
  requireThat(
    manifest.version === 1 &&
      bytesHash(readFileSync(join(source, "models.sqlite"))) ===
        manifest.database_hash,
    "INTEGRITY_FAILURE",
    "Backup-Datenbank-Prüfsumme stimmt nicht.",
  );
  for (const b of manifest.blobs) {
    requireThat(
      /^[a-f0-9]{64}$/.test(b.hash),
      "INTEGRITY_FAILURE",
      "Ungültiger Backup-Dateiname.",
    );
    const bytes = readFileSync(join(source, "blobs", b.hash));
    requireThat(
      bytesHash(bytes) === b.hash && bytes.length === b.bytes,
      "INTEGRITY_FAILURE",
      "Backup-Blob-Prüfsumme stimmt nicht.",
    );
  }
  const check = new DatabaseSync(join(source, "models.sqlite"), {
    readOnly: true,
  });
  try {
    requireThat(
      (check.prepare("PRAGMA integrity_check").get() as any).integrity_check ===
        "ok",
      "INTEGRITY_FAILURE",
      "SQLite-Integritätsprüfung fehlgeschlagen.",
    );
  } finally {
    check.close();
  }
  const firstCreated = mkdirSync(join(target, "blobs"), {
    recursive: true,
    mode: 0o700,
  });
  copyFileSync(join(source, "models.sqlite"), join(target, "models.sqlite"));
  syncFile(join(target, "models.sqlite"));
  for (const b of manifest.blobs) {
    copyFileSync(join(source, "blobs", b.hash), join(target, "blobs", b.hash));
    syncFile(join(target, "blobs", b.hash));
  }
  syncDirectory(join(target, "blobs"));
  syncCreatedDirectories(target, firstCreated);
  return { status: "restored", blobs: manifest.blobs.length };
}
/** Retention policy (Bauplan 15.2, 18.5): derived previews and unreferenced cache generations expire
 * after explicit windows; committed revisions, their proofs and export packages are never collected. */
export const RETENTION_POLICY = {
  version: 1,
  orphan_blob_minimum_age_ms: 7 * 24 * 60 * 60 * 1000,
  preview_artifact_ttl_ms: 7 * 24 * 60 * 60 * 1000,
  orphan_cache_ttl_ms: 30 * 24 * 60 * 60 * 1000,
  expired_publication_grace_ms: 24 * 60 * 60 * 1000,
  never_collected: [
    "revisions",
    "commit_proofs",
    "export_packages",
    "imported_originals",
    "audit",
  ],
  external_backups: "operator_retention_and_erasure_required_separately",
};
/** Remove expired preview artifacts, stale publications and cache generations no revision references. */
export function applyRetention(store: Store, now = Date.now()) {
  const previews = store.all(
    "SELECT id,created,manifest FROM artifacts WHERE json_extract(manifest,'$.quality')='preview_only' AND json_extract(manifest,'$.filename') IN ('preview.json','preview.bin')",
  );
  let removedPreviews = 0;
  for (const row of previews)
    if (
      now - Date.parse(row.created) >=
      RETENTION_POLICY.preview_artifact_ttl_ms
    ) {
      store.run("DELETE FROM artifacts WHERE id=?", row.id);
      removedPreviews++;
    }
  // A cached preview lives exactly as long as the artifacts it points to.
  for (const row of store.all("SELECT tenant,key,artifacts FROM render_cache"))
    if (
      !(JSON.parse(row.artifacts) as string[]).every((aid) =>
        store.get("SELECT 1 FROM artifacts WHERE id=?", aid),
      )
    )
      store.run(
        "DELETE FROM render_cache WHERE tenant=? AND key=?",
        row.tenant,
        row.key,
      );
  const referencedKeys = new Set<string>();
  for (const row of store.all(
    "SELECT geometry FROM revisions WHERE geometry IS NOT NULL",
  )) {
    const facts = JSON.parse(row.geometry).facts ?? {};
    for (const fact of Object.values<any>(facts)) {
      if (fact?.cache_key) {
        referencedKeys.add(fact.cache_key);
        referencedKeys.add(fact.cache_key + ":topology");
      }
      if (fact?.local_cache_key) {
        referencedKeys.add(fact.local_cache_key);
        referencedKeys.add(fact.local_cache_key + ":topology");
      }
    }
  }
  let removedCache = 0;
  for (const row of store.all("SELECT tenant,key,created FROM cache"))
    if (
      // Tessellations (key:mesh:deflection) follow the feature they belong to.
      !referencedKeys.has(row.key.split(":mesh:")[0]) &&
      !row.key.startsWith("field:") &&
      row.created !== null &&
      now - row.created >= RETENTION_POLICY.orphan_cache_ttl_ms
    ) {
      store.run(
        "DELETE FROM cache WHERE tenant=? AND key=?",
        row.tenant,
        row.key,
      );
      removedCache++;
    }
  const expiredPublications = store.run(
    "DELETE FROM publications WHERE expires < ?",
    now - RETENTION_POLICY.expired_publication_grace_ms,
  ).changes;
  store.audit("retention_applied", {
    removed_preview_artifacts: removedPreviews,
    removed_cache_generations: removedCache,
    removed_expired_publications: expiredPublications,
    policy_version: RETENTION_POLICY.version,
  });
  return {
    removed_preview_artifacts: removedPreviews,
    removed_cache_generations: removedCache,
    removed_expired_publications: expiredPublications,
  };
}
/** Offline retention GC: only unreferenced blobs older than the explicit retention window. */
export function garbageCollect(
  store: Store,
  minimumAgeMs = RETENTION_POLICY.orphan_blob_minimum_age_ms,
) {
  requireThat(
    minimumAgeMs >= 0,
    "INVALID_SCHEMA",
    "Ungültige Aufbewahrungsfrist.",
  );
  const referenced = new Set<string>();
  for (const row of store.all("SELECT hash FROM artifacts"))
    referenced.add(row.hash);
  for (const row of store.all("SELECT blob FROM cache"))
    referenced.add(row.blob);
  for (const row of store.all(
    "SELECT geometry FROM revisions WHERE geometry IS NOT NULL",
  ))
    for (const b of Object.values<string>(JSON.parse(row.geometry).blobs ?? {}))
      referenced.add(b);
  const deleted = [];
  for (const name of readdirSync(join(store.root, "blobs")))
    if (
      /^[a-f0-9]{64}$/.test(name) &&
      !referenced.has(name) &&
      Date.now() - statSync(join(store.root, "blobs", name)).mtimeMs >=
        minimumAgeMs
    ) {
      unlinkSync(join(store.root, "blobs", name));
      deleted.push(name);
    }
  store.audit("retention_gc", {
    deleted_count: deleted.length,
    minimum_age_ms: minimumAgeMs,
  });
  return { deleted_count: deleted.length };
}

/** Operator-only, offline removal of whole model projects. Everything that
 *  belongs to a named model goes with it; everything another model still holds
 *  stays, because shapes and files are addressed by content and shared between
 *  models that build them the same way. Without `apply` it only counts. */
export function eraseModels(store: Store, ids: string[], apply = false) {
  requireThat(
    Array.isArray(ids) && ids.length > 0 && ids.length <= 256,
    "INVALID_SCHEMA",
    "Ein bis 256 Modelle.",
  );
  const models = ids.map((id) => {
    const row = store.get("SELECT id,name FROM models WHERE id=?", id);
    requireThat(row, "ACCESS_DENIED", "Modell nicht vorhanden: " + id);
    return row;
  });
  const list = models.map((m) => m.id),
    marks = list.map(() => "?").join(",");
  requireThat(
    store.get(
      `SELECT COUNT(*) AS n FROM jobs WHERE state IN ('queued','running') AND model IN (${marks})`,
      ...list,
    ).n === 0,
    "CONSTRAINT_CONFLICT",
    "Zu diesen Modellen laufen noch Jobs.",
  );
  const owned = (table: string) =>
    store
      .all(`SELECT id FROM ${table} WHERE model IN (${marks})`, ...list)
      .map((r: any) => r.id);
  const selections = owned("selections"),
    jobs = owned("jobs"),
    transactions = owned("transactions"),
    publications = owned("publications"),
    grants = owned("model_grants"),
    approvals = owned("approval_requests"),
    revisions = store.get(
      `SELECT COUNT(*) AS n FROM revisions WHERE model IN (${marks})`,
      ...list,
    ).n,
    artifacts = store.get(
      `SELECT COUNT(*) AS n FROM artifacts WHERE model IN (${marks})`,
      ...list,
    ).n;
  const counts: Record<string, number> = {
    models: list.length,
    revisions,
    artifacts,
    transactions: transactions.length,
    jobs: jobs.length,
    selections: selections.length,
    publications: publications.length,
  };
  if (!apply) return { status: "dry_run", models, counts };
  const removed: Record<string, number> = {};
  const del = (name: string, sql: string, ...params: any[]) => {
    removed[name] =
      (removed[name] ?? 0) + Number(store.run(sql, ...params).changes);
  };
  // Long lists are deleted in bounded statements; SQLite takes only so many
  // parameters at once.
  const byId = (name: string, sql: string, values: string[]) => {
    for (let i = 0; i < values.length; i += 400) {
      const chunk = values.slice(i, i + 400);
      del(name, sql.replace("?IN?", chunk.map(() => "?").join(",")), ...chunk);
    }
  };
  store.atomic(() => {
    byId(
      "selection_faces",
      "DELETE FROM selection_faces WHERE selection_id IN (?IN?)",
      selections,
    );
    byId("selections", "DELETE FROM selections WHERE id IN (?IN?)", selections);
    byId(
      "job_authorizations",
      "DELETE FROM job_authorizations WHERE job IN (?IN?)",
      jobs,
    );
    byId("jobs", "DELETE FROM jobs WHERE id IN (?IN?)", jobs);
    byId(
      "transaction_authorizations",
      "DELETE FROM transaction_authorizations WHERE tx IN (?IN?)",
      transactions,
    );
    byId(
      "transactions",
      "DELETE FROM transactions WHERE id IN (?IN?)",
      transactions,
    );
    byId(
      "grant_actions",
      "DELETE FROM grant_actions WHERE grant_id IN (?IN?)",
      grants,
    );
    byId("model_grants", "DELETE FROM model_grants WHERE id IN (?IN?)", grants);
    byId(
      "approval_consumptions",
      "DELETE FROM approval_consumptions WHERE request IN (?IN?)",
      approvals,
    );
    byId(
      "approval_requests",
      "DELETE FROM approval_requests WHERE id IN (?IN?)",
      approvals,
    );
    byId(
      "publication_artifacts",
      "DELETE FROM publication_artifacts WHERE publication IN (?IN?)",
      publications,
    );
    byId(
      "publications",
      "DELETE FROM publications WHERE id IN (?IN?)",
      publications,
    );
    del(
      "model_acl",
      `DELETE FROM model_acl WHERE model IN (${marks})`,
      ...list,
    );
    del(
      "annotations",
      `DELETE FROM annotations WHERE model IN (${marks})`,
      ...list,
    );
    // A file stays as long as any surviving model still declares it.
    del(
      "artifact_models",
      `DELETE FROM artifact_models WHERE model IN (${marks})`,
      ...list,
    );
    del(
      "artifacts",
      `DELETE FROM artifacts WHERE model IN (${marks})
         AND NOT EXISTS (SELECT 1 FROM artifact_models am WHERE am.artifact=artifacts.id)`,
      ...list,
    );
    del(
      "revisions",
      `DELETE FROM revisions WHERE model IN (${marks})`,
      ...list,
    );
    del("models", `DELETE FROM models WHERE id IN (${marks})`, ...list);
    for (const id of list)
      del(
        "idempotency",
        "DELETE FROM idempotency WHERE result LIKE ?",
        "%" + id + "%",
      );
    // Shapes, tessellations and measurements that no revision references any
    // more. Field caches carry their own key space and are left alone.
    const referenced = new Set<string>();
    for (const row of store.all(
      "SELECT geometry FROM revisions WHERE geometry IS NOT NULL",
    ))
      for (const fact of Object.values<any>(
        JSON.parse(row.geometry).facts ?? {},
      )) {
        if (fact?.cache_key) referenced.add(fact.cache_key);
        if (fact?.local_cache_key) referenced.add(fact.local_cache_key);
      }
    for (const row of store.all("SELECT tenant,key FROM cache"))
      if (
        !row.key.startsWith("field:") &&
        !referenced.has(row.key.split(":")[0])
      )
        del(
          "cache",
          "DELETE FROM cache WHERE tenant=? AND key=?",
          row.tenant,
          row.key,
        );
    for (const row of store.all(
      "SELECT tenant,key,artifacts FROM render_cache",
    ))
      if (
        !(JSON.parse(row.artifacts) as string[]).every((aid) =>
          store.get("SELECT 1 FROM artifacts WHERE id=?", aid),
        )
      )
        del(
          "render_cache",
          "DELETE FROM render_cache WHERE tenant=? AND key=?",
          row.tenant,
          row.key,
        );
    store.audit("models_erased", {
      models: models.map((m) => ({ model_id: m.id, name: m.name })),
      removed,
      surviving_models: store.get("SELECT COUNT(*) AS n FROM models").n,
    });
  });
  return { status: "erased", models, removed };
}

/** Operator-only, offline erasure. The Store lock excludes gateways and workers. */
export function eraseTenant(store: Store, tenant: string, apply = false) {
  requireThat(
    typeof tenant === "string" && tenant.length > 0 && tenant.length <= 256,
    "INVALID_SCHEMA",
    "Mandantenkennung fehlt oder ist zu lang.",
  );
  const models = store
    .all("SELECT id FROM models WHERE tenant=?", tenant)
    .map((r) => r.id);
  const revisions = models.flatMap((model) =>
    store.all("SELECT id FROM revisions WHERE model=?", model).map((r) => r.id),
  );
  const objects = new Set<string>([...models, ...revisions]);
  const counts: Record<string, number> = {
    models: models.length,
    revisions: revisions.length,
  };
  for (const table of [
    "transactions",
    "jobs",
    "artifacts",
    "selections",
  ] as const) {
    const rows = store.all(`SELECT id FROM ${table} WHERE tenant=?`, tenant);
    counts[table] = rows.length;
    for (const row of rows) objects.add(row.id);
  }
  for (const table of [
    "idempotency",
    "cache",
    "render_cache",
    "annotations",
    "agent_settings",
    "scheduler",
  ] as const)
    counts[table] = store.get(
      `SELECT COUNT(*) AS n FROM ${table} WHERE tenant=?`,
      tenant,
    ).n;
  const associated = (value: unknown): boolean => {
    if (typeof value === "string") return objects.has(value);
    if (Array.isArray(value)) return value.some(associated);
    if (value && typeof value === "object")
      return Object.entries(value).some(
        ([k, v]) => (k === "tenant" && v === tenant) || associated(v),
      );
    return false;
  };
  const audit = store.all("SELECT * FROM audit ORDER BY seq");
  const redacted = audit.filter((row) => associated(JSON.parse(row.data)));
  const outbox = store
    .all("SELECT id,payload FROM outbox")
    .filter((row) => associated(JSON.parse(row.payload)));
  counts.audit_events = redacted.length;
  counts.outbox = outbox.length;
  if (!apply)
    return {
      status: "dry_run",
      counts,
      backups: "unchanged; separate retention or erasure required",
    };
  store.db.exec("PRAGMA secure_delete=ON");
  store.atomic(() => {
    for (const row of outbox)
      store.run("DELETE FROM outbox WHERE id=?", row.id);
    for (const table of [
      "selections",
      "jobs",
      "transactions",
      "artifacts",
      "idempotency",
      "cache",
      "render_cache",
      "annotations",
      "agent_settings",
      "scheduler",
    ] as const)
      store.run(`DELETE FROM ${table} WHERE tenant=?`, tenant);
    for (const model of models)
      store.run("DELETE FROM revisions WHERE model=?", model);
    store.run("DELETE FROM models WHERE tenant=?", tenant);
    for (const row of redacted)
      store.run("DELETE FROM audit WHERE seq=?", row.seq);
    // A documented redaction creates a new chain over surviving event payloads.
    // Preserve their event times and order; anchor the previous chain's tip.
    let previous = "0".repeat(64);
    for (const row of store.all("SELECT * FROM audit ORDER BY seq")) {
      const digest = hash({
        event: row.event,
        data: JSON.parse(row.data),
        previous,
        created: row.created,
      });
      store.run(
        "UPDATE audit SET previous_hash=?,hash=? WHERE seq=?",
        previous,
        digest,
        row.seq,
      );
      previous = digest;
    }
    store.audit("retention_redaction", {
      previous_chain_tip: audit.at(-1)?.hash ?? "0".repeat(64),
      removed_event_count: redacted.length,
    });
  });
  // Workers also produce intermediate blobs. Offline GC removes every orphan,
  // retaining content still referenced by any remaining tenant.
  const gc = garbageCollect(store, 0);
  store.db.exec(
    "PRAGMA wal_checkpoint(TRUNCATE); VACUUM; PRAGMA wal_checkpoint(TRUNCATE);",
  );
  return {
    status: "active_store_erased",
    counts,
    deleted_blobs: gc.deleted_count,
    backups: "unchanged; separate retention or erasure required",
    storage_note:
      "No forensic erasure guarantee for SSD snapshots or external storage.",
  };
}
