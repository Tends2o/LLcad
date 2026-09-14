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
