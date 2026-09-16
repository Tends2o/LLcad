import test from "node:test";
import assert from "node:assert/strict";
import { join } from "node:path";
import { mkdtempSync, rmSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { once } from "node:events";
import { DatabaseSync } from "node:sqlite";
import { setup, call, finish, importFixture, principal } from "../helpers.js";
import { SCOPES } from "../../packages/policy/index.js";
import { sphere } from "../../scripts/fixtures.js";
import { id } from "../../packages/semantic-ir/hash.js";
import { Store } from "../../packages/model-service/store.js";
import {
  applyRetention,
  backupStore,
  restoreStore,
  RETENTION_POLICY,
} from "../../packages/model-service/maintenance.js";
import { PIPELINE_POLICY } from "../../hooks/server-registry/index.js";
import { createApp } from "../../packages/mcp-gateway/app.js";

test("retention removes only expired previews and orphan cache generations; proofs and revisions stay", async () => {
  const env = setup(),
    s = env.service;
  try {
    const m = await importFixture(s, sphere);
    await finish(
      s,
      call(s, "cad_render", {
        model_id: m.model_id,
        revision: m.revision,
        idempotency_key: id("preview"),
      }),
    );
    const artifactsBefore = s.store.get(
      "SELECT COUNT(*) AS n FROM artifacts",
    ).n;
    const untouched = applyRetention(s.store);
    assert.equal(untouched.removed_preview_artifacts, 0);
    assert.equal(untouched.removed_cache_generations, 0);
    s.store.run(
      "INSERT INTO cache(tenant,key,blob,created) VALUES(?,?,?,?)",
      principal.tenant,
      "a".repeat(64),
      "b".repeat(64),
      Date.now() - RETENTION_POLICY.orphan_cache_ttl_ms - 1,
    );
    const later = applyRetention(
      s.store,
      Date.now() + RETENTION_POLICY.preview_artifact_ttl_ms + 1,
    );
    assert.equal(later.removed_preview_artifacts, 1);
    assert.equal(later.removed_cache_generations, 1);
    assert.equal(
      s.store.get("SELECT COUNT(*) AS n FROM artifacts").n,
      artifactsBefore - 1,
    );
    assert.equal(
      s.store.get("SELECT COUNT(*) AS n FROM cache WHERE key=?", "a".repeat(64))
        .n,
      0,
    );
    assert.equal(
      s.store.revision(principal, m.model_id, m.revision).quality,
      "checks_passed_within_profile",
    );
    assert.ok(
      s.store.get(
        "SELECT validation FROM transactions WHERE committed_revision=?",
        m.revision,
      ).validation,
    );
    assert.ok(
      s.store.all("SELECT * FROM audit WHERE event='retention_applied'")
        .length >= 2,
    );
  } finally {
    await env.close();
  }
});

test("a backup from storage version 5 restores and migrates to version 6 with unchanged heads and proofs", async () => {
  const env = setup();
  const work = mkdtempSync(join(tmpdir(), "mathforge-restore-v5-"));
  try {
    const m = await importFixture(env.service, sphere);
    const head = env.service.store.revision(principal, m.model_id, m.revision);
    await backupStore(env.service.store, join(work, "backup"));
    // Rewind the backup to the previous schema version: drop version-6 columns and tables.
    const db = new DatabaseSync(join(work, "backup", "models.sqlite"));
    db.exec("PRAGMA user_version=5");
    db.exec(
      "DROP TABLE IF EXISTS publications; DROP TABLE IF EXISTS publication_artifacts; DROP TABLE IF EXISTS metrics;",
    );
    db.exec(
      "ALTER TABLE jobs DROP COLUMN phase; ALTER TABLE jobs DROP COLUMN heartbeat; ALTER TABLE jobs DROP COLUMN budget_seconds; ALTER TABLE jobs DROP COLUMN started; ALTER TABLE jobs DROP COLUMN finished;",
    );
    db.exec(
      "ALTER TABLE selection_faces DROP COLUMN anchor; ALTER TABLE cache DROP COLUMN created; ALTER TABLE transactions DROP COLUMN repair_of; ALTER TABLE transactions DROP COLUMN repair_cause;",
    );
    db.close();
    const manifest = JSON.parse(
      readFileSync(join(work, "backup", "backup-manifest.json"), "utf8"),
    );
    manifest.database_hash = (
      await import("../../packages/semantic-ir/hash.js")
    ).bytesHash(readFileSync(join(work, "backup", "models.sqlite")));
    (await import("node:fs")).writeFileSync(
      join(work, "backup", "backup-manifest.json"),
      JSON.stringify(manifest),
    );
    restoreStore(join(work, "backup"), join(work, "restored"));
    const restored = new Store(join(work, "restored"));
    try {
      assert.equal(restored.get("PRAGMA user_version").user_version, 6);
      const r = restored.revision(principal, m.model_id, m.revision);
      assert.equal(r.ir_hash, head.ir_hash);
      assert.equal(restored.model(principal, m.model_id).head, m.revision);
      const migrations = restored
        .all(
          "SELECT data FROM audit WHERE event='job_progress_publication_migrated'",
        )
        .map((row: any) => JSON.parse(row.data));
      assert.deepEqual(
        migrations.map((m: any) => m.from_store_version),
        [0, 5],
      );
      assert.equal(
        migrations[1].existing_revisions_unchanged,
        restored.get("SELECT COUNT(*) AS n FROM revisions").n,
      );
      assert.ok(migrations[1].existing_revisions_unchanged >= 1);
      assert.equal(
        restored.get("SELECT COUNT(*) AS n FROM jobs WHERE phase IS NULL").n,
        restored.get("SELECT COUNT(*) AS n FROM jobs").n,
      );
    } finally {
      restored.close();
    }
  } finally {
    await env.close();
    rmSync(work, { recursive: true, force: true });
  }
});

test("the generated pipeline policy document matches the registry and metrics are readable over HTTP", async () => {
  assert.deepEqual(
    JSON.parse(
      readFileSync("hooks/server-registry/pipeline-policy.json", "utf8"),
    ),
    JSON.parse(JSON.stringify(PIPELINE_POLICY)),
  );
  assert.equal(PIPELINE_POLICY.defaults.unknown_operator, "deny");
  const env = setup(),
    s = env.service;
  const server = (await import("node:http")).createServer();
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  const url = `http://127.0.0.1:${(server.address() as any).port}`;
  const { app, auth } = createApp(s, {
    mode: "local",
    publicURL: url,
    dataRoot: env.dir,
  });
  server.on("request", app);
  try {
    // The local gateway token authenticates the local principal, so its artifacts must belong to it.
    const local = { tenant: "local", user: "local-user", scopes: SCOPES };
    const m = await importFixture(s, sphere, local);
    const response = await fetch(url + "/api/metrics", {
      headers: { Authorization: "Bearer " + auth.token },
    });
    assert.equal(response.status, 200);
    const metrics = await response.json();
    assert.ok(metrics.totals["tool_calls"] >= 4);
    assert.ok(metrics.counters["tool_calls{cad_import}"].count >= 1);
    assert.ok(metrics.counters["jobs_succeeded{evaluate}"].count >= 1);
    assert.ok(
      metrics.counters["worker_cold_starts"] ||
        metrics.counters["worker_warm_starts"],
    );
    assert.ok(metrics.error_rate === null || metrics.error_rate <= 1);
    assert.equal((await fetch(url + "/api/metrics")).status, 401);
    const preview = await finish(
      s,
      call(
        s,
        "cad_render",
        {
          model_id: m.model_id,
          revision: m.revision,
          idempotency_key: id("gz"),
        },
        local,
      ),
      local,
    );
    const compressed = await fetch(url + preview.artifacts[0].download, {
      headers: {
        Authorization: "Bearer " + auth.token,
        "Accept-Encoding": "gzip",
      },
    });
    assert.equal(compressed.status, 200);
    assert.ok((await compressed.json()).meshes.length >= 1);
    const job = call(
      s,
      "cad_job_get",
      {
        job_id: s.store.get(
          "SELECT id FROM jobs WHERE kind='render' ORDER BY created DESC LIMIT 1",
        ).id,
      },
      local,
    );
    assert.equal(job.phase, "succeeded");
    assert.ok(job.elapsed_seconds >= 0);
    assert.ok(job.budget_seconds > 0);
  } finally {
    await new Promise<void>((r) => server.close(() => r()));
    await env.close();
  }
});
