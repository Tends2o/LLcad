import test from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";
import { id } from "../../packages/semantic-ir/hash.js";

test("local MCP starts without an HTTP service, browser login or token prompt and keeps models after reconnect", async () => {
  const directory = mkdtempSync(join(tmpdir(), "llcad-stdio-"));
  let model: string | undefined;
  try {
    for (let pass = 0; pass < 2; pass++) {
      const client = new Client({ name: "local-host-test", version: "1" });
      const transport = new StdioClientTransport({
        command: process.execPath,
        args: ["--import", "tsx", "packages/mcp-gateway/stdio.ts"],
        cwd: process.cwd(),
        env: {
          ...Object.fromEntries(
            Object.entries(process.env).filter(
              (entry): entry is [string, string] => entry[1] !== undefined,
            ),
          ),
          MATHFORGE_DATA: directory,
          MATHFORGE_VIEWER_PORT: "0",
        },
        stderr: "pipe",
      });
      let stderr = "";
      transport.stderr?.on("data", (chunk) => {
        stderr += chunk;
      });
      try {
        await client.connect(transport);
        assert.match(
          client.getInstructions() ?? "",
          /never require the user to click/i,
        );
        assert.equal((await client.listTools()).tools.length, 25);
        if (pass === 0) {
          // The viewer is optional and starts on demand on the loopback interface.
          const opened = (
            await client.callTool({
              name: "cad_viewer_open",
              arguments: { launch_browser: false },
            })
          ).structuredContent as any;
          assert.equal(opened.status, "ok");
          assert.equal(opened.transport, "stdio");
          assert.equal(opened.running, true);
          assert.match(opened.url, /^http:\/\/127\.0\.0\.1:\d+\/#code=/);
          const health = await fetch(new URL("/healthz", opened.url));
          assert.equal(health.status, 200);
          const closed = (
            await client.callTool({ name: "cad_viewer_close", arguments: {} })
          ).structuredContent as any;
          assert.equal(closed.running, false);
          const gone = await fetch(new URL("/healthz", opened.url)).catch(
            () => null,
          );
          assert.equal(gone, null, "the viewer listener is closed");
          const created = await client.callTool({
            name: "cad_create_model",
            arguments: {
              name: "LLcad ohne Klicks",
              idempotency_key: id("create"),
            },
          });
          assert.equal(
            created.isError ?? false,
            false,
            JSON.stringify(created),
          );
          model = (created.structuredContent as any).model_id;
        }
        const listed = await client.callTool({
          name: "cad_list_models",
          arguments: {},
        });
        assert.equal(
          (listed.structuredContent as any).models[0].model_id,
          model,
        );
      } finally {
        await client.close();
      }
      assert.equal(
        /STORE_BUSY|UnhandledPromiseRejection|TypeError/.test(stderr),
        false,
        stderr,
      );
    }
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});
