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
import { join, resolve } from "node:path";
import { tmpdir } from "node:os";
import { Store } from "../model-service/store.js";
import { CadError, requireThat } from "../semantic-ir/errors.js";
import { LIMITS } from "../compiler/index.js";
import { hash } from "../semantic-ir/hash.js";
export const PROJECT_ROOT = resolve(
  process.env.MATHFORGE_ROOT ?? process.cwd(),
);
export class Worker {
  private static pythonPrefix: string | null = null;
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
    if (!child?.pid) return;
    try {
      process.kill(-child.pid, "SIGKILL");
    } catch (error: any) {
      if (error.code !== "ESRCH") child.kill("SIGKILL");
    }
  }
  async run(store: Store, tenant: string, request: any) {
    requireThat(
      Worker.probe(),
      "SANDBOX_UNAVAILABLE",
      "Der erforderliche isolierte Worker kann nicht gestartet werden.",
    );
    const dir = mkdtempSync(join(tmpdir(), "mathforge-"));
    chmodSync(dir, 0o700);
    mkdirSync(join(dir, "out"), { mode: 0o700 });
    mkdirSync(join(dir, "cache"), { mode: 0o755 });
    try {
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
        const cached = store.get(
          "SELECT blob FROM cache WHERE tenant=? AND key=?",
          tenant,
          f.cache_key,
        );
        if (cached)
          writeFileSync(
            join(dir, "cache", f.cache_key + ".brep"),
            store.readBlob(cached.blob),
            { mode: 0o444 },
          );
        const topology = store.get(
          "SELECT blob FROM cache WHERE tenant=? AND key=?",
          tenant,
          f.cache_key + ":topology",
        );
        if (topology)
          writeFileSync(
            join(dir, "cache", f.cache_key + ".topology.json"),
            store.readBlob(topology.blob),
            { mode: 0o444 },
          );
        if (f.construction.operator === "imported") {
          const a = store.get(
            "SELECT * FROM artifacts WHERE id=? AND tenant=?",
            f.construction.artifact_id,
            tenant,
          );
          requireThat(a, "ACCESS_DENIED", "Importartefakt fehlt.");
          writeFileSync(
            join(dir, "input-" + a.id + "." + f.construction.format),
            store.readBlob(a.hash),
            { mode: 0o444 },
          );
        }
      }
      writeFileSync(join(dir, "request.json"), JSON.stringify(request), {
        mode: 0o444,
      });
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
        ...(Worker.pythonPrefix === "/usr"
          ? []
          : ["--ro-bind", Worker.pythonPrefix, Worker.pythonPrefix]),
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
        "/worker/main.py",
      ];
      await new Promise<void>((resolvePromise, reject) => {
        if (this.cancelled) {
          reject(new CadError("CANCELLED", "Job wurde abgebrochen."));
          return;
        }
        this.process = spawn("bwrap", args, {
          detached: true,
          stdio: ["ignore", "ignore", "pipe"],
          env: { PATH: "/usr/bin:/bin" },
        });
        const child = this.process;
        // A crash before bubblewrap finishes startup can leave its namespace
        // helper holding stderr open. Reap the dedicated group on exit too,
        // otherwise Node's "close" event and the whole job may never finish.
        child.once("exit", () => this.killGroup(child));
        let errors = "",
          timedOut = false;
        const timer = setTimeout(() => {
          timedOut = true;
          this.killGroup(child);
        }, LIMITS.job_seconds * 1000);
        this.process.stderr?.on("data", (chunk) => {
          if (errors.length < 4096) errors += chunk.toString();
        });
        this.process.on("error", () => {
          clearTimeout(timer);
          reject(
            new CadError(
              "SANDBOX_UNAVAILABLE",
              "Worker konnte nicht gestartet werden.",
            ),
          );
        });
        this.process.on("close", (code) => {
          clearTimeout(timer);
          this.process = null;
          if (this.cancelled)
            reject(new CadError("CANCELLED", "Job wurde abgebrochen."));
          else if (timedOut)
            reject(
              new CadError(
                "BUDGET_EXCEEDED",
                "Worker-Zeitbudget überschritten.",
              ),
            );
          else if (code !== 0)
            reject(
              new CadError(
                "KERNEL_FAILURE",
                "Worker wurde vorzeitig beendet.",
                { exit_code: code },
              ),
            );
          else resolvePromise();
          // Native stderr is deliberately not persisted: it may contain imported data.
        });
      });
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
      if (result.status === "failed")
        throw new CadError(result.error.code, result.error.message);
      let total = 0;
      const blobs: Record<string, string> = {};
      requireThat(
        lstatSync(join(dir, "out")).isDirectory() &&
          !lstatSync(join(dir, "out")).isSymbolicLink(),
        "KERNEL_FAILURE",
        "Ungültiges Worker-Ausgabeverzeichnis.",
      );
      const names = [
        ...(request.action === "solve_constraints" ? [] : request.plan.features)
          .filter((f: any) => f.construction.operator !== "field")
          .flatMap((f: any) => [
            f.cache_key + ".brep",
            f.cache_key + ".topology.json",
          ]),
        ...result.files,
      ];
      for (const name of new Set<string>(names)) {
        requireThat(
          /^(model\.(brep|step|stl|vdb)|preview\.json|roundtrip\.json|[a-f0-9]{64}\.(brep|topology\.json|field\.json))$/.test(
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
      return { ...result, blobs };
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  }
}
