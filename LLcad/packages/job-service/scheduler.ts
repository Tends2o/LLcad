import { Store } from "../model-service/store.js";

/** Durable round robin between principals, FIFO within each principal.
 *  A preview rendered ahead (request.ahead) is a convenience nobody waits for:
 *  it goes after every other queued job and waits while any evaluate is queued
 *  or running, so it never competes with the agent's next build for a core. */
export function claimQueuedJob(store: Store, lease: string, now = Date.now()) {
  return store.atomic(() => {
    const building = !!store.get(
      "SELECT 1 FROM jobs WHERE kind='evaluate' AND state IN ('queued','running') LIMIT 1",
    );
    const job = store.get(
      `SELECT jobs.* FROM jobs
      LEFT JOIN scheduler ON scheduler.tenant=jobs.tenant AND scheduler.owner=jobs.owner
      WHERE (jobs.state='queued' AND NOT (? AND jobs.kind='render' AND CASE WHEN jobs.kind='render' THEN json_extract(jobs.request,'$.ahead') IS 1 ELSE 0 END))
        OR (jobs.state='running' AND jobs.lease_until<?)
      ORDER BY CASE WHEN jobs.kind='render' THEN json_extract(jobs.request,'$.ahead') IS 1 ELSE 0 END,
        COALESCE(scheduler.last_tick,0),jobs.created,jobs.id LIMIT 1`,
      building ? 1 : 0,
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
