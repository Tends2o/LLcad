import { spawn, spawnSync, ChildProcess } from "node:child_process";
import {
  mkdtempSync,
  mkdirSync,
  writeFileSync,
  readFileSync,
  chmodSync,
  rmSync,
  existsSync,
  statSync,
  lstatSync,
} from "node:fs";
import { linkSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { tmpdir } from "node:os";
import { readdirSync } from "node:fs";
import { createHash } from "node:crypto";
import { Store } from "../model-service/store.js";
import { CadError, requireThat } from "../semantic-ir/errors.js";
import { LIMITS, referencedArtifacts } from "../compiler/index.js";
import { fontBinds } from "../compiler/fonts.js";
import { hash } from "../semantic-ir/hash.js";
import { checkNativeMeshBuild } from "../compiler/native-build.js";
import { parallelEval } from "./parallel.js";
import { IS_WINDOWS, venvPython } from "../semantic-ir/host.js";
export const PROJECT_ROOT = resolve(
  process.env.MATHFORGE_ROOT ?? process.cwd(),
);
/** A run that ran out of time still built something. Every finished feature is
 *  a file named after its own cache key, and a feature's archive says which
 *  bytes it belongs to — so the pieces can be checked and kept even though the
 *  run as a whole failed. The next attempt then starts where this one stopped,
 *  which is what lets a model of any size be built inside a fixed time budget. */
/** Putting the cache into the sandbox: a hard link where the blob store and the
 *  work directory share a filesystem, a copy where they do not. Linking costs
 *  nothing and the blobs are read-only for everyone, so the sandbox cannot
 *  change what it borrows. */
function stage(store: Store, blob: string, path: string) {
  try {
    linkSync(store.path(blob), path);
  } catch {
    writeFileSync(path, store.readBlob(blob), { mode: 0o444 });
  }
}
/** Keeps every feature archive a run wrote, whether the run finished or ran out
 *  of time: a finished evaluate, render or measurement built its uncached
 *  features just as a timed-out round did, and dropping them made every later
 *  job rebuild the same frontier again. Only pairs whose archive names its own
 *  key and the sha256 of the B-Rep bytes beside it are kept; with `allowed`,
 *  only keys of the plan the run was given. */
function salvage(
  store: Store,
  tenant: string,
  dir: string,
  allowed?: Set<string>,
) {
  const out = join(dir, "out");
  let kept = 0;
  if (!existsSync(out)) return kept;
  for (const name of readdirSync(out)) {
    const key = /^([a-f0-9]{64})\.topology\.json$/.exec(name)?.[1];
    if (!key || (allowed && !allowed.has(key))) continue;
    try {
      const history = readFileSync(join(out, name)),
        archive = JSON.parse(history.toString("utf8")),
        shape = readFileSync(join(out, key + ".brep"));
      // The archive names the exact bytes it describes; a half-written pair
      // cannot pass this and is left behind.
      if (
        archive.cache_key !== key ||
        archive.brep_sha256 !== createHash("sha256").update(shape).digest("hex")
      )
        continue;
      store.atomic(() => {
        store.run(
          "INSERT OR IGNORE INTO cache(tenant,key,blob,created) VALUES(?,?,?,?)",
          tenant,
          key,
          store.blob(shape),
          Date.now(),
        );
        store.run(
          "INSERT OR IGNORE INTO cache(tenant,key,blob,created) VALUES(?,?,?,?)",
          tenant,
          key + ":topology",
          store.blob(history),
          Date.now(),
        );
      });
      const measured = join(out, key + ".facts.json");
      if (existsSync(measured))
        store.run(
          "INSERT OR IGNORE INTO cache(tenant,key,blob,created) VALUES(?,?,?,?)",
          tenant,
          key + ":facts",
          store.blob(readFileSync(measured)),
          Date.now(),
        );
      kept++;
    } catch {
      /* an unfinished pair is simply not kept */
    }
  }
  return kept;
}
/** Whether a feature's shape, history and measured facts are all cached. */
function completeIn(store: Store, tenant: string) {
  const has = (key: string) =>
    !!store.get("SELECT 1 FROM cache WHERE tenant=? AND key=?", tenant, key);
  return (f: any) => {
    const local = f.local_cache_key ?? f.cache_key;
    return (
      f.authoritative_representation === "brep" &&
      has(f.cache_key + ":facts") &&
      has(local) &&
      has(local + ":topology")
    );
  };
}
/** The shapes an action itself opens after the build: a render draws its
 *  targets, an export writes its outputs, a measurement reads its features. An
 *  evaluate needs none of them — the model aggregate is combined from each
 *  output's cached facts — unless the manufacturing profile measures the
 *  outputs' walls. */
function actionTargets(request: any): string[] {
  const plan = request.plan;
  switch (request.action) {
    case "render":
      return request.feature_id ? [request.feature_id] : (plan.outputs ?? []);
    case "export":
      return plan.outputs ?? [];
    case "distance":
    case "analysis":
      return [request.feature_id, request.other_feature_id].filter(Boolean);
    case "contact_graph":
      return (request.nodes ?? []).flatMap((n: any) => n.features ?? []);
    default:
      return plan.profile === "manufacturing_candidate"
        ? (plan.outputs ?? [])
        : [];
  }
}
/** The features this run actually has to open: what the action reads,
 *  everything whose shape or measured facts are missing from the cache, and
 *  whatever those build on. The rest is reported from its stored facts without
 *  ever being loaded — the cache is staged in full regardless, so the decision
 *  can never leave the run without a shape it turns out to need. */
function shapesNeeded(store: Store, tenant: string, request: any) {
  const plan = request.plan;
  if (request.script === "preview" || !plan?.features?.length) return null;
  const complete = completeIn(store, tenant);
  const needed = new Set<string>(actionTargets(request));
  for (const f of [...plan.features].reverse()) {
    // Only a feature that has to be rebuilt needs what it was built from. One
    // that comes out of the cache is simply read back, and its own inputs stay
    // where they are — otherwise a single changed screw would drag the whole
    // construction tree into the sandbox.
    if (complete(f)) continue;
    needed.add(f.id);
    for (const dependency of f.depends_on ?? []) needed.add(dependency);
  }
  return needed;
}
/** Native measurements between named B-Rep features. */
const TARGETED_METRICS = new Set([
  "clearance",
  "surface_distance",
  "wall_thickness",
  "angle",
  "curvature",
]);
/** A measurement reads a few named features of an evaluated revision. Its
 *  sandbox gets exactly those, plus — for a feature that is not fully cached —
 *  what it is rebuilt from, down to cached inputs. No other feature is staged,
 *  opened or re-measured, and no model aggregate is formed. Anything that is
 *  not plain B-Rep keeps the whole plan. */
function targetedRequest(store: Store, tenant: string, request: any) {
  const plan = request.plan;
  const targeted =
    request.action === "distance" ||
    request.action === "contact_graph" ||
    (request.action === "analysis" &&
      TARGETED_METRICS.has(request.metric) &&
      request.point_local === undefined);
  if (!targeted || !plan?.features?.length) return request;
  const byID = new Map<string, any>(plan.features.map((f: any) => [f.id, f]));
  const complete = completeIn(store, tenant);
  const keep = new Set<string>();
  const visit = (fid: string) => {
    if (keep.has(fid)) return;
    const f = byID.get(fid);
    if (!f) return;
    keep.add(fid);
    if (!complete(f)) for (const d of f.depends_on ?? []) visit(d);
  };
  const targets = actionTargets(request);
  if (!targets.length || !targets.every((t) => byID.has(t))) return request;
  targets.forEach(visit);
  if ([...keep].some((fid) => byID.get(fid).authoritative_representation !== "brep"))
    return request;
  return {
    ...request,
    plan: {
      ...plan,
      features: plan.features.filter((f: any) => keep.has(f.id)),
      outputs: [],
    },
  };
}
/** Exit statuses of a sandbox the kernel stopped for exceeding a resource
 *  limit: 152 = 128 + SIGXCPU (processor limit), 137 = 128 + SIGKILL (the hard
 *  processor limit or the memory controller). The gateway's own wall-clock
 *  timer and cancellations are recognised before these. */
const RESOURCE_EXIT = new Map<number, string>([
  [152, "cpu_limit"],
  [137, "killed_cpu_or_memory_limit"],
]);
/** One isolated, single-use worker process bound to its own work directory:
 *  bubblewrap on Linux; on Windows the interpreter itself, confined by the Job
 *  Object it creates (hostlimits.py) and stopped together with its process tree. */
type Sandbox = {
  dir: string;
  process: ChildProcess;
  errors: string;
  exited: Promise<number | null>;
  warm: boolean;
  spawned: number;
  script: string;
};
/** main.py evaluates; preview.py only tessellates cached shapes. */
const WARM_SCRIPTS = ["main", "preview"] as const;
/** Pre-warmed sandboxes hold an imported kernel in memory while idle. Windows
 *  keeps none by default, so an idle service costs nothing. */
const WARM_POOL_TARGET = Math.max(
  0,
  Math.min(
    2,
    Number(process.env.MATHFORGE_WARM_WORKERS ?? (IS_WINDOWS ? "0" : "1")),
  ),
);
export class Worker {
  private static pythonPrefix: string | null = null;
  private static pool: Sandbox[] = [];
  private static warming = 0;
  private static draining = false;
  process: ChildProcess | null = null;
  cancelled = false;
  static probe() {
    if (IS_WINDOWS) return existsSync(venvPython(PROJECT_ROOT));
    const result = spawnSync(
      "bwrap",
      [
        "--unshare-all",
        "--ro-bind",
        "/usr",
        "/usr",
        "--symlink",
        "usr/lib",
        "/lib",
        "--symlink",
        "usr/lib64",
        "/lib64",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--",
        "/usr/bin/true",
      ],
      { timeout: 3000, encoding: "utf8" },
    );
    return result.status === 0;
  }
  cancel() {
    this.cancelled = true;
    this.killGroup();
  }
  private killGroup(child = this.process) {
    Worker.killSandbox(child);
  }
  private static killSandbox(child: ChildProcess | null | undefined) {
    if (!child?.pid) return;
    if (IS_WINDOWS) {
      // The venv's python.exe is a launcher that starts the interpreter as its
      // child: end the whole tree, and never a PID that has already exited.
      if (child.exitCode !== null || child.signalCode !== null) return;
      const killed = spawnSync(
        "taskkill",
        ["/PID", String(child.pid), "/T", "/F"],
        { windowsHide: true, stdio: "ignore", timeout: 5000 },
      );
      if (killed.status !== 0) child.kill();
      return;
    }
    try {
      process.kill(-child.pid, "SIGKILL");
    } catch (error: any) {
      if (error.code !== "ESRCH") child.kill("SIGKILL");
    }
  }
  private static prefix() {
    if (Worker.pythonPrefix === null) {
      const detected = spawnSync(
        join(PROJECT_ROOT, ".venv/bin/python"),
        ["-c", "import sys; print(sys.base_prefix)"],
        { encoding: "utf8", timeout: 3000 },
      );
      requireThat(
        detected.status === 0,
        "SANDBOX_UNAVAILABLE",
        "Python base installation not available.",
      );
      Worker.pythonPrefix = detected.stdout.trim();
      requireThat(
        Worker.pythonPrefix.startsWith("/") && Worker.pythonPrefix !== "/",
        "SANDBOX_UNAVAILABLE",
        "Invalid Python base installation.",
      );
    }
    return Worker.pythonPrefix;
  }
  /** Windows: the interpreter runs directly in its work directory with a minimal
   *  environment; its temporary files stay inside that directory as well. */
  private static spawnWindows(dir: string, wait: boolean, script: string) {
    const python = venvPython(PROJECT_ROOT),
      system = process.env.SystemRoot ?? "C:\\Windows",
      temp = join(dir, "tmp");
    mkdirSync(temp, { recursive: true });
    return spawn(
      python,
      [
        join(PROJECT_ROOT, "workers", "cad-occt", script + ".py"),
        ...(wait ? ["--wait"] : []),
      ],
      {
        cwd: dir,
        windowsHide: true,
        stdio: ["ignore", "ignore", "pipe"],
        env: {
          SystemRoot: system,
          PATH: [dirname(python), join(system, "System32")].join(";"),
          TEMP: temp,
          TMP: temp,
          OPENBLAS_NUM_THREADS: "1",
          OMP_NUM_THREADS: "1",
          PYTHONDONTWRITEBYTECODE: "1",
          PYTHONUTF8: "1",
        },
      },
    );
  }
  /** Spawn the isolated process; with wait=true it imports the kernel and idles until request.json exists. */
  private static launch(dir: string, wait: boolean, script: string): Sandbox {
    const child = IS_WINDOWS
      ? Worker.spawnWindows(dir, wait, script)
      : Worker.spawnBubblewrap(dir, wait, script);
    const sandbox: Sandbox = {
      dir,
      process: child,
      errors: "",
      exited: new Promise((resolveExit) => {
        // A crash before bubblewrap finishes startup can leave its namespace
        // helper holding stderr open. Reap the dedicated group on exit too.
        if (!IS_WINDOWS) child.once("exit", () => Worker.killSandbox(child));
        child.on("error", () => resolveExit(-1));
        child.on("close", (code) => resolveExit(code));
      }),
      warm: wait,
      spawned: Date.now(),
      script,
    };
    child.stderr?.on("data", (chunk) => {
      if (sandbox.errors.length < 4096) sandbox.errors += chunk.toString();
    });
    return sandbox;
  }
  private static spawnBubblewrap(dir: string, wait: boolean, script: string) {
    const prefix = Worker.prefix();
    const args = [
      "--die-with-parent",
      "--unshare-all",
      "--new-session",
      "--uid",
      "65534",
      "--gid",
      "65534",
      "--cap-drop",
      "ALL",
      "--ro-bind",
      "/usr",
      "/usr",
      ...(prefix === "/usr" ? [] : ["--ro-bind", prefix, prefix]),
      "--symlink",
      "usr/lib",
      "/lib",
      "--symlink",
      "usr/lib64",
      "/lib64",
      "--dir",
      "/etc",
      "--ro-bind",
      "/etc/ld.so.cache",
      "/etc/ld.so.cache",
      "--proc",
      "/proc",
      "--dev",
      "/dev",
      "--tmpfs",
      "/tmp",
      "--ro-bind",
      join(PROJECT_ROOT, ".venv"),
      "/opt/venv",
      "--ro-bind",
      join(PROJECT_ROOT, "workers/cad-occt"),
      "/worker",
      // Whitelisted text fonts, read-only at fixed paths (/fonts/<family>-<style>.ttf).
      ...fontBinds(),
      "--bind",
      dir,
      "/work",
      "--chdir",
      "/work",
      "--clearenv",
      "--setenv",
      "PATH",
      "/usr/bin",
      "--setenv",
      "OPENBLAS_NUM_THREADS",
      "1",
      "--setenv",
      "OMP_NUM_THREADS",
      "1",
      "--setenv",
      "PYTHONDONTWRITEBYTECODE",
      "1",
      "--",
      "/opt/venv/bin/python",
      `/worker/${script}.py`,
      ...(wait ? ["--wait"] : []),
    ];
    return spawn("bwrap", args, {
      detached: true,
      stdio: ["ignore", "ignore", "pipe"],
      env: { PATH: "/usr/bin:/bin" },
    });
  }
  /** Work directories live beside the blob store, so staging can link instead
   *  of copy; /tmp is the fallback when that directory cannot be used. */
  private static workRoot() {
    const root = join(
      resolve(process.env.MATHFORGE_DATA ?? join(PROJECT_ROOT, "data")),
      "work",
    );
    try {
      mkdirSync(root, { recursive: true, mode: 0o700 });
      return root;
    } catch {
      return tmpdir();
    }
  }
  private static newDirectory() {
    const dir = mkdtempSync(join(Worker.workRoot(), "mathforge-"));
    if (!IS_WINDOWS) chmodSync(dir, 0o700);
    mkdirSync(join(dir, "out"), { mode: 0o700 });
    mkdirSync(join(dir, "cache"), { mode: 0o755 });
    return dir;
  }
  /** Keep one idle pre-warmed sandbox per script ready; each sandbox serves
   *  exactly one job and then exits. */
  static ensurePool() {
    if (WARM_POOL_TARGET === 0 || Worker.draining || !Worker.probe()) return;
    for (const script of WARM_SCRIPTS)
      while (
        Worker.pool.filter((s) => s.script === script).length + Worker.warming <
        WARM_POOL_TARGET
      ) {
        Worker.warming++;
        try {
          const sandbox = Worker.launch(Worker.newDirectory(), true, script);
          Worker.pool.push(sandbox);
          void sandbox.exited.then(() => {
            // An idle sandbox that timed out or died leaves the pool silently;
            // a fresh one follows, so the next job still starts warm.
            const index = Worker.pool.indexOf(sandbox);
            if (index >= 0) {
              Worker.pool.splice(index, 1);
              rmSync(sandbox.dir, { recursive: true, force: true });
              setTimeout(() => Worker.ensurePool(), 5000).unref();
            }
          });
        } finally {
          Worker.warming--;
        }
      }
  }
  static drainPool() {
    Worker.draining = true;
    for (const sandbox of Worker.pool.splice(0)) {
      Worker.killSandbox(sandbox.process);
      rmSync(sandbox.dir, { recursive: true, force: true });
    }
  }
  static poolSize() {
    return Worker.pool.length;
  }
  private static takeWarm(script: string): Sandbox | null {
    for (const sandbox of Worker.pool.filter((s) => s.script === script)) {
      Worker.pool.splice(Worker.pool.indexOf(sandbox), 1);
      if (
        sandbox.process.exitCode === null &&
        Date.now() - sandbox.spawned < 540000
      )
        return sandbox;
      Worker.killSandbox(sandbox.process);
      rmSync(sandbox.dir, { recursive: true, force: true });
    }
    return null;
  }
  async run(store: Store, tenant: string, request: any) {
    checkNativeMeshBuild();
    const seconds = request.policy_budget_seconds ?? LIMITS.job_seconds;
    const expires = request.policy_expires_at ?? null;
    requireThat(
      Number.isInteger(seconds) &&
        seconds >= 1 &&
        seconds <= LIMITS.job_seconds,
      "BUDGET_EXCEEDED",
      "Invalid approved worker budget.",
    );
    requireThat(
      expires === null ||
        (Number.isSafeInteger(expires) && expires > Date.now()),
      "ACCESS_DENIED",
      "The worker approval has expired.",
    );
    requireThat(
      Worker.probe(),
      "SANDBOX_UNAVAILABLE",
      "The required isolated worker cannot be started.",
    );
    if (this.cancelled)
      throw new CadError("CANCELLED", "The job was cancelled.");
    const script = request.script === "preview" ? "preview" : "main";
    const warm = Worker.takeWarm(script);
    const dir = warm?.dir ?? Worker.newDirectory();
    try {
      // A measurement is given only the features it reads (and what is
      // missing underneath them), not the whole model.
      request = targetedRequest(store, tenant, request);
      // Only what this run has to touch is copied into the sandbox. A feature
      // whose shape and measured facts are both in the cache, and that nothing
      // being rebuilt depends on, travels as its facts alone — a few kilobytes
      // instead of a few hundred.
      const staged = shapesNeeded(store, tenant, request);
      for (const f of request.plan.features) {
        if (
          f.construction.operator === "field" &&
          request.cache_model &&
          request.cache_owner
        ) {
          const previous = store.get(
            "SELECT blob FROM cache WHERE tenant=? AND key=?",
            tenant,
            "field:" + hash([request.cache_owner, request.cache_model, f.id]),
          );
          if (previous)
            writeFileSync(
              join(dir, "cache", f.cache_key + ".field.json"),
              store.readBlob(previous.blob),
              { mode: 0o444 },
            );
        }
        const measured = store.get(
          "SELECT blob FROM cache WHERE tenant=? AND key=?",
          tenant,
          f.cache_key + ":facts",
        );
        if (measured)
          stage(
            store,
            measured.blob,
            join(dir, "cache", f.cache_key + ".facts.json"),
          );
        for (const key of new Set<string>([
          f.cache_key,
          f.local_cache_key ?? f.cache_key,
        ])) {
          const cached = store.get(
            "SELECT blob FROM cache WHERE tenant=? AND key=?",
            tenant,
            key,
          );
          if (cached)
            stage(store, cached.blob, join(dir, "cache", key + ".brep"));
          const topology = store.get(
            "SELECT blob FROM cache WHERE tenant=? AND key=?",
            tenant,
            key + ":topology",
          );
          if (topology)
            stage(
              store,
              topology.blob,
              join(dir, "cache", key + ".topology.json"),
            );
        }
      }
      for (const input of [
        ...referencedArtifacts(request.plan.features),
        ...(request.inputs ?? []),
      ]) {
        requireThat(
          ["step", "stl", "vdb", "svg"].includes(input.format),
          "OUT_OF_SCOPE",
          "Unregistered input format.",
        );
        const a = store.get(
          "SELECT * FROM artifacts WHERE id=? AND tenant=?",
          input.artifact_id,
          tenant,
        );
        requireThat(a, "ACCESS_DENIED", "Import artifact is missing.");
        writeFileSync(
          join(dir, "input-" + a.id + "." + input.format),
          store.readBlob(a.hash),
          { mode: 0o444 },
        );
      }
      // The request is always the last input written: a pre-warmed sandbox starts on its appearance.
      const sandbox = warm ?? Worker.launch(dir, false, script);
      this.process = sandbox.process;
      // Opt-in parallel subtree evaluation (LLCAD_PARALLEL_EVAL, see parallel.ts).
      const parallel = script === "main" ? parallelEval(request) : undefined;
      writeFileSync(
        join(dir, "request.json"),
        JSON.stringify({
          ...request,
          ...(staged ? { shapes_needed: [...staged] } : {}),
          ...(parallel ? { parallel_eval: parallel } : {}),
        }),
        { mode: 0o444 },
      );
      const started = Date.now();
      let timedOut = false;
      const timer = setTimeout(
        () => {
          timedOut = true;
          this.killGroup(sandbox.process);
        },
        Math.max(
          0,
          Math.min(
            seconds * 1000,
            expires === null ? Infinity : expires - Date.now(),
          ),
        ),
      );
      const code = await sandbox.exited;
      clearTimeout(timer);
      this.process = null;
      if (this.cancelled)
        throw new CadError("CANCELLED", "The job was cancelled.");
      // Archives are kept only for keys of the plan this run was given.
      const planKeys = new Set<string>(
        (request.plan?.features ?? []).flatMap((f: any) => [
          f.cache_key,
          f.local_cache_key ?? f.cache_key,
        ]),
      );
      if (timedOut)
        throw new CadError(
          "BUDGET_EXCEEDED",
          "Worker time budget exceeded.",
          { salvaged: salvage(store, tenant, dir, planKeys) },
        );
      if (code === -1)
        throw new CadError(
          "SANDBOX_UNAVAILABLE",
          "The worker could not be started.",
        );
      // A sandbox stopped by the processor limit (or the hard limit's kill) ran
      // out of budget; it did not hit a kernel defect. Its finished features are
      // kept, and an evaluate continues from them like a timed-out round.
      if (code !== null && RESOURCE_EXIT.has(code))
        throw new CadError(
          "BUDGET_EXCEEDED",
          "Worker compute time budget exhausted; finished features stay in the cache.",
          {
            exit_code: code,
            cause: RESOURCE_EXIT.get(code),
            salvaged: salvage(store, tenant, dir, planKeys),
          },
        );
      if (code !== 0)
        throw new CadError(
          "KERNEL_FAILURE",
          "The worker ended prematurely.",
          {
            exit_code: code,
            salvaged: salvage(store, tenant, dir, planKeys),
          },
        );
      // Native stderr is deliberately not persisted: it may contain imported data.
      const resultPath = join(dir, "result.json");
      requireThat(
        existsSync(resultPath) &&
          lstatSync(resultPath).isFile() &&
          !lstatSync(resultPath).isSymbolicLink() &&
          statSync(resultPath).size < LIMITS.max_artifact_bytes,
        "KERNEL_FAILURE",
        "The worker result is missing or too large.",
      );
      const result = JSON.parse(readFileSync(resultPath, "utf8"));
      if (result.status === "failed") {
        // Operator diagnostics only: native stderr may contain imported data and is never persisted.
        if (process.env.MATHFORGE_DEBUG_ERRORS)
          console.error("[worker stderr]", sandbox.errors.slice(-2000));
        // The worker caught its own processor limit: what it finished is kept.
        if (result.error?.code === "BUDGET_EXCEEDED")
          throw new CadError(result.error.code, result.error.message, {
            salvaged: salvage(store, tenant, dir, planKeys),
          });
        throw new CadError(result.error.code, result.error.message);
      }
      let total = 0;
      const blobs: Record<string, string> = {};
      requireThat(
        lstatSync(join(dir, "out")).isDirectory() &&
          !lstatSync(join(dir, "out")).isSymbolicLink(),
        "KERNEL_FAILURE",
        "Invalid worker output directory.",
      );
      // Every feature archive this run built goes straight into the cache, for
      // evaluate, render and measurement runs alike (paired by key and B-Rep
      // hash, as after a timed-out round). The result keeps only what the job
      // itself returns plus the measured facts, which are picked up from the
      // directory rather than from the result list.
      const kept = salvage(store, tenant, dir, planKeys);
      const names = [
        ...result.files,
        ...readdirSync(join(dir, "out")).filter((name) =>
          /^[a-f0-9]{64}\.facts\.json$/.test(name),
        ),
      ];
      for (const name of new Set<string>(names)) {
        requireThat(
          /^(model\.(brep|step|stl|vdb)|preview\.json|roundtrip\.json|view\.(svg|json)|[a-f0-9]{64}\.(brep|topology\.json|facts\.json|field\.json|mesh\.json))$/.test(
            name,
          ),
          "KERNEL_FAILURE",
          "Unregistered worker artifact.",
        );
        const path = join(dir, "out", name);
        const stat = lstatSync(path);
        total += stat.size;
        requireThat(
          stat.isFile() &&
            stat.size <= LIMITS.max_artifact_bytes &&
            total <= 128 * 1024 * 1024,
          "BUDGET_EXCEEDED",
          "Artifact budget exceeded.",
        );
        blobs[name] = store.blob(readFileSync(path));
      }
      if (result.metrics && typeof result.metrics === "object")
        result.metrics.archives_kept = kept;
      if (result.metrics && typeof result.metrics === "object")
        result.metrics.gateway_wall_seconds = (Date.now() - started) / 1000;
      return { ...result, blobs };
    } finally {
      rmSync(dir, { recursive: true, force: true });
      Worker.ensurePool();
    }
  }
}
