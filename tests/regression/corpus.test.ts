import test from "node:test";
import assert from "node:assert/strict";
import { setup, call, finish, principal } from "../helpers.js";
import { id } from "../../packages/semantic-ir/hash.js";
import { CORPUS } from "./corpus.js";

test("geometric regression corpus: every boundary class has a declared, checked outcome", async () => {
  const env = setup(),
    s = env.service;
  const results: Record<string, string> = {};
  try {
    for (const entry of CORPUS) {
      const m = call(s, "cad_create_model", {
        name: entry.name,
        profile: entry.ir.profile,
        idempotency_key: id("create"),
      });
      const upload = s.store.artifact(
        principal,
        JSON.stringify(entry.ir),
        "application/json",
        m.model_id,
        m.revision,
        {},
      );
      const draft = call(s, "cad_import", {
        model_id: m.model_id,
        base_revision: m.revision,
        artifact_id: upload.artifact_id,
        format: "ir",
        source_unit: "mm",
        idempotency_key: id("import"),
      });
      const job = await s.jobs.wait(principal, draft.job_id);
      if (entry.expect.outcome === "error") {
        assert.equal(
          job.status,
          "failed",
          entry.name + ": " + JSON.stringify(job),
        );
        assert.equal(
          job.error.code,
          entry.expect.code,
          entry.name + ": " + JSON.stringify(job.error),
        );
        assert.equal(
          s.store.model(principal, m.model_id).head,
          m.revision,
          entry.name + ": head must stay unchanged",
        );
        results[entry.name] = "error:" + job.error.code;
        continue;
      }
      assert.equal(
        job.status,
        "succeeded",
        entry.name + ": " + JSON.stringify(job.error),
      );
      const validation = await finish(
        s,
        call(s, "cad_validate", {
          model_id: m.model_id,
          base_revision: m.revision,
          transaction_id: draft.transaction_id,
          idempotency_key: id("validate"),
        }),
      );
      assert.equal(
        validation.status,
        "checks_passed_within_profile",
        entry.name +
          ": " +
          JSON.stringify(
            validation.checks.filter((c: any) => c.status !== "passed"),
          ),
      );
      const revision = s.store.revision(
        principal,
        m.model_id,
        draft.candidate_revision,
      );
      if (entry.expect.facts)
        assert.ok(
          entry.expect.facts(
            revision.geometry.facts,
            revision.geometry.aggregate,
          ),
          entry.name + ": facts " + JSON.stringify(revision.geometry.facts),
        );
      const commit = call(s, "cad_commit", {
        model_id: m.model_id,
        base_revision: m.revision,
        transaction_id: draft.transaction_id,
        validation_digest: validation.digest,
        idempotency_key: id("commit"),
      });
      if (entry.expect.roundtrip) {
        const exported = await finish(
          s,
          call(s, "cad_export", {
            model_id: m.model_id,
            revision: commit.revision,
            format: "step",
            idempotency_key: id("step"),
          }),
        );
        const file = exported.artifacts.find(
          (a: any) => a.manifest.filename === "model.step",
        );
        assert.equal(
          file.manifest.roundtrip.status,
          "checks_passed_within_profile",
          entry.name,
        );
      }
      results[entry.name] = "validated";
    }
    assert.equal(Object.keys(results).length, CORPUS.length);
    assert.ok(new Set(CORPUS.map((c) => c.class)).size >= 11);
  } finally {
    await env.close();
  }
});
