#!/usr/bin/env node
// LLcad on demand: an MCP stdio server for Claude Code that forwards every message to
// the local HTTP service and starts that service only when a client really needs it.
//
//   node bin/llcad-bridge.mjs            stdio bridge (registered with `claude mcp add`)
//   node bin/llcad-bridge.mjs start      start the service if it is not running
//   node bin/llcad-bridge.mjs viewer [model-id]
//                                        start the service and open the viewer in the browser
//   node bin/llcad-bridge.mjs status     whether the service runs
//   node bin/llcad-bridge.mjs stop       stop the service
//
// Idle cost: Claude Code keeps only this small process per session. `initialize` and the
// list calls are answered from a cache of the last real answers (keyed by the build), so a
// session that never models never starts the service. The service ends itself after
// LLCAD_IDLE_EXIT_MINUTES without requests (main.ts); the next call starts it again, which
// is invisible to the client because MCP over HTTP is stateless here.
//
// Configuration: KEY=VALUE lines in llcad.env next to the LLcad folder (LLCAD_ENV_FILE
// overrides), written by install.ps1.
import { spawn, spawnSync } from "node:child_process";
import {
  appendFileSync,
  existsSync,
  mkdirSync,
  openSync,
  readFileSync,
  renameSync,
  statSync,
  writeFileSync,
} from "node:fs";
import { request } from "node:http";
import { dirname, join, resolve } from "node:path";
import { createInterface } from "node:readline";
import { fileURLToPath } from "node:url";
import { createHash } from "node:crypto";

const ROOT = resolve(fileURLToPath(new URL("..", import.meta.url)));
const ENV_FILE = process.env.LLCAD_ENV_FILE ?? join(ROOT, "..", "llcad.env");
const SERVER_ENTRY = join(ROOT, "dist", "packages", "mcp-gateway", "main.js");

function readEnvFile(path) {
  const values = {};
  if (!existsSync(path)) return values;
  for (const line of readFileSync(path, "utf8").split(/\r?\n/)) {
    const match = /^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$/.exec(line);
    if (match) values[match[1]] = match[2].trim();
  }
  return values;
}

const config = readEnvFile(ENV_FILE);
const PORT = Number(config.PORT ?? 4310);
const HOST = "127.0.0.1";
const BASE = `http://${HOST}:${PORT}`;
const DATA = resolve(config.MATHFORGE_DATA ?? join(ROOT, "data"));
const LOG_DIR = join(DATA, "logs");
const log = (...parts) => process.stderr.write(`[llcad-bridge] ${parts.join(" ")}\n`);

function token() {
  return readFileSync(join(DATA, "local-token"), "utf8").trim();
}
// A feature worker under agy runs with a short-lived token of its own: the service sets
// LLCAD_MCP_TOKEN for that one process. MCP calls then carry it instead of the local key,
// and the tool-list cache stays out of it (a narrower grant must not answer for others).
const WORKER_TOKEN = /^[A-Za-z0-9_-]{32,128}$/.test(process.env.LLCAD_MCP_TOKEN ?? "")
  ? process.env.LLCAD_MCP_TOKEN
  : null;
const mcpToken = () => WORKER_TOKEN ?? token();

// ------------------------------------------------------------------ service lifecycle

function httpJSON(method, path, body, { timeout = 2000, auth = false } = {}) {
  return new Promise((resolveRequest, reject) => {
    const payload = body === undefined ? undefined : JSON.stringify(body);
    const req = request(
      {
        host: HOST,
        port: PORT,
        method,
        path,
        timeout,
        headers: {
          ...(payload ? { "Content-Type": "application/json" } : {}),
          ...(auth ? { Authorization: "Bearer " + token() } : {}),
        },
      },
      (res) => {
        const chunks = [];
        res.on("data", (c) => chunks.push(c));
        res.on("end", () => {
          const text = Buffer.concat(chunks).toString("utf8");
          try {
            resolveRequest({ status: res.statusCode, body: text ? JSON.parse(text) : null });
          } catch {
            resolveRequest({ status: res.statusCode, body: text });
          }
        });
      },
    );
    req.on("timeout", () => req.destroy(new Error("timeout")));
    req.on("error", reject);
    req.end(payload);
  });
}

async function healthy() {
  try {
    return (await httpJSON("GET", "/healthz", undefined, { timeout: 1500 })).status === 200;
  } catch {
    return false;
  }
}

/** A clean environment for the service: system variables plus llcad.env, never the
 *  calling client's own (a Claude Code session must not leak into viewer agent runs). */
function serviceEnvironment() {
  const keep = /^(SystemRoot|SystemDrive|windir|ComSpec|PATHEXT|OS|PROCESSOR_.*|NUMBER_OF_PROCESSORS|USERPROFILE|USERNAME|USERDOMAIN|HOMEDRIVE|HOMEPATH|HOME|APPDATA|LOCALAPPDATA|ProgramData|ProgramFiles.*|CommonProgram.*|PUBLIC|TEMP|TMP|LANG|LC_.*|PATH|Path)$/i;
  const env = {};
  for (const [key, value] of Object.entries(process.env))
    if (keep.test(key) && value !== undefined) env[key] = value;
  const path = env.PATH ?? env.Path ?? "";
  delete env.Path;
  Object.assign(env, config);
  env.PATH = [dirname(process.execPath), config.PATH, path].filter(Boolean).join(process.platform === "win32" ? ";" : ":");
  env.MATHFORGE_ROOT ??= ROOT;
  env.MATHFORGE_DATA ??= DATA;
  env.HOST = HOST;
  env.PORT = String(PORT);
  return env;
}

function openLog() {
  mkdirSync(LOG_DIR, { recursive: true });
  const file = join(LOG_DIR, "server.log");
  try {
    if (statSync(file).size > 5 * 1024 * 1024) renameSync(file, file + ".1");
  } catch {
    /* no log yet */
  }
  return openSync(file, "a");
}

let starting = null;
/** Start the service unless it answers already; resolves once /healthz is up. */
function ensureService() {
  starting ??= (async () => {
    try {
      if (await healthy()) return;
      if (!existsSync(SERVER_ENTRY))
        throw new Error(`LLcad is not built (${SERVER_ENTRY} missing); run install.ps1.`);
      log("starting LLcad service on", BASE);
      const out = openLog();
      const child = spawn(process.execPath, [SERVER_ENTRY], {
        cwd: ROOT,
        env: serviceEnvironment(),
        detached: true,
        windowsHide: true,
        stdio: ["ignore", out, out],
      });
      child.unref();
      let exitedAt = 0;
      child.once("exit", () => (exitedAt = Date.now()));
      const deadline = Date.now() + 60_000;
      while (Date.now() < deadline) {
        await new Promise((r) => setTimeout(r, 250));
        if (await healthy()) return;
        // A starter that lost the race for the data folder exits at once while the
        // winner comes up; give that one a moment, then report the failure.
        if (exitedAt && Date.now() - exitedAt > 5000) break;
      }
      let tail = "";
      try {
        tail = readFileSync(join(LOG_DIR, "server.log"), "utf8").trim().split(/\r?\n/).slice(-6).join(" | ");
      } catch {
        /* no log */
      }
      throw new Error(`LLcad service did not come up (${join(LOG_DIR, "server.log")}): ${tail}`);
    } finally {
      starting = null;
    }
  })();
  return starting;
}

function servicePid() {
  try {
    return Number(readFileSync(join(DATA, ".store.owner"), "utf8").trim()) || null;
  } catch {
    return null;
  }
}

// ------------------------------------------------------------------ MCP forwarding

/** Version of the build the cache belongs to: the gateway bundle and the package lock. */
function buildStamp() {
  const h = createHash("sha256");
  for (const file of [SERVER_ENTRY, join(ROOT, "dist", "packages", "model-service", "index.js"), join(ROOT, "package-lock.json")])
    try {
      const s = statSync(file);
      h.update(file + ":" + s.size + ":" + s.mtimeMs + "\n");
    } catch {
      h.update(file + ":missing\n");
    }
  return h.digest("hex").slice(0, 16);
}
const CACHE_FILE = join(DATA, "mcp-cache.json");
const CACHEABLE = new Set(["server/discover", "tools/list", "resources/list", "resources/templates/list", "prompts/list"]);
let cache = null;
function loadCache() {
  if (cache) return cache;
  try {
    const stored = JSON.parse(readFileSync(CACHE_FILE, "utf8"));
    cache = stored.build === buildStamp() ? stored : { build: buildStamp(), entries: {} };
  } catch {
    cache = { build: buildStamp(), entries: {} };
  }
  return cache;
}
function cacheKey(message) {
  if (message.method === "initialize") return "initialize:" + (message.params?.protocolVersion ?? "");
  if (CACHEABLE.has(message.method) && !message.params?.cursor) return message.method + ":" + (versionOf(message) ?? "");
  return null;
}
function remember(key, result) {
  const c = loadCache();
  c.entries[key] = result;
  try {
    mkdirSync(DATA, { recursive: true });
    writeFileSync(CACHE_FILE, JSON.stringify(c));
  } catch {
    /* the cache is only a convenience */
  }
}

let protocolVersion = null;
/** The protocol version a message travels under: per request in `_meta` (2026-07-28 and
 *  later, where stdio carries no headers), otherwise the one `initialize` negotiated. */
const META_VERSION = "io.modelcontextprotocol/protocolVersion";
function versionOf(message) {
  return message.params?._meta?.[META_VERSION] ?? protocolVersion;
}
/** Header value for a name: plain printable ASCII, else the =?base64?...?= form. */
function headerName(value) {
  if (typeof value !== "string") return undefined;
  return /^[\x20-\x7e]+$/.test(value) && value.trim() === value
    ? value
    : "=?base64?" + Buffer.from(value, "utf8").toString("base64") + "?=";
}
const write = (message) => process.stdout.write(JSON.stringify(message) + "\n");

/** POST one JSON-RPC message to /mcp and hand every answer (JSON or SSE) to `deliver`. */
function postMCP(message, deliver) {
  return new Promise((resolvePost, reject) => {
    const payload = Buffer.from(JSON.stringify(message));
    // The same headers a Streamable HTTP client sends: the version, and for the
    // per-request protocol also the method and the addressed tool or resource.
    const version = versionOf(message);
    const perRequest = message.params?._meta?.[META_VERSION] !== undefined;
    const name = ["tools/call", "prompts/get"].includes(message.method)
      ? headerName(message.params?.name)
      : message.method === "resources/read"
        ? headerName(message.params?.uri)
        : undefined;
    const req = request(
      {
        host: HOST,
        port: PORT,
        method: "POST",
        path: "/mcp",
        headers: {
          Authorization: "Bearer " + mcpToken(),
          "Content-Type": "application/json",
          Accept: "application/json, text/event-stream",
          "Content-Length": payload.length,
          ...(version ? { "mcp-protocol-version": version } : {}),
          ...(perRequest && message.method ? { "mcp-method": message.method } : {}),
          ...(perRequest && name ? { "mcp-name": name } : {}),
        },
      },
      (res) => {
        const type = res.headers["content-type"] ?? "";
        let buffer = "";
        res.setEncoding("utf8");
        const flushEvent = (block) => {
          const data = block
            .split(/\r?\n/)
            .filter((l) => l.startsWith("data:"))
            .map((l) => l.slice(5).replace(/^ /, ""))
            .join("\n");
          if (data) deliver(JSON.parse(data));
        };
        res.on("data", (chunk) => {
          buffer += chunk;
          if (!type.includes("text/event-stream")) return;
          let index;
          while ((index = buffer.search(/\r?\n\r?\n/)) >= 0) {
            const block = buffer.slice(0, index);
            buffer = buffer.slice(buffer.slice(index).match(/^\r?\n\r?\n/)[0].length + index);
            flushEvent(block);
          }
        });
        res.on("end", () => {
          try {
            if (type.includes("text/event-stream")) {
              if (buffer.trim()) flushEvent(buffer);
            } else if (buffer.trim()) {
              const body = JSON.parse(buffer);
              for (const item of Array.isArray(body) ? body : [body]) deliver(item);
            }
            resolvePost(res.statusCode);
          } catch (error) {
            reject(error);
          }
        });
        res.on("error", reject);
      },
    );
    req.on("error", reject);
    req.end(payload);
  });
}

/** Diagnosis: with data/logs/bridge-debug.on present, every incoming method is logged. */
const DEBUG = existsSync(join(LOG_DIR, "bridge-debug.on"));
function debug(message, how) {
  if (!DEBUG) return;
  try {
    appendFileSync(
      join(LOG_DIR, "bridge-debug.log"),
      `${new Date().toISOString()} ${how} ${message.method ?? "(answer)"} id=${message.id ?? "-"} ${JSON.stringify(message.params ?? {}).slice(0, 160)}\n`,
    );
  } catch {
    /* diagnosis only */
  }
}

async function forward(message) {
  const isRequest = message.id !== undefined && message.method !== undefined;
  const key = isRequest && !WORKER_TOKEN ? cacheKey(message) : null;
  // Answer from the cache while the service sleeps; never start it just to list tools.
  if (key && loadCache().entries[key] !== undefined && !(await healthyQuick())) {
    if (message.method === "initialize") protocolVersion = loadCache().entries[key].protocolVersion ?? null;
    write({ jsonrpc: "2.0", id: message.id, result: loadCache().entries[key] });
    debug(message, "cache");
    return;
  }
  // Notifications for a sleeping service carry nothing it needs (it is stateless).
  if (!isRequest && message.method?.startsWith("notifications/") && !(await healthyQuick())) {
    debug(message, "dropped");
    return;
  }
  debug(message, "forward");
  for (let attempt = 0; ; attempt++) {
    try {
      await ensureService();
      await postMCP(message, (answer) => {
        if (isRequest && answer.id === message.id && answer.result !== undefined) {
          if (message.method === "initialize") protocolVersion = answer.result.protocolVersion ?? protocolVersion;
          if (key) remember(key, answer.result);
        }
        write(answer);
      });
      return;
    } catch (error) {
      // The service may have just ended itself for idleness: start it once more.
      if (attempt === 0 && ["ECONNREFUSED", "ECONNRESET"].includes(error?.code)) continue;
      log("forwarding failed:", error?.message ?? String(error));
      if (isRequest)
        write({
          jsonrpc: "2.0",
          id: message.id,
          error: { code: -32603, message: "LLcad service unavailable: " + (error?.message ?? String(error)) },
        });
      return;
    }
  }
}

let quick = { at: 0, value: false };
async function healthyQuick() {
  if (Date.now() - quick.at < 2000) return quick.value;
  quick = { at: Date.now(), value: await healthy() };
  return quick.value;
}

function bridge() {
  const lines = createInterface({ input: process.stdin, crlfDelay: Infinity });
  lines.on("line", (line) => {
    if (!line.trim()) return;
    let message;
    try {
      message = JSON.parse(line);
    } catch {
      write({ jsonrpc: "2.0", id: null, error: { code: -32700, message: "Parse error" } });
      return;
    }
    for (const item of Array.isArray(message) ? message : [message]) void forward(item);
  });
  lines.on("close", () => process.exit(0));
}

// ------------------------------------------------------------------ commands

async function main() {
  const [command, ...rest] = process.argv.slice(2);
  switch (command) {
    case undefined:
      return bridge();
    case "start":
      await ensureService();
      console.log(`LLcad is running: ${BASE}`);
      return;
    case "viewer": {
      await ensureService();
      const args = { launch_browser: true, ...(rest[0] ? { model_id: rest[0] } : {}) };
      const { status, body } = await httpJSON("POST", "/api/tools/cad_viewer_open", args, { timeout: 30000, auth: true });
      const result = body?.result ?? body;
      const url = result?.url ?? body?.url;
      if (status !== 200 || !url) {
        console.error("The viewer could not be opened:", JSON.stringify(body));
        process.exit(1);
      }
      console.log(result?.browser_launched ? "Viewer opened in the browser." : "Viewer URL (single use, valid for 5 min):");
      console.log(url);
      return;
    }
    case "status": {
      const up = await healthy();
      console.log(up ? `LLcad is running: ${BASE} (PID ${servicePid() ?? "?"})` : "LLcad is not running.");
      process.exitCode = up ? 0 : 3;
      return;
    }
    case "stop": {
      const pid = servicePid();
      if (!pid || !(await healthy())) {
        console.log("LLcad is not running.");
        return;
      }
      if (process.platform === "win32") spawnSync("taskkill", ["/PID", String(pid), "/T", "/F"], { stdio: "ignore", windowsHide: true });
      else process.kill(pid, "SIGTERM");
      console.log(`LLcad stopped (PID ${pid}).`);
      return;
    }
    default:
      console.error("Usage: llcad-bridge.mjs [start|viewer [model-id]|status|stop]");
      process.exit(2);
  }
}

main().catch((error) => {
  log(error?.message ?? String(error));
  process.exit(1);
});
