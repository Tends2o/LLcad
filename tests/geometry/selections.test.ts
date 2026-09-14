import test from "node:test";
import assert from "node:assert/strict";
import { setup, call, finish, importFixture, principal } from "../helpers.js";
import { housing, sphere } from "../../scripts/fixtures.js";
import { id } from "../../packages/semantic-ir/hash.js";
import { ModelIR } from "../../packages/semantic-ir/schema.js";
import { ModelService } from "../../packages/model-service/index.js";

function inspect(s: ModelService, m: any, feature = "feat-hole-01") {
  return call(s, "cad_inspect", {
    model_id: m.model_id,
    revision: m.revision,
    feature_id: feature,
    face_limit: 16,
  });
}
function role(detail: any, name: string) {
  const found = detail.face_page.faces.filter((f: any) =>
    f.origins.some((o: any) => o.role === name),
  );
  assert.equal(found.length, 1, `Expected one face for ${name}`);
  return found[0];
}
function select(s: ModelService, m: any, feature: string, face: any) {
  return call(s, "cad_inspect", {
    model_id: m.model_id,
    revision: m.revision,
    feature_id: feature,
    face_id: face.face_id,
  });
}
async function edit(
  s: ModelService,
  m: any,
  feature: string,
  parameter: string,
  value: string,
  handle?: string,
) {
  const detail = inspect(s, m, feature);
  const draft = call(s, "cad_apply_patch", {
    model_id: m.model_id,
    base_revision: m.revision,
    idempotency_key: id("edit"),
    ...(handle ? { selection_handle: handle } : {}),
    operations: [
      {
        op: "set_parameter",
        feature_id: feature,
        parameter,
        expected: detail.parameters[parameter],
        value: { value, unit: "mm" },
      },
    ],
  });
  await finish(s, draft);
  const validation = await finish(
    s,
    call(s, "cad_validate", {
      model_id: m.model_id,
      base_revision: m.revision,
      transaction_id: draft.transaction_id,
      idempotency_key: id("validate"),
    }),
  );
  return call(s, "cad_commit", {
    model_id: m.model_id,
    base_revision: m.revision,
    transaction_id: draft.transaction_id,
    validation_digest: validation.digest,
    idempotency_key: id("commit"),
  });
}

test("a rendered groove face selects its actual source, survives cache/restart and explicitly rebinds after a 20 µm edit", async () => {
  const env = setup();
  let s = env.service;
  try {
    const m = await importFixture(s, housing);
    const inner = role(inspect(s, m), "inner_wall");
    const selection = select(s, m, "feat-hole-01", inner);
    assert.deepEqual(selection.selected_entities, ["feat-groove-07"]);
    assert.equal(selection.selected_face.geometry_feature_id, "feat-hole-01");
    assert.ok(
      Math.abs(selection.selected_face.area - 2 * Math.PI * 9.4 * 0.8) < 1e-7,
    );

    const rendered = await finish(
      s,
      call(s, "cad_render", {
        model_id: m.model_id,
        revision: m.revision,
        idempotency_key: id("render"),
      }),
    );
    const artifact = rendered.artifacts.find(
      (a: any) => a.mime === "application/json",
    );
    assert.ok(artifact, JSON.stringify(rendered));
    const preview = JSON.parse(
      s.store
        .readBlob(s.store.getArtifact(principal, artifact.artifact_id).hash)
        .toString(),
    );
    const mesh = preview.meshes[0];
    const range = mesh.face_ranges.find(
      (r: any) => r.face_id === inner.face_id,
    );
    assert.ok(range.triangle_count > 0);
    assert.equal(
      mesh.face_ranges.reduce((n: number, r: any) => n + r.triangle_count, 0),
      mesh.triangles.length,
    );
    assert.ok(rendered.metrics.cache_hits >= 3);

    const changed = await edit(
      s,
      m,
      "feat-groove-07",
      "depth",
      "0.82",
      selection.selection_handle,
    );
    const binding = {
      model_id: m.model_id,
      revision: changed.revision,
      selection_handle: selection.selection_handle,
    };
    assert.equal(
      s.call(principal, "cad_inspect", binding).errors[0].code,
      "STALE_REVISION",
    );
    const rebound = call(s, "cad_inspect", { ...binding, rebind: true });
    assert.notEqual(rebound.selection_handle, selection.selection_handle);
    assert.notEqual(rebound.selected_face.face_id, inner.face_id);
    assert.equal(rebound.parameters.depth.value, "0.82");
    assert.ok(
      Math.abs(rebound.selected_face.area - 2 * Math.PI * 9.4 * 0.82) < 1e-7,
    );

    await s.close();
    s = new ModelService(env.dir);
    const restored = call(s, "cad_inspect", {
      model_id: m.model_id,
      revision: changed.revision,
      selection_handle: rebound.selection_handle,
    });
    assert.equal(restored.selected_face.face_id, rebound.selected_face.face_id);
    const later = await edit(
      s,
      changed,
      "feat-groove-07",
      "depth",
      "0.84",
      restored.selection_handle,
    );
    assert.equal(
      inspect(s, later, "feat-groove-07").parameters.depth.value,
      "0.84",
    );
    assert.equal(
      inspect(s, m, "feat-groove-07").parameters.depth.value,
      "0.80",
    );
  } finally {
    await s.close();
    const { rmSync } = await import("node:fs");
    rmSync(env.dir, { recursive: true, force: true });
  }
});

test("split faces and a later merge never silently resurrect a selection", async () => {
  const env = setup(),
    s = env.service;
  try {
    const ir = ModelIR.parse({
      schema_version: "1",
      unit: "mm",
      profile: "precision_cad",
      features: [
        {
          id: "base",
          semantic_name: "Platte",
          kind: "box",
          parameters: {
            width: { value: "20", unit: "mm" },
            depth: { value: "20", unit: "mm" },
            height: { value: "4", unit: "mm" },
          },
          construction: { operator: "box" },
          depends_on: [],
        },
        {
          id: "slot",
          semantic_name: "Tasche",
          kind: "pocket",
          parameters: Object.fromEntries(
            Object.entries({
              width: "2",
              length: "2",
              depth: "1",
              x: "10",
              y: "10",
              z: "4",
            }).map(([k, value]) => [k, { value, unit: "mm" }]),
          ),
          construction: { operator: "pocket" },
          depends_on: ["base"],
        },
      ],
      outputs: ["slot"],
    });
    const m = await importFixture(s, ir);
    const top = select(s, m, "slot", role(inspect(s, m, "slot"), "top"));
    assert.deepEqual(top.selected_entities, ["base"]);
    const split = await edit(s, m, "slot", "length", "20");
    const rebind = (revision: string, handle = top.selection_handle) =>
      s.call(principal, "cad_inspect", {
        model_id: m.model_id,
        revision,
        selection_handle: handle,
        rebind: true,
      });
    assert.equal(rebind(split.revision).errors[0].code, "AMBIGUOUS_SELECTION");
    const fragments = inspect(s, split, "slot").face_page.faces.filter(
      (f: any) => f.origins.some((o: any) => o.role === "top"),
    );
    assert.equal(fragments.length, 2);
    const oneFragment = select(s, split, "slot", fragments[0]);
    const merged = await edit(s, split, "slot", "length", "2");
    assert.equal(rebind(merged.revision).errors[0].code, "AMBIGUOUS_SELECTION");
    assert.equal(
      rebind(merged.revision, oneFragment.selection_handle).errors[0].code,
      "AMBIGUOUS_SELECTION",
    );
    assert.equal(inspect(s, m, "slot").parameters.length.value, "2");
  } finally {
    await env.close();
  }
});

test("face bindings enforce ownership, expiry, exact revisions and operation scope", async () => {
  const env = setup(),
    s = env.service;
  try {
    const m = await importFixture(s, housing);
    const selected = select(
      s,
      m,
      "feat-hole-01",
      role(inspect(s, m), "inner_wall"),
    );
    const args = {
      model_id: m.model_id,
      revision: m.revision,
      selection_handle: selected.selection_handle,
    };
    assert.equal(
      s.call({ ...principal, user: "bob" }, "cad_inspect", args).errors[0].code,
      "ACCESS_DENIED",
    );
    const other = await importFixture(s, sphere);
    assert.equal(
      s.call(principal, "cad_inspect", {
        ...args,
        model_id: other.model_id,
        revision: other.revision,
        rebind: true,
      }).errors[0].code,
      "STALE_REVISION",
    );
    for (const operations of [
      [
        {
          op: "set_parameter",
          feature_id: "feat-base",
          parameter: "height",
          expected: { value: "3", unit: "mm" },
          value: { value: "4", unit: "mm" },
        },
      ],
      [{ op: "set_outputs", outputs: ["feat-base"] }],
    ]) {
      const result = s.call(principal, "cad_apply_patch", {
        model_id: m.model_id,
        base_revision: m.revision,
        selection_handle: selected.selection_handle,
        idempotency_key: id("denied"),
        operations,
      });
      assert.equal(
        result.errors[0].code,
        "OUT_OF_SCOPE",
        JSON.stringify(result),
      );
    }
    const first = call(s, "cad_inspect", {
      model_id: m.model_id,
      revision: m.revision,
      feature_id: "feat-hole-01",
      face_limit: 2,
    });
    const second = call(s, "cad_inspect", {
      model_id: m.model_id,
      revision: m.revision,
      feature_id: "feat-hole-01",
      face_limit: 2,
      face_offset: first.face_page.next_offset,
    });
    assert.equal(first.face_page.faces.length, 2);
    assert.notEqual(
      first.face_page.faces[0].face_id,
      second.face_page.faces[0].face_id,
    );
    assert.equal(
      s.call(principal, "cad_inspect", {
        model_id: m.model_id,
        feature_id: "feat-hole-01",
        face_id: selected.selected_face.face_id,
      }).errors[0].code,
      "INVALID_SCHEMA",
    );
    s.store.run(
      "UPDATE selections SET expires=0 WHERE id=?",
      selected.selection_handle,
    );
    assert.equal(
      s.call(principal, "cad_inspect", { ...args, rebind: true }).errors[0]
        .code,
      "STALE_REVISION",
    );
    s.store.run("DELETE FROM selections WHERE id=?", selected.selection_handle);
    assert.equal(
      s.store.get(
        "SELECT * FROM selection_faces WHERE selection_id=?",
        selected.selection_handle,
      ),
      undefined,
    );
  } finally {
    await env.close();
  }
});
