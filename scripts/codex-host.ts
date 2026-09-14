/** Verify the installed Codex host and shared service; no model inference. */
import assert from "node:assert/strict";
import { readFileSync, writeFileSync, mkdirSync } from "node:fs";
import { setTimeout as delay } from "node:timers/promises";
import { CodexHostClient } from "./codex-host-client.js";
import { housing } from "./fixtures.js";
import { id } from "../packages/semantic-ir/hash.js";
import { REGISTRY_HASH } from "../packages/compiler/index.js";
import { IMPLEMENTATION_HASH } from "../packages/compiler/build.js";

const client = new CodexHostClient();
const second = new CodexHostClient();
const calls: string[] = [];
const evidence: Record<string, any> = {
  status: "failed",
  created: new Date().toISOString(),
  registry_hash: REGISTRY_HASH,
  implementation_hash: IMPLEMENTATION_HASH,
  scope:
    "Installed Codex app-server MCP transport and deterministic CAD workflow; no model turn or LLM reasoning evaluation.",
  model_turns: 0,
  browser_interactions: 0,
  calls,
};
async function tool(name: string, args: any = {}): Promise<any> {
  calls.push(name);
  const result = await client.callTool(name, args);
  assert.equal(result.isError ?? false, false, JSON.stringify(result));
  const data = result.structuredContent;
  assert.ok(data, "Structured result required");
  assert.notEqual(data.status, "failed", JSON.stringify(data));
  return data;
}
async function wait(job: any) {
  const deadline = Date.now() + 60000;
  while (Date.now() < deadline) {
    const result = await tool("cad_job_get", { job_id: job.job_id });
    if (result.status === "succeeded") return result.result;
    assert.notEqual(result.status, "cancelled");
    await delay(250);
  }
  throw new Error("CAD job timed out");
}
async function commit(model: any, draft: any) {
  await wait(draft);
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
  return tool("cad_commit", {
    ...binding,
    validation_digest: validation.digest,
    idempotency_key: id("commit"),
  });
}
try {
  const inventories = await Promise.all([client.connect(), second.connect()]);
  evidence.host = client.version;
  evidence.tools = Object.values(inventories[0].tools)
    .map((t: any) => t.name)
    .sort();
  for (const inventory of inventories)
    assert.ok(
      Object.values(inventory.tools).some(
        (t: any) => t.name === "cad_list_models",
      ),
      JSON.stringify(inventory),
    );
  evidence.simultaneous_connections = inventories.length;
  const capabilities = await tool("cad_capabilities");
  assert.equal(capabilities.registry_hash, REGISTRY_HASH);
  assert.equal(
    capabilities.implementation_hash,
    IMPLEMENTATION_HASH,
    "Restart the installed service after a source change before recording host evidence",
  );
  const model = await tool("cad_create_model", {
    name: "Codex-Hostprüfung · Dichtungsgehäuse",
    purpose: "Automatischer Transportnachweis ohne CAD-Klicks",
    idempotency_key: id("host-model"),
  });
  evidence.model_id = model.model_id;
  const construction = {
    model_id: model.model_id,
    base_revision: model.revision,
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
  const base = await commit(
    model,
    await tool("cad_apply_patch", {
      ...construction,
      idempotency_key: id("build"),
    }),
  );
  const search = await tool("cad_find", {
    model_id: base.model_id,
    revision: base.revision,
    query: "Dichtungsnut",
  });
  assert.equal(search.total_matches, 1);
  const detail = await tool("cad_inspect", {
    model_id: base.model_id,
    revision: base.revision,
    selection_handle: search.matches[0].selection_handle,
  });
  const patch = {
    model_id: base.model_id,
    base_revision: base.revision,
    operations: [
      {
        op: "set_parameter",
        feature_id: search.matches[0].feature_id,
        parameter: "depth",
        expected: detail.parameters.depth,
        value: { value: "0.82", unit: "mm" },
      },
    ],
  };
  await tool("cad_plan_edit", { ...patch, idempotency_key: id("plan") });
  const request = { ...patch, idempotency_key: id("edit") };
  const draft = await tool("cad_apply_patch", request);
  assert.equal((await tool("cad_apply_patch", request)).job_id, draft.job_id);
  const changed = await commit(base, draft);
  evidence.original_revision = base.revision;
  evidence.edited_revision = changed.revision;
  evidence.measurements = await tool("cad_measure", {
    model_id: base.model_id,
    revision: changed.revision,
    feature_id: search.matches[0].feature_id,
  });
  evidence.comparison = await tool("cad_compare", {
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
  const token = readFileSync("data/local-token", "utf8").trim();
  const response = await fetch(
    new URL(artifact.download, "http://127.0.0.1:4310"),
    { headers: { Authorization: `Bearer ${token}` } },
  );
  assert.equal(response.status, 200);
  const bytes = Buffer.from(await response.arrayBuffer());
  assert.ok(bytes.includes(Buffer.from("ISO-10303-21")));
  mkdirSync("reports", { recursive: true });
  writeFileSync("reports/codex-host-model.step", bytes);
  evidence.roundtrip = artifact.manifest.roundtrip;
  const shared = await second.callTool("cad_get_model", {
    model_id: base.model_id,
  });
  assert.equal(shared.structuredContent.revision, changed.revision);
  evidence.shared_revision_visible = true;
  evidence.status = "passed";
  console.log(
    `Codex host passed: ${calls.length} CAD calls, 2 simultaneous connections, validated edit and STEP export, zero browser interactions.`,
  );
} finally {
  await Promise.all([client.close(), second.close()]);
  mkdirSync("reports", { recursive: true });
  writeFileSync(
    "reports/codex-host.json",
    JSON.stringify(evidence, null, 2) + "\n",
  );
}
