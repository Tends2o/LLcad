/** Actual Codex transport; connect/callTool never start a model turn. */
import { spawn } from "node:child_process";
import { createInterface } from "node:readline";
import { once } from "node:events";
import { resolve } from "node:path";

export class CodexHostClient {
  private child;
  private pending = new Map<
    number,
    { resolve: (v: any) => void; reject: (e: Error) => void }
  >();
  private sequence = 0;
  thread = "";
  private closed = false;
  version = "";
  constructor(
    options: {
      env?: NodeJS.ProcessEnv;
      config?: string[];
      notification?: (message: any) => void;
    } = {},
  ) {
    this.child = spawn(
      "codex",
      [
        "app-server",
        "--strict-config",
        ...(options.config ?? []).flatMap((c) => ["-c", c]),
      ],
      { stdio: ["pipe", "pipe", "ignore"], env: options.env ?? process.env },
    );
    createInterface({ input: this.child.stdout }).on("line", (line) => {
      let message;
      try {
        message = JSON.parse(line);
      } catch {
        this.fail(new Error("Invalid Codex JSON-RPC response"));
        return;
      }
      if (message.method && message.id !== undefined) {
        options.notification?.({
          method: "host/requestRejected",
          params: { method: message.method },
        });
        // A transport test must never approve unrelated actions or elicitation.
        this.child.stdin.write(
          JSON.stringify({
            id: message.id,
            error: {
              code: -32601,
              message: "Unsupported host request in transport check",
            },
          }) + "\n",
        );
        return;
      }
      if (message.method) {
        options.notification?.(message);
        return;
      }
      const pending = this.pending.get(message.id);
      if (!pending) return;
      this.pending.delete(message.id);
      if (message.error) pending.reject(new Error(message.error.message));
      else pending.resolve(message.result);
    });
    this.child.on("error", (error) => this.fail(error));
    this.child.on("exit", () => {
      this.closed = true;
      this.fail(new Error("Codex app-server exited"));
    });
    this.child.stdin.on("error", (error) => this.fail(error));
  }
  private fail(error: Error) {
    for (const pending of this.pending.values()) pending.reject(error);
    this.pending.clear();
  }
  request(method: string, params: any): Promise<any> {
    if (this.closed)
      return Promise.reject(new Error("Codex app-server is closed"));
    const id = ++this.sequence;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`Codex request timed out: ${method}`));
      }, 90000);
      this.pending.set(id, {
        resolve: (v) => {
          clearTimeout(timer);
          resolve(v);
        },
        reject: (e) => {
          clearTimeout(timer);
          reject(e);
        },
      });
      this.child.stdin.write(JSON.stringify({ id, method, params }) + "\n");
    });
  }
  async connect(threadOptions: Record<string, unknown> = {}) {
    const info = await this.request("initialize", {
      clientInfo: { name: "llcad-host-check", version: "1" },
      capabilities: { experimentalApi: true },
    });
    this.version = info.userAgent;
    this.child.stdin.write(JSON.stringify({ method: "initialized" }) + "\n");
    const started = await this.request("thread/start", {
      cwd: resolve("."),
      ephemeral: true,
      ...threadOptions,
    });
    this.thread = started.thread.id;
    return this.inventory();
  }
  async inventory() {
    let cursor;
    do {
      const page = await this.request("mcpServerStatus/list", {
        threadId: this.thread,
        cursor,
      });
      const server = page.data.find((s: any) => s.name === "llcad");
      if (server) return server;
      cursor = page.nextCursor;
    } while (cursor);
    throw new Error("LLcad is not configured in this Codex host");
  }
  callTool(name: string, args: any = {}) {
    return this.request("mcpServer/tool/call", {
      threadId: this.thread,
      server: "llcad",
      tool: name,
      arguments: args,
    });
  }
  async close() {
    if (this.closed) return;
    const exited = once(this.child, "exit");
    this.child.kill("SIGTERM");
    const timer = setTimeout(() => this.child.kill("SIGKILL"), 5000);
    try {
      await exited;
    } finally {
      clearTimeout(timer);
    }
  }
}
