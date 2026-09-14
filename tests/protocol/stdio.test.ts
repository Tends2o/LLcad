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
        assert.equal((await client.listTools()).tools.length, 21);
        if (pass === 0) {
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
