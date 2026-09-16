import test from "node:test";
import assert from "node:assert/strict";
import { once } from "node:events";
import { createServer } from "node:net";
import { request as httpRequest } from "node:http";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";
import { setup, principal, importFixture } from "../helpers.js";
import { sphere } from "../../scripts/fixtures.js";
import { createApp } from "../../packages/mcp-gateway/app.js";
import { SharedViewer } from "../../packages/mcp-gateway/viewer-host.js";
const TOKEN = "protocol-test-" + "x".repeat(32);
async function server() {
  const env = setup();
  const probe = createServer();
  probe.listen(0, "127.0.0.1");
  await once(probe, "listening");
  const port = (probe.address() as any).port;
  await new Promise<void>((r) => probe.close(() => r()));
  const url = `http://127.0.0.1:${port}`;
  const { app, issueBootstrapCode } = createApp(env.service, {
    mode: "local",
    publicURL: url,
    dataRoot: env.dir,
    localToken: TOKEN,
  });
  const http = app.listen(port, "127.0.0.1");
  return {
    ...env,
    url,
    issueBootstrapCode,
    async stop() {
      http.closeAllConnections();
      await new Promise<void>((r) => http.close(() => r()));
      await env.close();
    },
  };
}
const meta = {
  "io.modelcontextprotocol/protocolVersion": "2026-07-28",
  "io.modelcontextprotocol/clientCapabilities": {},
  "io.modelcontextprotocol/clientInfo": {
    name: "conformance-test",
    version: "1",
  },
};
async function rpc(
  url: string,
  method: string,
  params: any = {},
  overrides: Record<string, string> = {},
) {
  return fetch(url + "/mcp", {
    method: "POST",
    headers: {
      Authorization: `Bearer ${TOKEN}`,
      "Content-Type": "application/json",
      Accept: "application/json, text/event-stream",
      "Mcp-Protocol-Version": "2026-07-28",
      "Mcp-Method": method,
      ...(params.name || params.uri
        ? { "Mcp-Name": params.name ?? params.uri }
        : {}),
      ...overrides,
    },
    body: JSON.stringify({
      jsonrpc: "2.0",
      id: 1,
      method,
      params: { _meta: meta, ...params },
    }),
  });
}
test("2026 discovery, tools, metadata, header mismatch, unknown version and unknown method", async () => {
  const env = await server();
  try {
    let response = await rpc(env.url, "server/discover");
    assert.equal(response.status, 200);
    let json = await response.json();
    assert.deepEqual(json.result.supportedVersions, [
      "2026-07-28",
      "2025-11-25",
      "2025-06-18",
      "2025-03-26",
    ]);
    assert.equal(json.result.resultType, "complete");
    assert.equal(
      json.result._meta["io.modelcontextprotocol/serverInfo"].name,
      "mathforge-3d",
    );
    response = await rpc(env.url, "tools/list");
    json = await response.json();
    assert.equal(json.result.tools.length, 25);
    assert.equal(
      json.result.tools.find((t: any) => t.name === "cad_render").annotations
        .readOnlyHint,
      false,
    );
    response = await rpc(env.url, "tools/call", {
      name: "cad_capabilities",
      arguments: {},
    });
    assert.equal(
      (await response.json()).result.structuredContent.application_version,
      "0.1.0",
    );
    response = await rpc(
      env.url,
      "tools/list",
      {},
      { "Mcp-Method": "cad_commit" },
    );
    assert.equal(response.status, 400);
    assert.equal((await response.json()).error.code, -32020);
    response = await rpc(
      env.url,
      "tools/list",
      {},
      { "Mcp-Protocol-Version": "1900-01-01" },
    );
    assert.equal((await response.json()).error.code, -32022);
    response = await rpc(env.url, "initialize");
    assert.equal(response.status, 404);
    assert.equal((await response.json()).error.code, -32601);
    response = await rpc(env.url, "tools/list", {
      _meta: { "io.modelcontextprotocol/protocolVersion": "2026-07-28" },
    });
    assert.equal(response.status, 400);
  } finally {
    await env.stop();
  }
});
test("official legacy SDK initialization, tool schemas and structured result", async () => {
  const env = await server();
  const client = new Client({ name: "legacy-test", version: "1" });
  try {
    await client.connect(
      new StreamableHTTPClientTransport(new URL(env.url + "/mcp"), {
        requestInit: { headers: { Authorization: `Bearer ${TOKEN}` } },
      }),
    );
    const tools = await client.listTools();
    assert.equal(tools.tools.length, 25);
    const result = await client.callTool({
      name: "cad_capabilities",
      arguments: {},
    });
    assert.equal((result.structuredContent as any).ir_schema_version, "1");
    const model = await importFixture(env.service, sphere, {
      ...principal,
      tenant: "local",
      user: "local-user",
    });
    const inspected = await client.callTool({
      name: "cad_inspect",
      arguments: { model_id: model.model_id, feature_id: "sphere" },
    });
    assert.equal(
      (inspected.structuredContent as any).face_page.faces[0].center.length,
      3,
    );
    const measured = await client.callTool({
      name: "cad_measure",
      arguments: { model_id: model.model_id, metric: "bounds" },
    });
    assert.equal((measured.structuredContent as any).measurements.length, 6);
    const denied = await client.callTool({
      name: "cad_get_model",
      arguments: { model_id: "unavailable" },
    });
    assert.equal(denied.isError, true);
    assert.equal((denied.structuredContent as any).committed, false);
  } finally {
    await client.close();
    await env.stop();
  }
});
test("viewer tools mint a single-use login code and report the shared HTTP service", async () => {
  const env = await server();
  try {
    env.service.viewer = new SharedViewer(env.url, env.issueBootstrapCode);
    const opened = await env.service.call(principal, "cad_viewer_open", {
      launch_browser: false,
    });
    assert.equal(opened.status, "ok");
    assert.equal(opened.running, true);
    assert.equal(opened.transport, "http");
    assert.equal(opened.browser_launched, false);
    const code = /code=([^&]+)/.exec(new URL(opened.url).hash)?.[1];
    assert.ok(code);
    const login = () =>
      fetch(env.url + "/api/session", {
        method: "POST",
        headers: { "Content-Type": "application/json", Origin: env.url },
        body: JSON.stringify({ code }),
      });
    const first = await login();
    assert.equal(first.status, 200);
    assert.match(first.headers.get("set-cookie") ?? "", /mathforge_session=/);
    assert.equal((await login()).status, 401, "a code is single-use");
    const closed = await env.service.call(principal, "cad_viewer_close", {});
    assert.equal(closed.status, "ok");
    assert.equal(closed.running, true);
    const unknown = await env.service.call(principal, "cad_viewer_open", {
      model_id: "model-does-not-exist",
      launch_browser: false,
    });
    assert.equal(unknown.status, "failed");
  } finally {
    await env.stop();
  }
});
test("HTTP negotiates supported protocol versions and offers a tested fallback", async () => {
  const env = await server();
  try {
    for (const requested of [
      "2025-03-26",
      "2025-06-18",
      "2025-11-25",
      "2099-01-01",
      "2024-11-05",
    ]) {
      const headers = {
        Authorization: `Bearer ${TOKEN}`,
        "Content-Type": "application/json",
        Accept: "application/json, text/event-stream",
      };
      const send = (method: string, params: any, version?: string) =>
        fetch(env.url + "/mcp", {
          method: "POST",
          headers: {
            ...headers,
            ...(version ? { "MCP-Protocol-Version": version } : {}),
          },
          body: JSON.stringify({ jsonrpc: "2.0", id: 1, method, params }),
        });
      const init = await send("initialize", {
        protocolVersion: requested,
        capabilities: {},
        clientInfo: { name: "host-compatibility", version: "1" },
      });
      assert.equal(init.status, 200);
      const negotiated = (await init.json()).result.protocolVersion;
      assert.equal(
        negotiated,
        requested.startsWith("2025-") ? requested : "2025-11-25",
      );
      const result = await send(
        "tools/call",
        { name: "cad_list_models", arguments: {} },
        negotiated,
      );
      assert.equal(result.status, 200);
      assert.equal((await result.json()).result.structuredContent.total, 0);
      const invalid = await send(
        "tools/call",
        { name: "cad_list_models", arguments: {} },
        "2099-01-01",
      );
      assert.equal(invalid.status, 400);
    }
  } finally {
    await env.stop();
  }
});
test("HTTP authentication, DNS rebinding, origin validation, cookie CSRF and schema injection", async () => {
  const env = await server();
  try {
    let response = await fetch(env.url + "/api/models");
    assert.equal(response.status, 401);
    assert.ok(
      response.headers.get("www-authenticate")?.includes("resource_metadata"),
    );
    const badHostStatus = await new Promise<number | undefined>(
      (resolve, reject) => {
        const req = httpRequest(
          env.url + "/healthz",
          { headers: { Host: "evil.example" } },
          (r) => {
            r.resume();
            resolve(r.statusCode);
          },
        );
        req.on("error", reject);
        req.end();
      },
    );
    assert.equal(badHostStatus, 403);
    response = await fetch(env.url + "/api/models", {
      headers: {
        Authorization: `Bearer ${TOKEN}`,
        Origin: "https://evil.example",
      },
    });
    assert.equal(response.status, 403);
    response = await fetch(env.url + "/api/tools/cad_capabilities", {
      method: "POST",
      headers: {
        Cookie: `mathforge_session=${TOKEN}`,
        "Content-Type": "application/json",
      },
      body: "{}",
    });
    assert.equal(response.status, 401);
    response = await rpc(env.url, "tools/call", {
      name: "cad_create_model",
      arguments: {
        name: "injection",
        tenant: "evil",
        idempotency_key: "a".repeat(16),
      },
    });
    const json = await response.json();
    assert.equal(json.result.isError, true);
    assert.equal(
      json.result.structuredContent.errors[0].code,
      "INVALID_SCHEMA",
    );
    response = await fetch(env.url + "/mcp", {
      method: "POST",
      headers: {
        Authorization: `Bearer ${TOKEN}`,
        "Content-Type": "application/json",
      },
      body: "{",
    });
    assert.equal(response.status, 400);
  } finally {
    await env.stop();
  }
});
