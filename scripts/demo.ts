import { resolve } from "node:path";
import { mkdirSync, writeFileSync } from "node:fs";
import { ModelService } from "../packages/model-service/index.js";
import { SCOPES } from "../packages/policy/index.js";
import { housing } from "./fixtures.js";
import { id } from "../packages/semantic-ir/hash.js";
const service = new ModelService(resolve(process.env.MATHFORGE_DATA ?? "data"));
const p = { tenant: "local", user: "local-user", scopes: SCOPES };
const call = (name: any, args: any) => {
  const r = service.call(p, name, args);
  if (r.status === "failed") throw new Error(JSON.stringify(r.errors));
  return r;
};
const wait = async (r: any) => {
  const job = await service.jobs.wait(p, r.job_id);
  if (job.status !== "succeeded") throw new Error(JSON.stringify(job.error));
  return job.result;
};
try {
  const m = call("cad_create_model", {
    name: "Dichtungsgehäuse · 20-µm-Korrektur",
    purpose: "Durchgängiger Abnahmelauf des Bauplans, Abschnitt 25",
    idempotency_key: id("demo-create"),
  });
  const a = service.store.artifact(
    p,
    JSON.stringify(housing),
    "application/json",
    m.model_id,
    m.revision,
    { source: "reference_fixture" },
  );
  async function commit(draft: any, base: string) {
    await wait(draft);
    const v = await wait(
      call("cad_validate", {
        model_id: m.model_id,
        base_revision: base,
        transaction_id: draft.transaction_id,
        idempotency_key: id("demo-validate"),
      }),
    );
    if (v.status !== "checks_passed_within_profile")
      throw new Error(JSON.stringify(v));
    const revision = call("cad_commit", {
      model_id: m.model_id,
      base_revision: base,
      transaction_id: draft.transaction_id,
      validation_digest: v.digest,
      idempotency_key: id("demo-commit"),
    });
    return { revision, validation: v };
  }
  const first = await commit(
    call("cad_import", {
      model_id: m.model_id,
      base_revision: m.revision,
      artifact_id: a.artifact_id,
      format: "ir",
      source_unit: "mm",
      idempotency_key: id("demo-import"),
    }),
    m.revision,
  );
  const second = await commit(
    call("cad_apply_patch", {
      model_id: m.model_id,
      base_revision: first.revision.revision,
      idempotency_key: id("demo-patch"),
      operations: [
        {
          op: "set_parameter",
          feature_id: "feat-groove-07",
          parameter: "depth",
          expected: { value: "0.80", unit: "mm" },
          value: { value: "0.82", unit: "mm" },
        },
      ],
    }),
    first.revision.revision,
  );
  const exported = await wait(
    call("cad_export", {
      model_id: m.model_id,
      revision: second.revision.revision,
      format: "step",
      idempotency_key: id("demo-export"),
    }),
  );
  const detail = call("cad_inspect", {
    model_id: m.model_id,
    revision: second.revision.revision,
    feature_id: "feat-groove-07",
  });
  mkdirSync("reports/demo", { recursive: true });
  for (const artifact of exported.artifacts) {
    const stored = service.store.getArtifact(p, artifact.artifact_id);
    writeFileSync(
      "reports/demo/" + stored.manifest.filename,
      service.store.readBlob(stored.hash),
    );
  }
  const report = {
    status: "passed",
    model_id: m.model_id,
    before_revision: first.revision.revision,
    after_revision: second.revision.revision,
    measurements: detail.known_facts.dimensions,
    validation: second.validation,
    artifacts: exported.artifacts,
  };
  writeFileSync(
    "reports/demo/report.json",
    JSON.stringify(report, null, 2) + "\n",
  );
  console.log(
    JSON.stringify(
      {
        model_id: m.model_id,
        revision: second.revision.revision,
        measurements: report.measurements,
        step: "reports/demo/model.step",
      },
      null,
      2,
    ),
  );
} finally {
  await service.close();
}
