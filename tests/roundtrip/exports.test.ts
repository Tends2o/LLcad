import test from "node:test";
import assert from "node:assert/strict";
import { setup, call, finish, importFixture, principal } from "../helpers.js";
import { sphere, organic } from "../../scripts/fixtures.js";
import { id, hash } from "../../packages/semantic-ir/hash.js";
test("IR, STEP, STL, B-Rep and GLB exports have actual roundtrip reports and private artifacts", async () => {
  const env = setup(),
    s = env.service;
  try {
    const m = await importFixture(s, sphere);
    for (const format of ["ir", "step", "stl", "brep", "glb"]) {
      const response = call(s, "cad_export", {
        model_id: m.model_id,
        revision: m.revision,
        format,
        idempotency_key: id("export"),
      });
      const result = response.job_id ? await finish(s, response) : response;
      assert.ok(result.artifacts.length);
      const artifact = s.store.getArtifact(
        principal,
        result.artifacts[0].artifact_id,
      );
      assert.ok(artifact.manifest.roundtrip);
      assert.ok(s.store.readBlob(artifact.hash).length > 20);
      const manifestArtifact = s.store.getArtifact(
        principal,
        result.package_manifest.artifact_id,
      );
      const manifest = JSON.parse(
        s.store.readBlob(manifestArtifact.hash).toString(),
      );
      assert.equal(manifest.revision, m.revision);
      assert.equal(
        manifest.exported_geometry_unit,
        format === "glb" ? "m" : "mm",
      );
      assert.equal(manifest.precision.certified_surface_error_bound_mm, null);
      const component = (filename: string) =>
        manifest.components.find((a: any) => a.filename === filename);
      for (const entry of manifest.components) {
        const stored = s.store.getArtifact(principal, entry.artifact_id);
        assert.equal(stored.hash, entry.sha256);
        assert.equal(s.store.readBlob(stored.hash).length, entry.byte_length);
      }
      const ir = JSON.parse(
        s.store.readBlob(component("model.ir.json").sha256).toString(),
      );
      assert.equal(
        hash(ir),
        s.store.revision(principal, m.model_id, m.revision).ir_hash,
      );
      const proof = JSON.parse(
        s.store.readBlob(component("validation.json").sha256).toString(),
      );
      const { digest, ...proofBody } = proof.validation;
      assert.equal(digest, hash(proofBody));
      assert.equal(manifest.validation_digest, digest);
      assert.equal(proof.validation.ir_hash, hash(ir));
      assert.throws(() =>
        s.store.getArtifact(
          { ...principal, user: "other-user" },
          manifestArtifact.id,
        ),
      );
      if (format === "glb") {
        const data = s.store.readBlob(artifact.hash);
        assert.equal(data.toString("ascii", 0, 4), "glTF");
        assert.equal(data.readUInt32LE(8), data.length);
        const scene = JSON.parse(
          data.subarray(20, 20 + data.readUInt32LE(12)).toString(),
        );
        assert.deepEqual(scene.nodes[0].rotation, [
          -Math.SQRT1_2,
          0,
          0,
          Math.SQRT1_2,
        ]);
        assert.equal(artifact.manifest.unit, "m");
      }
    }
  } finally {
    await env.close();
  }
});
test("organic preview is extracted from the field AST with honest preview status", async () => {
  const env = setup(),
    s = env.service;
  try {
    const m = await importFixture(s, organic);
    const result = await finish(
      s,
      call(s, "cad_render", {
        model_id: m.model_id,
        revision: m.revision,
        idempotency_key: id("render"),
      }),
    );
    const a = s.store.getArtifact(principal, result.artifacts[0].artifact_id);
    const mesh = JSON.parse(s.store.readBlob(a.hash).toString()).meshes[0];
    assert.equal(mesh.field_semantics, "exact_sdf");
    assert.equal(mesh.quality, "preview_only");
    assert.ok(mesh.triangles.length > 1000);
    assert.ok(mesh.pruned_cells > 0);
  } finally {
    await env.close();
  }
});

test("imported originals remain hash-bound in private export packages and mismatched proof blocks export", async () => {
  const env = setup(),
    s = env.service;
  try {
    const sourceModel = await importFixture(s, sphere);
    const step = await finish(
      s,
      call(s, "cad_export", {
        model_id: sourceModel.model_id,
        revision: sourceModel.revision,
        format: "step",
        idempotency_key: id("step"),
      }),
    );
    const original = step.artifacts.find(
      (a: any) => a.manifest.filename === "model.step",
    );
    const importedIR = {
      schema_version: "1",
      unit: "mm",
      features: [
        {
          id: "imported-part",
          semantic_name: "Fremdteil",
          kind: "imported",
          parameters: {},
          construction: {
            operator: "imported",
            artifact_id: original.artifact_id,
            format: "step",
            source_unit: "mm",
          },
        },
      ],
      outputs: ["imported-part"],
    };
    const model = await importFixture(s, importedIR);
    const exported = call(s, "cad_export", {
      model_id: model.model_id,
      revision: model.revision,
      format: "ir",
      idempotency_key: id("ir"),
    });
    const manifest = JSON.parse(
      s.store.readBlob(exported.package_manifest.hash).toString(),
    );
    assert.equal(manifest.source_assets.length, 1);
    assert.equal(manifest.source_assets[0].sha256, original.hash);
    assert.equal(
      manifest.semantic_sidecar.imported_source_assets_required,
      true,
    );
    const stored = s.store.get(
      "SELECT id,validation FROM transactions WHERE committed_revision=?",
      model.revision,
    );
    const proof = JSON.parse(stored.validation);
    proof.ir_hash = "0".repeat(64);
    s.store.run(
      "UPDATE transactions SET validation=? WHERE id=?",
      JSON.stringify(proof),
      stored.id,
    );
    const denied = s.call(principal, "cad_export", {
      model_id: model.model_id,
      revision: model.revision,
      format: "ir",
      idempotency_key: id("invalid-proof"),
    });
    assert.equal(denied.status, "failed");
    assert.equal(denied.errors[0].code, "INTEGRITY_FAILURE");
  } finally {
    await env.close();
  }
});
