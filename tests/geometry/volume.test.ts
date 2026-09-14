import test from "node:test";
import assert from "node:assert/strict";
import { setup, call, finish, importFixture, principal } from "../helpers.js";
import { organic } from "../../scripts/fixtures.js";
import { id, hash } from "../../packages/semantic-ir/hash.js";
import { ModelService } from "../../packages/model-service/index.js";
import { rmSync } from "node:fs";

test("isolated OpenVDB roundtrip and private spatial reuse survive restart and a compact edit", async () => {
  const env = setup();
  let s = env.service;
  try {
    const m = await importFixture(s, organic);
    const render = () =>
      finish(
        s,
        call(s, "cad_render", {
          model_id: m.model_id,
          revision: m.revision,
          idempotency_key: id("render"),
        }),
      );
    const first = await render();
    assert.ok(first.metrics.field_samples.organic.sample_cache_misses > 0);
    await s.close();
    s = new ModelService(env.dir);
    const second = await render();
    assert.equal(second.metrics.field_samples.organic.sample_cache_misses, 0);
    assert.ok(second.metrics.field_samples.organic.sample_cache_hits > 100);
    assert.equal(
      second.artifacts.some((a: any) =>
        a.manifest.filename.endsWith(".field.json"),
      ),
      false,
    );
    const exported = await finish(
      s,
      call(s, "cad_export", {
        model_id: m.model_id,
        revision: m.revision,
        format: "vdb",
        idempotency_key: id("vdb"),
      }),
    );
    const artifact = exported.artifacts.find(
      (a: any) => a.manifest.filename === "model.vdb",
    );
    assert.ok(artifact);
    assert.equal(artifact.manifest.roundtrip.sample_roundtrip_error, 0);
    assert.equal(
      artifact.manifest.roundtrip.stored_semantics,
      "truncated_implicit_samples",
    );
    assert.equal(
      artifact.manifest.roundtrip.continuous_distance_certificate,
      null,
    );
    assert.ok(s.store.readBlob(artifact.hash).length > 1000);
    const source = (organic.features[0].construction as any).expression;
    const draft = call(s, "cad_apply_patch", {
      model_id: m.model_id,
      base_revision: m.revision,
      idempotency_key: id("detail"),
      operations: [
        {
          op: "set_field",
          feature_id: "organic",
          expected_hash: hash(source),
          expression: {
            op: "local_field_delta",
            source,
            center: ["5", "0", "0"],
            radius: "1",
            amplitude: "0.1",
          },
        },
      ],
    });
    const candidate = await finish(s, draft);
    const edited = await finish(
      s,
      call(s, "cad_render", {
        model_id: m.model_id,
        revision: candidate.candidate_revision,
        idempotency_key: id("preview"),
      }),
    );
    const stats = edited.metrics.field_samples.organic;
    assert.ok(stats.sample_cache_hits > stats.sample_cache_misses);
    assert.ok(stats.sample_cache_misses > 0);
    const p = { ...principal, user: "bob" };
    assert.equal(
      s.call(p, "cad_export", {
        model_id: m.model_id,
        revision: m.revision,
        format: "vdb",
        idempotency_key: id("foreign"),
      }).errors[0].code,
      "ACCESS_DENIED",
    );
    const other = await importFixture(s, organic, p);
    const cold = await finish(
      s,
      call(
        s,
        "cad_render",
        {
          model_id: other.model_id,
          revision: other.revision,
          idempotency_key: id("other"),
        },
        p,
      ),
      p,
    );
    assert.equal(
      cold.metrics.field_samples.organic.sample_cache_misses,
      first.metrics.field_samples.organic.sample_cache_misses,
    );
  } finally {
    await s.close();
    rmSync(env.dir, { recursive: true, force: true });
  }
});

test("existing compact details can be revised while protected regions still block global edits", async () => {
  const env = setup(),
    s = env.service;
  try {
    const ir = structuredClone(organic),
      construction: any = ir.features[0].construction;
    construction.expression = {
      op: "local_field_delta",
      source: construction.expression,
      center: ["5", "0", "0"],
      radius: "1",
      amplitude: "0.04",
    };
    const model = await importFixture(s, ir);
    const change = async (revision: string, before: any, after: any) => {
      const draft = call(s, "cad_apply_patch", {
        model_id: model.model_id,
        base_revision: revision,
        idempotency_key: id("detail"),
        operations: [
          {
            op: "set_field",
            feature_id: "organic",
            expected_hash: hash(before),
            expression: after,
          },
        ],
      });
      await finish(s, draft);
      const validation = await finish(
        s,
        call(s, "cad_validate", {
          model_id: model.model_id,
          base_revision: revision,
          transaction_id: draft.transaction_id,
          idempotency_key: id("validate"),
        }),
      );
      return { draft, validation };
    };
    const modified = { ...construction.expression, amplitude: "0.02" };
    const good = await change(
      model.revision,
      construction.expression,
      modified,
    );
    assert.equal(good.validation.status, "checks_passed_within_profile");
    assert.equal(
      good.validation.checks.find((c: any) => c.check_id === "remote-protected")
        .guarantee,
      "exact_for_declared_domain",
    );
    const committed = call(s, "cad_commit", {
      model_id: model.model_id,
      base_revision: model.revision,
      transaction_id: good.draft.transaction_id,
      validation_digest: good.validation.digest,
      idempotency_key: id("commit"),
    });
    const bad = await change(committed.revision, modified, {
      ...modified,
      source: { ...modified.source, radius: "5.01" },
    });
    assert.equal(bad.validation.status, "failed");
    assert.equal(
      bad.validation.checks.find((c: any) => c.check_id === "remote-protected")
        .status,
      "failed",
    );
    const rejected = s.call(principal, "cad_commit", {
      model_id: model.model_id,
      base_revision: committed.revision,
      transaction_id: bad.draft.transaction_id,
      validation_digest: bad.validation.digest,
      idempotency_key: id("reject"),
    });
    assert.equal(rejected.status, "failed");
    assert.equal(
      s.store.model(principal, model.model_id).head,
      committed.revision,
    );
    assert.equal(
      s.store.revision(principal, model.model_id, model.revision).ir_hash,
      hash(ir),
    );
  } finally {
    await env.close();
  }
});
