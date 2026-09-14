import test from "node:test";
import assert from "node:assert/strict";
import { DatabaseSync } from "node:sqlite";
import { join } from "node:path";
import { existsSync, readFileSync } from "node:fs";
import { setup, importFixture, principal, call, finish } from "../helpers.js";
import { sphere } from "../../scripts/fixtures.js";
import { Store } from "../../packages/model-service/store.js";
import { eraseTenant } from "../../packages/model-service/maintenance.js";
import { hash, id } from "../../packages/semantic-ir/hash.js";

test("tenant erasure removes derived data while retaining shared bytes and another tenant's model", async () => {
  const env = setup(),
    s = env.service;
  const other = { ...principal, tenant: "tenant-b", user: "bob" };
  let offline: Store | undefined;
  let closed = false;
  try {
    const a = await importFixture(s, sphere);
    const b = await importFixture(s, sphere, other);
    const rendered = await finish(
      s,
      call(s, "cad_render", {
        model_id: a.model_id,
        revision: a.revision,
        idempotency_key: id("render"),
      }),
    );
    const selected = call(s, "cad_find", {
      model_id: a.model_id,
      query: "sphere",
    });
    const secret = "tenant-a-private-upload-" + id("unique");
    const upload = s.store.artifact(
      principal,
      secret,
      "text/plain",
      null,
      null,
      {},
    );
    const otherUpload = s.store.artifact(
      other,
      "retained private upload",
      "text/plain",
      null,
      null,
      {},
    );
    const shared = s.store.revision(other, b.model_id).geometry.blobs;
    await s.close();
    closed = true;
    offline = new Store(env.dir);
    assert.equal(eraseTenant(offline, principal.tenant).status, "dry_run");
    assert.equal(offline.model(principal, a.model_id).head, a.revision);
    const result = eraseTenant(offline, principal.tenant, true);
    assert.equal(result.status, "active_store_erased");
    assert.ok(result.counts.revisions >= 2);
    assert.ok(result.counts.selections > 0);
    assert.ok(result.counts.artifacts >= rendered.artifacts.length + 1);
    assert.throws(
      () => offline!.model(principal, a.model_id),
      (e: any) => e.code === "ACCESS_DENIED",
    );
    for (const table of [
      "models",
      "transactions",
      "jobs",
      "selections",
      "artifacts",
      "cache",
      "idempotency",
    ])
      assert.equal(
        offline.get(
          `SELECT COUNT(*) AS n FROM ${table} WHERE tenant=?`,
          principal.tenant,
        ).n,
        0,
      );
    assert.equal(offline.revision(other, b.model_id).id, b.revision);
    assert.equal(
      offline.readBlob(otherUpload.hash).toString(),
      "retained private upload",
    );
    for (const blob of Object.values<string>(shared))
      assert.ok(offline.readBlob(blob).length);
    assert.equal(existsSync(join(env.dir, "blobs", upload.hash)), false);
    assert.equal(
      readFileSync(join(env.dir, "models.sqlite")).includes(
        Buffer.from(secret),
      ),
      false,
    );
    let previous = "0".repeat(64);
    for (const row of offline.all("SELECT * FROM audit ORDER BY seq")) {
      assert.equal(row.previous_hash, previous);
      assert.equal(
        row.hash,
        hash({
          event: row.event,
          data: JSON.parse(row.data),
          previous,
          created: row.created,
        }),
      );
      assert.equal(row.data.includes(a.model_id), false);
      previous = row.hash;
    }
  } finally {
    offline?.close();
    if (closed) {
      const { rmSync } = await import("node:fs");
      rmSync(env.dir, { recursive: true, force: true });
    } else await env.close();
  }
});

test("store lock excludes a second gateway or offline maintainer and is released on close", async () => {
  const env = setup();
  try {
    assert.throws(
      () => new Store(env.dir),
      (e: any) => e.code === "STORE_BUSY",
    );
  } finally {
    await env.service.close();
  }
  const reopened = new Store(env.dir);
  reopened.close();
  const { rmSync } = await import("node:fs");
  rmSync(env.dir, { recursive: true, force: true });
});

test("unknown database versions fail without rewriting schema or leaving the store locked", async () => {
  const env = setup();
  await env.service.close();
  const db = new DatabaseSync(join(env.dir, "models.sqlite"));
  db.exec("PRAGMA user_version=99");
  db.close();
  try {
    for (let i = 0; i < 2; i++)
      assert.throws(
        () => new Store(env.dir),
        (e: any) => e.code === "BUILD_MISMATCH",
      );
    const check = new DatabaseSync(join(env.dir, "models.sqlite"));
    assert.equal(
      (check.prepare("PRAGMA user_version").get() as any).user_version,
      99,
    );
    check.close();
  } finally {
    const { rmSync } = await import("node:fs");
    rmSync(env.dir, { recursive: true, force: true });
  }
});
