/** A complete controller workflow over MCP. No viewer or direct service calls. */
import assert from "node:assert/strict";
import { createServer } from "node:http";
import { once } from "node:events";
import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { randomBytes } from "node:crypto";
import { setTimeout as delay } from "node:timers/promises";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";
import { ModelService } from "../packages/model-service/index.js";
import { createApp } from "../packages/mcp-gateway/app.js";
import { id } from "../packages/semantic-ir/hash.js";
import { REGISTRY_HASH } from "../packages/compiler/index.js";
import { IMPLEMENTATION_HASH } from "../packages/compiler/build.js";
import { housing } from "./fixtures.js";

const directory = mkdtempSync(join(tmpdir(), "llcad-mcp-workflow-"));
const service = new ModelService(directory);
const server = createServer();
const client = new Client({ name: "llcad-headless-controller", version: "1" });
const token = randomBytes(32).toString("hex");
const calls: string[] = [];
mkdirSync("reports", { recursive: true });
async function tool(name: string, args: any = {}): Promise<any> {
  calls.push(name);
  const result = await client.callTool({ name, arguments: args });
  assert.equal(result.isError ?? false, false, JSON.stringify(result));
  const data: any = result.structuredContent;
  assert.ok(data, "MCP tool must return structured results");
  assert.notEqual(data.status, "failed", JSON.stringify(data));
  return data;
}
async function wait(draft: any): Promise<any> {
  const deadline = Date.now() + 60000;
  while (Date.now() < deadline) {
    const status = await tool("cad_job_get", { job_id: draft.job_id });
    if (status.status === "succeeded") return status.result;
    assert.notEqual(status.status, "cancelled", JSON.stringify(status));
    await delay(150);
  }
  throw new Error("MCP job deadline exceeded");
}
async function validateAndCommit(model: any, draft: any) {
  const binding = {
    model_id: model.model_id,
    base_revision: model.revision,
    transaction_id: draft.transaction_id,
  };
  const validation = await wait(
    await tool("cad_validate", { ...binding, idempotency_key: id("validate") }),
  );
  assert.equal(
    validation.status,
    "checks_passed_within_profile",
    JSON.stringify(validation),
  );
  const committed = await tool("cad_commit", {
    ...binding,
    validation_digest: validation.digest,
    idempotency_key: id("commit"),
  });
  return { ...committed, validation };
}
try {
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  const url = `http://127.0.0.1:${(server.address() as any).port}`;
  server.on(
    "request",
    createApp(service, {
      mode: "local",
      publicURL: url,
      dataRoot: directory,
      localToken: token,
    }).app,
  );
  await client.connect(
    new StreamableHTTPClientTransport(new URL(url + "/mcp"), {
      requestInit: { headers: { Authorization: `Bearer ${token}` } },
    }),
  );
  assert.ok(
    (await client.listTools()).tools.some((t) => t.name === "cad_list_models"),
  );
  await tool("cad_capabilities");
  assert.equal((await tool("cad_list_models")).total, 0);
  const empty = await tool("cad_create_model", {
    name: "LLcad – MCP-Dichtungsgehäuse",
    purpose: "Vollständig ohne Browser bedienbarer Modellierablauf",
    idempotency_key: id("create"),
  });
  const construction = {
    model_id: empty.model_id,
    base_revision: empty.revision,
    operations: [
      ...housing.features.map((feature) => ({ op: "add_feature", feature })),
      { op: "set_outputs", outputs: housing.outputs },
      ...housing.constraints.map((constraint) => ({
        op: "add_constraint",
        constraint,
      })),
    ],
  };
  await tool("cad_plan_edit", { ...construction, idempotency_key: id("plan") });
  const draft = await tool("cad_apply_patch", {
    ...construction,
    idempotency_key: id("construct"),
  });
  await wait(draft);
  const base = await validateAndCommit(empty, draft);
  const found = await tool("cad_find", {
    model_id: base.model_id,
    revision: base.revision,
    query: "Dichtungsnut",
  });
  assert.equal(found.ambiguity, false);
  assert.equal(found.total_matches, 1);
  const detail = await tool("cad_inspect", {
    model_id: base.model_id,
    revision: base.revision,
    selection_handle: found.matches[0].selection_handle,
  });
  const native = detail.face_page.faces.find((face: any) =>
    face.origins.some((o: any) => o.role === "inner_wall"),
  );
  // Fetch the next page if necessary; face discovery is available through MCP.
  let face = native;
  let offset = detail.face_page.next_offset;
  while (!face && offset !== null) {
    const page = await tool("cad_inspect", {
      model_id: base.model_id,
      revision: base.revision,
      feature_id: found.matches[0].feature_id,
      face_offset: offset,
    });
    face = page.face_page.faces.find((f: any) =>
      f.origins.some((o: any) => o.role === "inner_wall"),
    );
    offset = page.face_page.next_offset;
  }
  assert.ok(face);
  const selected = await tool("cad_inspect", {
    model_id: base.model_id,
    revision: base.revision,
    feature_id: found.matches[0].feature_id,
    face_id: face.face_id,
  });
  const patch = {
    model_id: base.model_id,
    base_revision: base.revision,
    selection_handle: selected.selection_handle,
    operations: [
      {
        op: "set_parameter",
        feature_id: selected.selected_entities[0],
        parameter: "depth",
        expected: selected.parameters.depth,
        value: { value: "0.82", unit: "mm" },
      },
    ],
  };
  await tool("cad_plan_edit", { ...patch, idempotency_key: id("plan") });
  const request = { ...patch, idempotency_key: id("edit") };
  const edit = await tool("cad_apply_patch", request);
  assert.equal((await tool("cad_apply_patch", request)).job_id, edit.job_id);
  await wait(edit);
  const changed = await validateAndCommit(base, edit);
  const rebound = await tool("cad_inspect", {
    model_id: base.model_id,
    revision: changed.revision,
    selection_handle: selected.selection_handle,
    rebind: true,
  });
  assert.equal(rebound.parameters.depth.value, "0.82");
  const measurements = await tool("cad_measure", {
    model_id: base.model_id,
    revision: changed.revision,
    feature_id: selected.selected_entities[0],
  });
  const comparison = await tool("cad_compare", {
    model_id: base.model_id,
    from_revision: base.revision,
    to_revision: changed.revision,
  });
  const exported = await wait(
    await tool("cad_export", {
      model_id: base.model_id,
      revision: changed.revision,
      format: "step",
      idempotency_key: id("export"),
    }),
  );
  const artifact = exported.artifacts.find(
    (a: any) => a.manifest.filename === "model.step",
  );
  assert.ok(artifact);
  const downloaded = await fetch(new URL(artifact.download, url), {
    headers: { Authorization: `Bearer ${token}` },
  });
  assert.equal(downloaded.status, 200);
  const data = Buffer.from(await downloaded.arrayBuffer());
  assert.ok(data.includes(Buffer.from("ISO-10303-21")));
  writeFileSync("reports/mcp-model.step", data);
  assert.equal(
    (await tool("cad_list_models", { query: "MCP-Dichtungsgehäuse" })).models[0]
      .revision,
    changed.revision,
  );
  writeFileSync(
    "reports/mcp-workflow.json",
    JSON.stringify(
      {
        status: "passed",
        created: new Date().toISOString(),
        registry_hash: REGISTRY_HASH,
        implementation_hash: IMPLEMENTATION_HASH,
        transport: "MCP Streamable HTTP",
        client: "official SDK",
        browser_interactions: 0,
        model_id: base.model_id,
        original_revision: base.revision,
        edited_revision: changed.revision,
        calls,
        measurements,
        comparison,
        roundtrip: artifact.manifest.roundtrip,
        scope:
          "Deterministic MCP controller integration test; no real LLM/target-host evaluation claimed.",
      },
      null,
      2,
    ) + "\n",
  );
  console.log(
    `MCP workflow passed: ${calls.length} tool calls, zero browser interactions, STEP export verified.`,
  );
} catch (error) {
  writeFileSync(
    "reports/mcp-workflow.json",
    JSON.stringify(
      {
        status: "failed",
        created: new Date().toISOString(),
        registry_hash: REGISTRY_HASH,
        implementation_hash: IMPLEMENTATION_HASH,
        calls,
      },
      null,
      2,
    ) + "\n",
  );
  throw error;
} finally {
  await client.close();
  server.closeAllConnections();
  await new Promise<void>((resolve) => server.close(() => resolve()));
  await service.close();
  rmSync(directory, { recursive: true, force: true });
}
