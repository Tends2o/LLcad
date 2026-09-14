import { Store } from "../model-service/store.js";

/** Durable round robin between principals, FIFO within each principal. */
export function claimQueuedJob(store: Store, lease: string, now = Date.now()) {
  return store.atomic(() => {
    const job = store.get(
      `SELECT jobs.* FROM jobs
      LEFT JOIN scheduler ON scheduler.tenant=jobs.tenant AND scheduler.owner=jobs.owner
      WHERE jobs.state='queued' OR (jobs.state='running' AND jobs.lease_until<?)
      ORDER BY COALESCE(scheduler.last_tick,0),jobs.created,jobs.id LIMIT 1`,
      now,
    );
    if (!job) return null;
    const tick = store.get(
      "SELECT COALESCE(MAX(last_tick),0)+1 AS next FROM scheduler",
    ).next;
    store.run(
      "INSERT INTO scheduler VALUES(?,?,?) ON CONFLICT(tenant,owner) DO UPDATE SET last_tick=excluded.last_tick",
      job.tenant,
      job.owner,
      tick,
    );
    store.run(
      "UPDATE jobs SET state='running',lease=?,lease_until=?,attempts=attempts+1 WHERE id=?",
      lease,
      now + 60000,
      job.id,
    );
    if (job.tx && job.kind === "evaluate")
      store.run(
        "UPDATE transactions SET state='executing' WHERE id=? AND state='planned'",
        job.tx,
      );
    return job;
  });
}
