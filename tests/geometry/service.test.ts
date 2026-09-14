import test from "node:test";
import assert from "node:assert/strict";
import { setup, call, finish, importFixture } from "../helpers.js";
import { housing } from "../../scripts/fixtures.js";
import { id } from "../../packages/semantic-ir/hash.js";
test("0.80 → 0.82 mm groove correction, mandatory validation, measured width/wall, immutable revision and STEP roundtrip", async () => {
  const env = setup(),
    s = env.service;
  try {
    const m = await importFixture(s, housing);
    const patch = {
      model_id: m.model_id,
      base_revision: m.revision,
      idempotency_key: id("patch"),
      operations: [
        {
          op: "set_parameter",
          feature_id: "feat-groove-07",
          parameter: "depth",
          expected: { value: "0.80", unit: "mm" },
          value: { value: "0.82", unit: "mm" },
        },
      ],
    };
    const draft = call(s, "cad_apply_patch", patch);
    assert.equal(call(s, "cad_apply_patch", patch).job_id, draft.job_id);
    const candidate = await finish(s, draft);
    assert.ok(candidate.metrics.cache_hits >= 1);
    const detail = call(s, "cad_inspect", {
      model_id: m.model_id,
      revision: candidate.candidate_revision,
      feature_id: "feat-groove-07",
    });
    assert.ok(Math.abs(detail.known_facts.dimensions.depth - 0.82) < 1e-7);
    assert.ok(Math.abs(detail.known_facts.dimensions.width - 1.2) < 1e-7);
    assert.ok(
      Math.abs(detail.known_facts.dimensions.remaining_wall - 2.18) < 1e-7,
    );
    const denied = s.call(
      {
        tenant: "tenant-a",
        user: "alice",
        scopes: ["model:read", "model:commit"],
      },
      "cad_commit",
      {
        model_id: m.model_id,
        base_revision: m.revision,
        transaction_id: draft.transaction_id,
        validation_digest: "0".repeat(64),
        idempotency_key: id("no-validation"),
      },
    );
    assert.equal(denied.errors[0].code, "VALIDATION_REQUIRED");
    const validation = await finish(
      s,
      call(s, "cad_validate", {
        model_id: m.model_id,
        base_revision: m.revision,
        transaction_id: draft.transaction_id,
        idempotency_key: id("validate"),
      }),
    );
    assert.equal(validation.status, "checks_passed_within_profile");
    const committed = call(s, "cad_commit", {
      model_id: m.model_id,
      base_revision: m.revision,
      transaction_id: draft.transaction_id,
      validation_digest: validation.digest,
      idempotency_key: id("commit"),
    });
    assert.notEqual(committed.revision, m.revision);
    const old = call(s, "cad_inspect", {
      model_id: m.model_id,
      revision: m.revision,
      feature_id: "feat-groove-07",
    });
    assert.equal(old.parameters.depth.value, "0.80");
    const exported = await finish(
      s,
      call(s, "cad_export", {
        model_id: m.model_id,
        revision: committed.revision,
        format: "step",
        idempotency_key: id("export"),
      }),
    );
    assert.equal(
      exported.artifacts[0].manifest.roundtrip.status,
      "checks_passed_within_profile",
    );
  } finally {
    await env.close();
  }
});
