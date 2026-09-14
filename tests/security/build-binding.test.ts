import test from "node:test";
import assert from "node:assert/strict";
import { setup, call, importFixture, principal, finish } from "../helpers.js";
import { sphere } from "../../scripts/fixtures.js";
import { id } from "../../packages/semantic-ir/hash.js";
import { REGISTRY_HASH } from "../../packages/compiler/index.js";
test("old build geometry is readable but cannot silently be recalculated or exported", async () => {
  const env = setup(),
    s = env.service;
  try {
    const m = await importFixture(s, sphere);
    const rev = s.store.revision(principal, m.model_id, m.revision);
    rev.geometry.facts.sphere.cache_key = "0".repeat(64);
    s.store.run(
      "UPDATE revisions SET geometry=? WHERE id=?",
      JSON.stringify(rev.geometry),
      m.revision,
    );
    assert.equal(
      call(s, "cad_get_model", { model_id: m.model_id }).revision,
      m.revision,
    );
    assert.equal(
      call(s, "cad_get_model", { model_id: m.model_id }).build_compatibility
        .status,
      "rebuild_required",
    );
    for (const tool of ["cad_render", "cad_export"] as const) {
      const result = s.call(principal, tool, {
        model_id: m.model_id,
        revision: m.revision,
        idempotency_key: id("build"),
        ...(tool === "cad_export" ? { format: "step" } : {}),
      });
      assert.equal(result.errors[0].code, "BUILD_MISMATCH");
    }
  } finally {
    await env.close();
  }
});
test("explicit rebuild binds the target build, preserves old revisions and requires validation", async () => {
  const env = setup(),
    s = env.service;
  try {
    const m = await importFixture(s, sphere);
    const revision = s.store.revision(principal, m.model_id, m.revision);
    revision.geometry.facts.sphere.cache_key = "0".repeat(64);
    const oldGeometry = JSON.stringify(revision.geometry);
    s.store.run(
      "UPDATE revisions SET geometry=? WHERE id=?",
      oldGeometry,
      m.revision,
    );
    const binding = {
      model_id: m.model_id,
      base_revision: m.revision,
      target_registry_hash: REGISTRY_HASH,
    };
    const request = { ...binding, idempotency_key: id("rebuild") };
    assert.equal(
      s.call(principal, "cad_rebuild", {
        ...request,
        target_registry_hash: "0".repeat(64),
      }).errors[0].code,
      "BUILD_MISMATCH",
    );
    assert.equal(
      s.call({ ...principal, scopes: ["model:read"] }, "cad_rebuild", request)
        .errors[0].code,
      "ACCESS_DENIED",
    );
    assert.equal(
      s.call({ ...principal, user: "bob" }, "cad_rebuild", request).errors[0]
        .code,
      "ACCESS_DENIED",
    );
    const plan = call(s, "cad_rebuild", request);
    assert.equal(plan.status, "planned");
    assert.equal(plan.source_ir_hash, plan.candidate_ir_hash);
    assert.equal(plan.job_id, undefined);
    assert.deepEqual(plan.protected_constraints, revision.ir.constraints);
    const buildRequest = {
      ...binding,
      mode: "candidate",
      idempotency_key: id("candidate"),
    };
    const draft = call(s, "cad_rebuild", buildRequest);
    assert.equal(call(s, "cad_rebuild", buildRequest).job_id, draft.job_id);
    await finish(s, draft);
    const tx = {
      model_id: m.model_id,
      base_revision: m.revision,
      transaction_id: draft.transaction_id,
    };
    assert.equal(
      s.call(principal, "cad_commit", {
        ...tx,
        validation_digest: "0".repeat(64),
        idempotency_key: id("early-commit"),
      }).errors[0].code,
      "VALIDATION_REQUIRED",
    );
    const validation = await finish(
      s,
      call(s, "cad_validate", { ...tx, idempotency_key: id("validate") }),
    );
    assert.equal(validation.status, "checks_passed_within_profile");
    const committed = call(s, "cad_commit", {
      ...tx,
      validation_digest: validation.digest,
      idempotency_key: id("commit"),
    });
    assert.notEqual(committed.revision, m.revision);
    assert.equal(
      s.store.get("SELECT geometry FROM revisions WHERE id=?", m.revision)
        .geometry,
      oldGeometry,
    );
    assert.equal(
      call(s, "cad_get_model", { model_id: m.model_id }).build_compatibility
        .status,
      "current",
    );
    assert.equal(
      s.call(principal, "cad_rebuild", {
        ...binding,
        idempotency_key: id("stale-rebuild"),
      }).errors[0].code,
      "STALE_REVISION",
    );
    await finish(
      s,
      call(s, "cad_render", {
        model_id: m.model_id,
        revision: committed.revision,
        idempotency_key: id("render"),
      }),
    );
  } finally {
    await env.close();
  }
});
test("rebuild retains protected bounds and cannot commit changed geometry outside tolerance", async () => {
  const env = setup(),
    s = env.service;
  try {
    const construction = {
      ...sphere,
      constraints: [
        {
          id: "outer-bounds",
          kind: "protected_bounds",
          feature_id: "sphere",
          tolerance: { value: "0.00001", unit: "mm" },
        },
      ],
    };
    const m = await importFixture(s, construction);
    const revision = s.store.revision(principal, m.model_id, m.revision);
    revision.geometry.facts.sphere.cache_key = "0".repeat(64);
    revision.geometry.facts.sphere.bounds[0] -= 1;
    s.store.run(
      "UPDATE revisions SET geometry=? WHERE id=?",
      JSON.stringify(revision.geometry),
      m.revision,
    );
    const binding = { model_id: m.model_id, base_revision: m.revision };
    const draft = call(s, "cad_rebuild", {
      ...binding,
      target_registry_hash: REGISTRY_HASH,
      mode: "candidate",
      idempotency_key: id("rebuild"),
    });
    await finish(s, draft);
    const tx = { ...binding, transaction_id: draft.transaction_id };
    const validation = await finish(
      s,
      call(s, "cad_validate", { ...tx, idempotency_key: id("validate") }),
    );
    assert.notEqual(validation.status, "checks_passed_within_profile");
    assert.equal(
      s.call(principal, "cad_commit", {
        ...tx,
        validation_digest: validation.digest,
        idempotency_key: id("commit"),
      }).errors[0].code,
      "VALIDATION_REQUIRED",
    );
    assert.equal(
      call(s, "cad_get_model", { model_id: m.model_id }).revision,
      m.revision,
    );
  } finally {
    await env.close();
  }
});
