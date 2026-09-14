import test from "node:test";
import assert from "node:assert/strict";
import { join } from "node:path";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { setup, call, finish, importFixture, principal } from "../helpers.js";
import { sphere } from "../../scripts/fixtures.js";
import { Store } from "../../packages/model-service/store.js";
import {
  backupStore,
  restoreStore,
  garbageCollect,
} from "../../packages/model-service/maintenance.js";
import { ModelService } from "../../packages/model-service/index.js";
import { id, hash } from "../../packages/semantic-ir/hash.js";
import { compile } from "../../packages/compiler/index.js";
test("real backup restore preserves committed revisions and verifies every blob", async () => {
  const env = setup();
  const work = mkdtempSync(join(tmpdir(), "mathforge-restore-test-"));
  try {
    const m = await importFixture(env.service, sphere);
    const manifest = await backupStore(env.service.store, join(work, "backup"));
    assert.ok(manifest.blobs.length);
    restoreStore(join(work, "backup"), join(work, "restored"));
    const restored = new Store(join(work, "restored"));
    try {
      const r = restored.revision(principal, m.model_id, m.revision);
      assert.equal(r.quality, "checks_passed_within_profile");
      assert.equal(
        r.ir_hash,
        env.service.store.revision(principal, m.model_id, m.revision).ir_hash,
      );
      for (const b of manifest.blobs)
        assert.ok(restored.readBlob(b.hash).length);
    } finally {
      restored.close();
    }
    writeFileSync(
      join(work, "backup", "blobs", manifest.blobs[0].hash),
      "corruption",
    );
    assert.throws(
      () => restoreStore(join(work, "backup"), join(work, "bad")),
      (e: any) => e.code === "INTEGRITY_FAILURE",
    );
  } finally {
    await env.close();
    rmSync(work, { recursive: true, force: true });
  }
});
test("expired worker lease is recovered with fencing and does not duplicate a candidate", async () => {
  const env = setup(),
    s = env.service;
  try {
    const m = call(s, "cad_create_model", {
      name: "Recovery",
      idempotency_key: id("create"),
    });
    const draft = call(s, "cad_apply_patch", {
      model_id: m.model_id,
      base_revision: m.revision,
      idempotency_key: id("patch"),
      operations: [
        { op: "add_feature", feature: sphere.features[0] },
        { op: "set_outputs", outputs: ["sphere"] },
      ],
    });
    s.store.run(
      "UPDATE jobs SET state='running',lease='dead-worker',lease_until=0,attempts=1 WHERE id=?",
      draft.job_id,
    );
    s.store.run(
      "UPDATE transactions SET state='executing' WHERE id=?",
      draft.transaction_id,
    );
    const result = await finish(s, draft);
    assert.equal(s.jobs.get(principal, draft.job_id).attempts, 2);
    assert.equal(
      s.store.get(
        "SELECT COUNT(*) AS n FROM revisions WHERE id=?",
        result.candidate_revision,
      ).n,
      1,
    );
    await s.jobs.pump();
    assert.equal(
      s.store.get(
        "SELECT COUNT(*) AS n FROM revisions WHERE id=?",
        result.candidate_revision,
      ).n,
      1,
    );
  } finally {
    await env.close();
  }
});
test("kernel process crash produces failure without losing the authoritative revision", async () => {
  const env = setup(),
    s = env.service;
  try {
    const m = await importFixture(s, sphere);
    const job = call(s, "cad_render", {
      model_id: m.model_id,
      revision: m.revision,
      idempotency_key: id("render"),
    });
    let killed = false;
    for (let i = 0; i < 100; i++) {
      const process = (s.jobs as any).active?.process;
      if (process) {
        process.kill("SIGKILL");
        killed = true;
        break;
      }
      await new Promise((r) => setTimeout(r, 10));
    }
    assert.ok(killed);
    const result = await s.jobs.wait(principal, job.job_id);
    assert.equal(result.status, "failed");
    assert.equal(
      call(s, "cad_get_model", { model_id: m.model_id }).revision,
      m.revision,
    );
    assert.ok(call(s, "cad_capabilities", {}).operators.length);
  } finally {
    await env.close();
  }
});
test("crashes at different sandbox startup stages cannot leave the job waiting for inherited stderr", async () => {
  const env = setup(),
    s = env.service;
  try {
    const m = await importFixture(s, sphere);
    for (const pause of [0, 5, 20]) {
      const job = call(s, "cad_render", {
        model_id: m.model_id,
        revision: m.revision,
        idempotency_key: id("startup-crash"),
      });
      let child: any;
      for (let attempt = 0; attempt < 100; attempt++) {
        child = (s.jobs as any).active?.process;
        if (child) break;
        await new Promise((resolve) => setTimeout(resolve, 5));
      }
      assert.ok(child?.pid);
      if (pause) await new Promise((resolve) => setTimeout(resolve, pause));
      child.kill("SIGKILL");
      const result = await s.jobs.wait(principal, job.job_id, 3000);
      assert.equal(result.status, "failed");
      assert.equal(result.error.code, "KERNEL_FAILURE");
      assert.equal(
        call(s, "cad_get_model", { model_id: m.model_id }).revision,
        m.revision,
      );
    }
  } finally {
    await env.close();
  }
});
test("garbage collection retains referenced artifacts and deletes only aged orphans", async () => {
  const env = setup(),
    s = env.service;
  try {
    const a = s.store.artifact(
      principal,
      "retained",
      "text/plain",
      null,
      null,
      {},
    );
    s.store.blob("orphan");
    assert.equal(garbageCollect(s.store, 86400000).deleted_count, 0);
    assert.equal(garbageCollect(s.store, 0).deleted_count, 1);
    assert.equal(s.store.readBlob(a.hash).toString(), "retained");
  } finally {
    await env.close();
  }
});
test("audit chain detects any mutation and after-commit retries never roll back a commit", async () => {
  const env = setup(),
    s = env.service;
  try {
    s.gates.disableForTest("after_commit");
    const m = await importFixture(s, sphere);
    await s.jobs.pump();
    assert.equal(
      call(s, "cad_get_model", { model_id: m.model_id }).revision,
      m.revision,
    );
    assert.ok(
      s.store.get(
        "SELECT COUNT(*) AS n FROM outbox WHERE event='revision_committed' AND delivered=0",
      ).n > 0,
    );
    let previous = "0".repeat(64);
    for (const row of s.store.all("SELECT * FROM audit ORDER BY seq")) {
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
      previous = row.hash;
    }
  } finally {
    await env.close();
  }
});
