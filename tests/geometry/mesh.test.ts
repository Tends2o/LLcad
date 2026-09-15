import test from "node:test";
import assert from "node:assert/strict";
import { setup, call, finish, principal } from "../helpers.js";
import { id, hash } from "../../packages/semantic-ir/hash.js";
import { validate } from "../../packages/validation/index.js";
import { MeshQuality } from "../../packages/semantic-ir/mesh.js";
import { checkNativeMeshBuild } from "../../packages/compiler/native-build.js";

import { meshSTL as stl } from "../../scripts/mesh-fixtures.js";

async function imported(s: any, source: string, profile = "watertight_solid") {
  const m = call(s, "cad_create_model", {
    name: "Mesh acceptance",
    profile,
    idempotency_key: id("create"),
  });
  const original = s.store.artifact(
    principal,
    source,
    "model/stl",
    null,
    null,
    { source: "user_upload" },
  );
  const draft = call(s, "cad_import", {
    model_id: m.model_id,
    base_revision: m.revision,
    artifact_id: original.artifact_id,
    format: "stl",
    source_unit: "mm",
    idempotency_key: id("import"),
  });
  await finish(s, draft);
  const binding = {
    model_id: m.model_id,
    base_revision: m.revision,
    transaction_id: draft.transaction_id,
  };
  const proof = await finish(
    s,
    call(s, "cad_validate", { ...binding, idempotency_key: id("validate") }),
  );
  return { m, original, draft, binding, proof };
}

test("watertight STL uses indexed mesh authority, mandatory proofs and actual STL/GLB roundtrip checks", async () => {
  const env = setup(),
    s = env.service;
  try {
    assert.equal(checkNativeMeshBuild().kernel, "EPECK");
    const { m, original, binding, proof } = await imported(s, stl());
    assert.equal(
      proof.status,
      "checks_passed_within_profile",
      JSON.stringify(proof),
    );
    assert.equal(proof.profile, "watertight_solid");
    assert.ok(
      proof.checks.filter((c: any) => c.check_id.startsWith("mesh-")).length >=
        15,
    );
    const committed = call(s, "cad_commit", {
      ...binding,
      validation_digest: proof.digest,
      idempotency_key: id("commit"),
    });
    const model = call(s, "cad_get_model", { model_id: m.model_id });
    const detail = call(s, "cad_inspect", {
      model_id: m.model_id,
      revision: committed.revision,
      feature_id: model.features[0].id,
    });
    assert.equal(model.features[0].representation, "mesh");
    const q = MeshQuality.parse(detail.known_facts.mesh_quality);
    assert.equal(q.watertight_solid, true);
    assert.equal(detail.known_facts.solids, 1);
    assert.equal(detail.known_facts.precision_solid_only, false);
    const stored = s.store.revision(principal, m.model_id, committed.revision);
    assert.ok(
      Object.keys(stored.geometry.blobs).some((k) => k.endsWith(".mesh.json")),
    );
    assert.ok(
      !Object.keys(stored.geometry.blobs).some((k) => k.endsWith(".brep")),
    );
    for (const format of ["stl", "glb"]) {
      const result = await finish(
        s,
        call(s, "cad_export", {
          model_id: m.model_id,
          revision: committed.revision,
          format,
          idempotency_key: id("export"),
        }),
      );
      const output = result.artifacts.find(
        (a: any) => a.manifest.filename === "model." + format,
      );
      assert.ok(output);
      const rt = output.manifest.roundtrip;
      assert.equal(rt.restored_mesh_quality.watertight_solid, true);
      assert.ok(rt.measured_vertex_error_bound_mm <= 0.0001);
      assert.equal(output.manifest.quality, "checks_passed_within_profile");
      assert.equal(
        rt.restored_mesh_quality.manufacturing_status,
        "not_certified",
      );
      assert.ok(
        !result.artifacts.some((a: any) =>
          a.manifest.filename.endsWith(".mesh.json"),
        ),
      );
      const manifest = JSON.parse(
        s.store.readBlob(result.package_manifest.hash).toString(),
      );
      assert.equal(manifest.source_assets[0].sha256, original.hash);
      assert.equal(manifest.source_assets[0].artifact_id, original.artifact_id);
      assert.equal(manifest.precision.certified_surface_error_bound_mm, null);
    }
    const request = call(s, "cad_export", {
      model_id: m.model_id,
      revision: committed.revision,
      format: "step",
      idempotency_key: id("unsupported"),
    });
    const denied = await s.jobs.wait(principal, request.job_id);
    assert.equal(denied.status, "failed");
    assert.equal(denied.error.code, "OUT_OF_SCOPE");
  } finally {
    await env.close();
  }
});

test("open and inward meshes cannot commit as watertight; render surface preserves open geometry", async () => {
  const env = setup(),
    s = env.service;
  try {
    for (const source of [stl(true), stl(false, true)]) {
      const { m, binding, proof } = await imported(s, source);
      assert.equal(proof.status, "failed");
      assert.ok(
        proof.checks.some(
          (c: any) => c.check_id.startsWith("mesh-") && c.status === "failed",
        ),
      );
      const commit = s.call(principal, "cad_commit", {
        ...binding,
        validation_digest: proof.digest,
        idempotency_key: id("commit"),
      });
      assert.equal(commit.errors[0].code, "VALIDATION_REQUIRED");
      assert.equal(
        call(s, "cad_get_model", { model_id: m.model_id }).revision,
        m.revision,
      );
    }
    const { binding, proof } = await imported(s, stl(true), "render_surface");
    assert.equal(proof.status, "checks_passed_within_profile");
    const committed = call(s, "cad_commit", {
      ...binding,
      validation_digest: proof.digest,
      idempotency_key: id("commit"),
    });
    const rev = s.store.revision(
      principal,
      binding.model_id,
      committed.revision,
    );
    assert.equal(rev.geometry.aggregate.volume, null);
    assert.equal(
      rev.geometry.aggregate.mesh_quality.topology.boundary_edges,
      3,
    );
  } finally {
    await env.close();
  }
});

test("mesh proof rejects missing, stale, inconsistent and mismatched exact reports", async () => {
  const env = setup(),
    s = env.service;
  try {
    const { draft, binding } = await imported(s, stl());
    const tx = s.store.transaction(principal, draft.transaction_id);
    const base = s.store.revision(
      principal,
      binding.model_id,
      binding.base_revision,
    );
    const fid = tx.plan.outputs[0];
    const mutations = [
      (r: any) => delete r.facts[fid].mesh_quality,
      (r: any) =>
        (r.facts[fid].mesh_quality.native.source_hash = "0".repeat(64)),
      (r: any) => (r.facts[fid].geometry_hash = "0".repeat(64)),
      (r: any) => (r.facts[fid].mesh_quality.topology.boundary_edges = 1),
      (r: any) =>
        (r.facts[fid].mesh_quality.native.components[0].outward = false),
      (r: any) =>
        (r.aggregate.mesh_quality.checks.no_self_intersections = false),
      (r: any) => (r.aggregate.mesh_quality.unregistered = "untrusted"),
    ];
    for (const mutate of mutations) {
      const result = structuredClone(tx.result);
      mutate(result);
      assert.equal(
        validate(tx.plan.ir, result, base.ir, base.geometry, tx.candidate)
          .status,
        "failed",
      );
    }
    for (const target of ["1", "1.00000000000000001"]) {
      const result = structuredClone(tx.result),
        ir = structuredClone(tx.plan.ir);
      // The cube's true exact volume is 1; a sound tighter interval isolates
      // the decimal request comparison from CGAL's conservative enclosure.
      result.facts[fid].mesh_quality.native.signed_volume_interval_mm3 = [1, 1];
      ir.constraints.push({
        id: "exact-volume",
        kind: "volume",
        feature_id: fid,
        target: { value: target, unit: "mm3" },
        tolerance: { value: "0.000000000000000001", unit: "mm3" },
      });
      const checked = validate(
        ir,
        result,
        base.ir,
        base.geometry,
        tx.candidate,
      );
      assert.equal(
        checked.checks.find((c: any) => c.check_id === "exact-volume")?.status,
        target === "1" ? "passed" : "failed",
      );
    }
    assert.equal(
      hash(tx.result),
      hash(s.store.transaction(principal, draft.transaction_id).result),
    );
  } finally {
    await env.close();
  }
});

test("STL float32 collapse fails export without changing the committed mesh or publishing artifacts", async () => {
  const env = setup(),
    s = env.service;
  try {
    const { binding, proof } = await imported(s, stl(false, false, 0.001, 1e9));
    assert.equal(proof.status, "checks_passed_within_profile");
    const committed = call(s, "cad_commit", {
      ...binding,
      validation_digest: proof.digest,
      idempotency_key: id("commit"),
    });
    const before = s.store.get("SELECT COUNT(*) AS n FROM artifacts").n;
    const request = call(s, "cad_export", {
      model_id: binding.model_id,
      revision: committed.revision,
      format: "stl",
      idempotency_key: id("export"),
    });
    const failed = await s.jobs.wait(principal, request.job_id);
    assert.equal(failed.status, "failed");
    assert.ok(
      ["PRECISION_UNSUPPORTED", "GEOMETRY_INVALID"].includes(failed.error.code),
    );
    assert.equal(s.store.get("SELECT COUNT(*) AS n FROM artifacts").n, before);
    assert.equal(
      call(s, "cad_get_model", { model_id: binding.model_id }).revision,
      committed.revision,
    );
  } finally {
    await env.close();
  }
});
