import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import assert from "node:assert/strict";
import { ModelService } from "../packages/model-service/index.js";
import { Principal, SCOPES } from "../packages/policy/index.js";
import { ToolName } from "../packages/semantic-ir/schema.js";
import { id } from "../packages/semantic-ir/hash.js";
export const principal: Principal = {
  tenant: "tenant-a",
  user: "alice",
  scopes: SCOPES,
};
export function setup() {
  const dir = mkdtempSync(join(tmpdir(), "mathforge-test-"));
  const service = new ModelService(dir);
  return {
    service,
    dir,
    async close() {
      await service.close();
      rmSync(dir, { recursive: true, force: true });
    },
  };
}
export function call(
  s: ModelService,
  tool: ToolName,
  args: any,
  p = principal,
) {
  const r = s.call(p, tool, args);
  assert.notEqual(r.status, "failed", JSON.stringify(r));
  return r;
}
export async function finish(s: ModelService, result: any, p = principal) {
  const job = await s.jobs.wait(p, result.job_id);
  assert.equal(job.status, "succeeded", JSON.stringify(job));
  return job.result;
}
export async function importFixture(s: ModelService, ir: any, p = principal) {
  const m = call(
    s,
    "cad_create_model",
    { name: "Fixture", profile: ir.profile, idempotency_key: id("create") },
    p,
  );
  const artifact = s.store.artifact(
    p,
    JSON.stringify(ir),
    "application/json",
    m.model_id,
    m.revision,
    { source: "fixture" },
  );
  const draft = call(
    s,
    "cad_import",
    {
      model_id: m.model_id,
      base_revision: m.revision,
      idempotency_key: id("import"),
      artifact_id: artifact.artifact_id,
      source_unit: "mm",
      format: "ir",
    },
    p,
  );
  const candidate = await finish(s, draft, p);
  const validation = await finish(
    s,
    call(
      s,
      "cad_validate",
      {
        model_id: m.model_id,
        base_revision: m.revision,
        transaction_id: draft.transaction_id,
        idempotency_key: id("validate"),
      },
      p,
    ),
    p,
  );
  assert.equal(
    validation.status,
    "checks_passed_within_profile",
    JSON.stringify(validation),
  );
  const commit = call(
    s,
    "cad_commit",
    {
      model_id: m.model_id,
      base_revision: m.revision,
      transaction_id: draft.transaction_id,
      validation_digest: validation.digest,
      idempotency_key: id("commit"),
    },
    p,
  );
  return { ...commit, candidate, validation };
}
