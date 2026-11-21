import { cpus, freemem } from "node:os";
import { readFileSync } from "node:fs";

/** Parallel evaluation of independent subtrees inside one evaluate job
 *  (opt-in). LLCAD_PARALLEL_EVAL=<n> lets main.py fork up to n processes (the
 *  parent included) for a cold or largely dirty build: shared inputs first,
 *  then whole part subtrees per process, every archive content-keyed as in a
 *  serial run. Unset, 0 or 1 = serial (the default). The count is capped per
 *  box at half its processors (2 on 4 vCPU), and the gateway asks for it only
 *  while the service has at least LLCAD_PARALLEL_MIN_FREE_MB (default 1536) of
 *  memory headroom - host MemAvailable and, where set, the cgroup's
 *  memory.max. The worker checks MemAvailable itself again before it forks and
 *  records in metrics.parallel what it did and why. */
export const PARALLEL_ACTIONS = new Set(["evaluate", "render", "export"]);

export function parallelBoxLimit() {
  return Math.max(1, Math.min(4, Math.floor(cpus().length / 2)));
}

function meminfoAvailableMb(): number | null {
  try {
    const line = /^MemAvailable:\s+(\d+)\s+kB/m.exec(
      readFileSync("/proc/meminfo", "utf8"),
    );
    return line ? Math.floor(Number(line[1]) / 1024) : null;
  } catch {
    return null;
  }
}

/** Headroom under this process's cgroup v2 memory.max (systemd MemoryMax), or
 *  null when there is no numeric limit. */
function cgroupHeadroomMb(): number | null {
  try {
    const path = readFileSync("/proc/self/cgroup", "utf8")
      .split("\n")
      .find((line) => line.startsWith("0::"))
      ?.slice(3)
      .trim();
    if (path === undefined) return null;
    const base = "/sys/fs/cgroup" + (path === "/" ? "" : path);
    const max = readFileSync(base + "/memory.max", "utf8").trim();
    if (!/^\d+$/.test(max)) return null;
    const current = Number(
      readFileSync(base + "/memory.current", "utf8").trim(),
    );
    return Math.floor((Number(max) - current) / 1024 / 1024);
  } catch {
    return null;
  }
}

export function memoryHeadroomMb() {
  const values = [
    meminfoAvailableMb() ?? Math.floor(freemem() / 1024 / 1024),
    cgroupHeadroomMb(),
  ].filter((v): v is number => v !== null);
  return Math.min(...values);
}

/** The `parallel_eval` field for a main.py request, or undefined (serial). */
export function parallelEval(request: any, env = process.env) {
  if (!PARALLEL_ACTIONS.has(request.action ?? "evaluate")) return undefined;
  const requested = Math.floor(Number(env.LLCAD_PARALLEL_EVAL ?? "1"));
  if (!Number.isFinite(requested) || requested < 2) return undefined;
  const processes = Math.min(requested, parallelBoxLimit());
  const minFree = Math.max(
    256,
    Number(env.LLCAD_PARALLEL_MIN_FREE_MB ?? "1536") || 1536,
  );
  const headroom = memoryHeadroomMb();
  // Below the headroom the job runs serially; the worker's metrics say why.
  if (processes < 2 || headroom < minFree)
    return {
      processes: 1,
      requested,
      gateway_headroom_mb: headroom,
      skipped: processes < 2 ? "box_limit" : "memory",
    };
  return {
    processes,
    requested,
    min_free_mb: minFree,
    gateway_headroom_mb: headroom,
  };
}
