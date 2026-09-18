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
import { join, resolve } from "node:path";
import { tmpdir } from "node:os";
import { readdirSync } from "node:fs";
import { createHash } from "node:crypto";
import { Store } from "../model-service/store.js";
import { CadError, requireThat } from "../semantic-ir/errors.js";
import { LIMITS, referencedArtifacts } from "../compiler/index.js";
import { hash } from "../semantic-ir/hash.js";
import { checkNativeMeshBuild } from "../compiler/native-build.js";
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
function salvage(store: Store, tenant: string, dir: string) {
  const out = join(dir, "out");
  let kept = 0;
  if (!existsSync(out)) return kept;
  for (const name of readdirSync(out)) {
    const key = /^([a-f0-9]{64})\.topology\.json$/.exec(name)?.[1];
    if (!key) continue;
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
/** The features this run actually has to open: the outputs, everything whose
 *  shape or measured facts are missing from the cache, and whatever those build
 *  on. The rest is reported from its stored facts without ever being loaded —
 *  the cache is staged in full regardless, so the decision can never leave the
 *  run without a shape it turns out to need. */
function shapesNeeded(store: Store, tenant: string, request: any) {
  const plan = request.plan;
  if (request.action === "render" || !plan?.features?.length) return null;
  const has = (key: string) =>
    !!store.get("SELECT 1 FROM cache WHERE tenant=? AND key=?", tenant, key);
  const needed = new Set<string>(plan.outputs ?? []);
  for (const f of [...plan.features].reverse()) {
    const local = f.local_cache_key ?? f.cache_key,
      complete =
        f.authoritative_representation === "brep" &&
        has(f.cache_key + ":facts") &&
        has(local) &&
        has(local + ":topology");
    // Only a feature that has to be rebuilt needs what it was built from. One
    // that comes out of the cache is simply read back, and its own inputs stay
    // where they are — otherwise a single changed screw would drag the whole
    // construction tree into the sandbox.
    if (complete) continue;
    needed.add(f.id);
    for (const dependency of f.depends_on ?? []) needed.add(dependency);
  }
  return needed;
}
/** One isolated, single-use bubblewrap process bound to its own work directory. */
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
const WARM_POOL_TARGET = Math.max(
  0,
  Math.min(2, Number(process.env.MATHFORGE_WARM_WORKERS ?? "1")),
);
export class Worker {
  private static pythonPrefix: string | null = null;
  private static pool: Sandbox[] = [];
  private static warming = 0;
  private static draining = false;
  process: ChildProcess | null = null;
  cancelled = false;
  static probe() {
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
        "Python-Basisinstallation nicht verfügbar.",
      );
      Worker.pythonPrefix = detected.stdout.trim();
      requireThat(
        Worker.pythonPrefix.startsWith("/") && Worker.pythonPrefix !== "/",
        "SANDBOX_UNAVAILABLE",
        "Ungültige Python-Basisinstallation.",
      );
    }
    return Worker.pythonPrefix;
  }
  /** Spawn the isolated process; with wait=true it imports the kernel and idles until request.json exists. */
  private static launch(dir: string, wait: boolean, script: string): Sandbox {
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
    const child = spawn("bwrap", args, {
      detached: true,
      stdio: ["ignore", "ignore", "pipe"],
      env: { PATH: "/usr/bin:/bin" },
    });
    const sandbox: Sandbox = {
      dir,
      process: child,
      errors: "",
      exited: new Promise((resolveExit) => {
        // A crash before bubblewrap finishes startup can leave its namespace
        // helper holding stderr open. Reap the dedicated group on exit too.
        child.once("exit", () => Worker.killSandbox(child));
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
    chmodSync(dir, 0o700);
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
      "Ungültiges genehmigtes Workerbudget.",
    );
    requireThat(
      expires === null ||
        (Number.isSafeInteger(expires) && expires > Date.now()),
      "ACCESS_DENIED",
      "Workerfreigabe ist abgelaufen.",
    );
    requireThat(
      Worker.probe(),
      "SANDBOX_UNAVAILABLE",
      "Der erforderliche isolierte Worker kann nicht gestartet werden.",
    );
    if (this.cancelled)
      throw new CadError("CANCELLED", "Job wurde abgebrochen.");
    const script = request.script === "preview" ? "preview" : "main";
    const warm = Worker.takeWarm(script);
    const dir = warm?.dir ?? Worker.newDirectory();
    try {
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
          ["step", "stl", "vdb"].includes(input.format),
          "OUT_OF_SCOPE",
          "Nicht registriertes Eingabeformat.",
        );
        const a = store.get(
          "SELECT * FROM artifacts WHERE id=? AND tenant=?",
          input.artifact_id,
          tenant,
        );
        requireThat(a, "ACCESS_DENIED", "Importartefakt fehlt.");
        writeFileSync(
          join(dir, "input-" + a.id + "." + input.format),
          store.readBlob(a.hash),
          { mode: 0o444 },
        );
      }
      // The request is always the last input written: a pre-warmed sandbox starts on its appearance.
      const sandbox = warm ?? Worker.launch(dir, false, script);
      this.process = sandbox.process;
      writeFileSync(
        join(dir, "request.json"),
        JSON.stringify(
          staged ? { ...request, shapes_needed: [...staged] } : request,
        ),
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
        throw new CadError("CANCELLED", "Job wurde abgebrochen.");
      if (timedOut)
        throw new CadError(
          "BUDGET_EXCEEDED",
          "Worker-Zeitbudget überschritten.",
          { salvaged: salvage(store, tenant, dir) },
        );
      if (code === -1)
        throw new CadError(
          "SANDBOX_UNAVAILABLE",
          "Worker konnte nicht gestartet werden.",
        );
      if (code !== 0)
        throw new CadError(
          "KERNEL_FAILURE",
          "Worker wurde vorzeitig beendet.",
          {
            exit_code: code,
            salvaged: salvage(store, tenant, dir),
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
        "Worker-Ergebnis fehlt oder ist zu groß.",
      );
      const result = JSON.parse(readFileSync(resultPath, "utf8"));
      if (result.status === "failed") {
        // Operator diagnostics only: native stderr may contain imported data and is never persisted.
        if (process.env.MATHFORGE_DEBUG_ERRORS)
          console.error("[worker stderr]", sandbox.errors.slice(-2000));
        throw new CadError(result.error.code, result.error.message);
      }
      let total = 0;
      const blobs: Record<string, string> = {};
      requireThat(
        lstatSync(join(dir, "out")).isDirectory() &&
          !lstatSync(join(dir, "out")).isSymbolicLink(),
        "KERNEL_FAILURE",
        "Ungültiges Worker-Ausgabeverzeichnis.",
      );
      // A preview re-emits nothing it merely read from the cache.
      const names = [
        ...(request.action === "solve_constraints" ||
        request.action === "render" ||
        staged
          ? []
          : request.plan.features
        )
          .filter((f: any) => f.authoritative_representation === "brep")
          .flatMap((f: any) =>
            [
              ...new Set<string>([
                f.cache_key,
                f.local_cache_key ?? f.cache_key,
              ]),
            ].flatMap((key) => [key + ".brep", key + ".topology.json"]),
          ),
        ...result.files,
        // Measured facts are written per feature as the run goes; they are
        // picked up from the directory rather than from the result list, so the
        // kernel does not have to announce each one.
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
          "Nicht registriertes Worker-Artefakt.",
        );
        const path = join(dir, "out", name);
        const stat = lstatSync(path);
        total += stat.size;
        requireThat(
          stat.isFile() &&
            stat.size <= LIMITS.max_artifact_bytes &&
            total <= 128 * 1024 * 1024,
          "BUDGET_EXCEEDED",
          "Artefaktbudget überschritten.",
        );
        blobs[name] = store.blob(readFileSync(path));
      }
      if (result.metrics && typeof result.metrics === "object")
        result.metrics.gateway_wall_seconds = (Date.now() - started) / 1000;
      return { ...result, blobs };
    } finally {
      rmSync(dir, { recursive: true, force: true });
      Worker.ensurePool();
    }
  }
}
