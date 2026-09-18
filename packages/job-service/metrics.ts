import type { Store } from "../model-service/store.js";
/** Durable observability counters (Bauplan 24.7). Names are server constants, never request values. */
const NAMES = new Set([
  "tool_calls",
  "tool_failures",
  "tool_latency_ms",
  "blocked_commits",
  "ambiguous_selections",
  "stale_revisions",
  "budget_rejections",
  "precision_rejections",
  "jobs_succeeded",
  "jobs_failed",
  "jobs_cancelled",
  "job_retries",
  "job_continuations",
  "invalid_candidates",
  "worker_seconds",
  "worker_peak_rss_kib",
  "worker_cache_hits",
  "worker_warm_starts",
  "worker_cold_starts",
  "render_cache_hits",
  "dirty_features",
  "total_features",
  "preview_triangles",
  "field_active_cells",
  "export_loss_mm",
  "certificates_certified",
  "certificates_refused",
]);
export class Metrics {
  private window = new Map<string, number[]>();
  constructor(private store: Store) {}
  private key(name: string, label?: string) {
    if (!NAMES.has(name)) throw new Error("Unregistered metric " + name);
    return label
      ? name + "{" + label.replace(/[^a-zA-Z0-9_:-]/g, "_").slice(0, 64) + "}"
      : name;
  }
  increment(name: string, by = 1, label?: string) {
    this.observe(name, by, label);
  }
  observe(name: string, value: number, label?: string) {
    if (!Number.isFinite(value)) return;
    const key = this.key(name, label);
    const recent = this.window.get(key) ?? [];
    recent.push(value);
    if (recent.length > 512) recent.shift();
    this.window.set(key, recent);
    const write = () =>
      this.store.run(
        "INSERT INTO metrics(name,count,sum,max,updated) VALUES(?,1,?,?,?) ON CONFLICT(name) DO UPDATE SET count=count+1,sum=sum+excluded.sum,max=MAX(max,excluded.max),updated=excluded.updated",
        key,
        value,
        value,
        new Date().toISOString(),
      );
    try {
      if (this.store.db.isTransaction) write();
      else this.store.atomic(write);
    } catch {
      /* Telemetry must never block a geometric or transactional state change. */
    }
  }
  snapshot() {
    const rows = this.store.all("SELECT * FROM metrics ORDER BY name");
    const percentile = (values: number[], q: number) => {
      if (!values.length) return null;
      const sorted = [...values].sort((a, b) => a - b);
      return sorted[
        Math.min(sorted.length - 1, Math.ceil(sorted.length * q) - 1)
      ];
    };
    const totals: Record<string, number> = {};
    for (const r of rows) {
      const base = r.name.replace(/\{.*$/, "");
      totals[base] = (totals[base] ?? 0) + r.count;
    }
    return {
      totals,
      counters: Object.fromEntries(
        rows.map((r: any) => [
          r.name,
          {
            count: r.count,
            sum: r.sum,
            max: r.max,
            mean: r.count ? r.sum / r.count : null,
            updated: r.updated,
          },
        ]),
      ),
      recent_latency: Object.fromEntries(
        [...this.window.entries()]
          .filter(([k]) => k.startsWith("tool_latency_ms"))
          .map(([k, v]) => [
            k,
            {
              samples: v.length,
              p50_ms: percentile(v, 0.5),
              p95_ms: percentile(v, 0.95),
            },
          ]),
      ),
      error_rate: (() => {
        const calls = totals["tool_calls"] ?? 0;
        const failures = totals["tool_failures"] ?? 0;
        return calls ? failures / calls : null;
      })(),
      note: "Durable process-wide counters; latency percentiles cover this process's recent window only.",
    };
  }
}
