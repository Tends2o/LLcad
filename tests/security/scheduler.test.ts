import test from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { Store } from "../../packages/model-service/store.js";
import { claimQueuedJob } from "../../packages/job-service/scheduler.js";
test("scheduler interleaves principals, keeps FIFO and retains fairness after reopening", () => {
  const dir = mkdtempSync(join(tmpdir(), "mathforge-scheduler-"));
  let store = new Store(dir);
  try {
    let order = 0;
    for (const [owner, count] of [
      ["alice", 4],
      ["bob", 3],
      ["carol", 1],
    ] as const)
      for (let i = 0; i < count; i++)
        store.run(
          "INSERT INTO jobs(id,tenant,owner,model,kind,state,request,created) VALUES(?,?,?,?,?,?,?,?)",
          `${owner}-${i}`,
          "tenant",
          owner,
          "model",
          "scheduler-fixture",
          "queued",
          "{}",
          String(order++).padStart(4, "0"),
        );
    const claimed = [];
    for (let i = 0; i < 8; i++) {
      if (i === 2) {
        store.close();
        store = new Store(dir);
      }
      const job = claimQueuedJob(store, "lease-" + i, 1000);
      claimed.push(job.id);
      store.run(
        "UPDATE jobs SET state='succeeded',lease=NULL,lease_until=NULL WHERE id=?",
        job.id,
      );
    }
    assert.deepEqual(claimed, [
      "alice-0",
      "bob-0",
      "carol-0",
      "alice-1",
      "bob-1",
      "alice-2",
      "bob-2",
      "alice-3",
    ]);
    assert.equal(claimQueuedJob(store, "empty", 1000), null);
    store.run(
      "UPDATE jobs SET state='running',lease='expired',lease_until=999 WHERE id='carol-0'",
    );
    const retry = claimQueuedJob(store, "recovered", 1000);
    assert.equal(retry.id, "carol-0");
    assert.equal(
      store.get("SELECT attempts FROM jobs WHERE id='carol-0'").attempts,
      2,
    );
    assert.equal(claimQueuedJob(store, "no-duplicate", 1001), null);
  } finally {
    store.close();
    rmSync(dir, { recursive: true, force: true });
  }
});
