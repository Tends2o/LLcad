import test from "node:test";
import assert from "node:assert/strict";
import { ModelIR, Patch } from "../../packages/semantic-ir/schema.js";
import { compile, applyPatch } from "../../packages/compiler/index.js";
import { compileStructure } from "../../packages/compiler/structure.js";
import { hash, id } from "../../packages/semantic-ir/hash.js";
import { housing, organic } from "../../scripts/fixtures.js";
import { setup, call, finish, importFixture, principal } from "../helpers.js";

function framedHousing() {
  return ModelIR.parse({
    ...housing,
    features: housing.features.map((f) => ({
      ...f,
      owner_part: "part-housing",
      local_frame: "frame-housing",
    })),
    structure: {
      project: { id: "project-device", semantic_name: "Geräteprojekt" },
      frames: [
        {
          id: "frame-mount",
          semantic_name: "Einbaurahmen",
          parent: "world",
          translation: ["100000", "-200000", "300000"],
          axis: ["1", "2", "3"],
          angle: { value: "30", unit: "deg" },
        },
        {
          id: "frame-housing",
          semantic_name: "Gehäuserahmen",
          parent: "frame-mount",
          translation: ["5", "0", "0"],
          axis: ["0", "1", "0"],
          angle: { value: "90", unit: "deg" },
        },
      ],
      assemblies: [
        {
          id: "assembly-mount",
          semantic_name: "Einbaugruppe",
          local_frame: "frame-mount",
        },
      ],
      parts: [
        {
          id: "part-housing",
          semantic_name: "Dichtungsgehäuse",
          assembly: "assembly-mount",
          local_frame: "frame-housing",
          authoritative_representation: "brep",
          outputs: housing.outputs,
        },
      ],
    },
  });
}

test("structure validates hierarchy, one authority per part, frame cycles and declared part outputs", () => {
  const base = framedHousing();
  assert.equal(compile(base).features.length, 3);
  for (const mutate of [
    (ir: any) => (ir.structure.frames[0].parent = "frame-housing"),
    (ir: any) => (ir.structure.frames[0].axis = ["0", "0", "0"]),
    (ir: any) => (ir.structure.frames[0].angle.unit = "mm"),
    (ir: any) => (ir.structure.frames[0].translation[0] = "1000001"),
    (ir: any) =>
      (ir.structure.parts[0].authoritative_representation = "implicit"),
    (ir: any) => (ir.structure.parts[0].outputs = []),
    (ir: any) => (ir.features[0].owner_part = "missing"),
    (ir: any) => (ir.features[0].local_frame = "world"),
    (ir: any) =>
      (ir.structure.assemblies[0].parent_assembly = "assembly-mount"),
    (ir: any) => (ir.structure.frames[0].id = "world"),
  ]) {
    const ir = structuredClone(base);
    mutate(ir);
    assert.throws(() => compile(ir));
  }
  const deep = framedHousing();
  for (let i = 0; i < 18; i++)
    deep.structure!.frames.push({
      id: "deep-" + i,
      semantic_name: "tief",
      parent: i ? "deep-" + (i - 1) : "world",
      translation: ["0", "0", "0"],
      axis: ["0", "0", "1"],
      angle: { value: "0", unit: "deg" },
    });
  assert.throws(
    () => compile(deep),
    (e: any) => e.code === "BUDGET_EXCEEDED",
  );
  const old = compile(base),
    modified = structuredClone(base);
  modified.structure!.frames[0].translation[0] = "100001";
  const next = compile(modified);
  assert.ok(old.features.every((f) => f.cache_key !== next.hashes[f.id]));
  const protectedIR = structuredClone(base);
  protectedIR.constraints.push({
    id: "protect-base",
    kind: "protected_feature",
    feature_id: "feat-base",
  });
  assert.throws(
    () =>
      applyPatch(
        protectedIR,
        Patch.parse({
          model_id: "model",
          base_revision: "revision",
          idempotency_key: id("structure"),
          operations: [
            {
              op: "set_structure",
              expected_hash: hash(base.structure),
              structure: modified.structure,
            },
          ],
        }),
      ),
    (e: any) => e.code === "OUT_OF_SCOPE",
  );
});

test("cross-frame CSG converts referenced part geometry and retains the measured cavity volume", async () => {
  const env = setup(),
    s = env.service;
  try {
    const ir = ModelIR.parse({
      schema_version: "1",
      unit: "mm",
      features: [
        {
          id: "base",
          semantic_name: "Grundblock",
          kind: "box",
          owner_part: "part-body",
          local_frame: "body-frame",
          parameters: {
            width: { value: "20", unit: "mm" },
            depth: { value: "20", unit: "mm" },
            height: { value: "20", unit: "mm" },
            x: { value: "-10", unit: "mm" },
            y: { value: "-10", unit: "mm" },
            z: { value: "-10", unit: "mm" },
          },
          construction: { operator: "box" },
        },
        {
          id: "cutter",
          semantic_name: "Kugelausschnitt",
          kind: "sphere",
          owner_part: "part-cutter",
          local_frame: "cutter-frame",
          parameters: { radius: { value: "2", unit: "mm" } },
          construction: { operator: "sphere" },
        },
        {
          id: "body",
          semantic_name: "Ausgeschnittener Block",
          kind: "difference",
          owner_part: "part-body",
          local_frame: "body-frame",
          parameters: {},
          construction: { operator: "difference" },
          depends_on: ["base", "cutter"],
        },
      ],
      outputs: ["body"],
      structure: {
        project: { id: "project", semantic_name: "Hohlraum" },
        assemblies: [],
        frames: [
          {
            id: "body-frame",
            semantic_name: "Blocklage",
            parent: "world",
            translation: ["1000", "0", "0"],
            axis: ["0", "0", "1"],
            angle: { value: "0", unit: "deg" },
          },
          {
            id: "cutter-frame",
            semantic_name: "Werkzeuglage",
            parent: "world",
            translation: ["1005", "0", "0"],
            axis: ["1", "0", "0"],
            angle: { value: "90", unit: "deg" },
          },
        ],
        parts: [
          {
            id: "part-body",
            semantic_name: "Block",
            local_frame: "body-frame",
            authoritative_representation: "brep",
            outputs: ["body"],
          },
          {
            id: "part-cutter",
            semantic_name: "Werkzeug",
            local_frame: "cutter-frame",
            authoritative_representation: "brep",
            outputs: ["cutter"],
          },
        ],
      },
    });
    const m = await importFixture(s, ir),
      facts = call(s, "cad_inspect", {
        model_id: m.model_id,
        revision: m.revision,
        feature_id: "body",
        face_limit: 16,
      });
    assert.ok(
      Math.abs(facts.known_facts.volume - (8000 - (32 * Math.PI) / 3)) < 1e-7,
    );
    const cavity = facts.face_page.faces.find((f: any) =>
      f.origins.some((o: any) => o.feature_id === "cutter"),
    );
    assert.ok(cavity);
    assert.ok(Math.abs(cavity.center[0] - 1005) < 1e-8);
    const rendered = await finish(
      s,
      call(s, "cad_render", {
        model_id: m.model_id,
        revision: m.revision,
        idempotency_key: id("render"),
      }),
    );
    assert.equal(rendered.metrics.cache_hits, 3);
  } finally {
    await env.close();
  }
});

test("mixed parts report world bounds and cannot silently lose implicit geometry in native export", async () => {
  const env = setup(),
    s = env.service;
  try {
    const ir = ModelIR.parse({
      ...organic,
      features: [
        ...organic.features.map((f) => ({ ...f, owner_part: "part-field" })),
        {
          id: "ball",
          semantic_name: "Kugel",
          kind: "sphere",
          owner_part: "part-cad",
          parameters: {
            radius: { value: "1", unit: "mm" },
            x: { value: "20", unit: "mm" },
          },
          construction: { operator: "sphere" },
        },
      ],
      outputs: ["organic", "ball"],
      structure: {
        project: { id: "project", semantic_name: "Gemischt" },
        assemblies: [],
        frames: [],
        parts: [
          {
            id: "part-field",
            semantic_name: "Feld",
            local_frame: "world",
            authoritative_representation: "implicit",
            outputs: ["organic"],
          },
          {
            id: "part-cad",
            semantic_name: "CAD",
            local_frame: "world",
            authoritative_representation: "brep",
            outputs: ["ball"],
          },
        ],
      },
    });
    const m = await importFixture(s, ir),
      model = call(s, "cad_get_model", {
        model_id: m.model_id,
        revision: m.revision,
      });
    assert.equal(model.measurements.volume, null);
    assert.deepEqual(model.measurements.bounds, [-7, -7, -7, 21, 7, 7]);
    const rejected = call(s, "cad_export", {
      model_id: m.model_id,
      revision: m.revision,
      format: "step",
      idempotency_key: id("step"),
    });
    const job = await s.jobs.wait(principal, rejected.job_id);
    assert.equal(job.status, "failed");
    assert.equal(job.error.code, "OUT_OF_SCOPE");
    const glb = await finish(
      s,
      call(s, "cad_export", {
        model_id: m.model_id,
        revision: m.revision,
        format: "glb",
        idempotency_key: id("glb"),
      }),
    );
    assert.ok(
      glb.artifacts.some((a: any) => a.manifest?.filename === "model.glb"),
    );
    assert.equal(s.store.model(principal, m.model_id).head, m.revision);
  } finally {
    await env.close();
  }
});

test("local housing measurements remain precise at large world coordinates and selections survive stored frame geometry", async () => {
  const env = setup(),
    s = env.service;
  try {
    const ir = framedHousing(),
      m = await importFixture(s, ir);
    const structure = call(s, "cad_structure", {
      model_id: m.model_id,
      revision: m.revision,
      kind: "part",
      query: "Dichtungsgehäuse",
    });
    assert.equal(structure.entries.length, 1);
    assert.equal(structure.entries[0].feature_count, 3);
    assert.equal(structure.structure_hash, hash(ir.structure));
    assert.ok(structure.entries[0].world_bounds[0] > 99900);
    assert.equal(
      call(s, "cad_find", {
        model_id: m.model_id,
        revision: m.revision,
        owner_part: "part-housing",
        query: "Dichtungsnut",
      }).matches[0].feature_id,
      "feat-groove-07",
    );
    const before = call(s, "cad_inspect", {
      model_id: m.model_id,
      revision: m.revision,
      feature_id: "feat-groove-07",
      face_limit: 16,
    });
    assert.equal(before.known_facts.dimension_frame, "frame-housing");
    assert.ok(Math.abs(before.known_facts.dimensions.depth - 0.8) < 1e-8);
    const inner = before.face_page.faces.find((f: any) =>
      f.origins.some((o: any) => o.role === "inner_wall"),
    );
    assert.ok(inner);
    const selection = call(s, "cad_inspect", {
      model_id: m.model_id,
      revision: m.revision,
      feature_id: "feat-groove-07",
      face_id: inner.face_id,
    });
    const patch = {
      model_id: m.model_id,
      base_revision: m.revision,
      idempotency_key: id("plan"),
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
    call(s, "cad_plan_edit", patch);
    const draft = call(s, "cad_apply_patch", {
      ...patch,
      idempotency_key: id("apply"),
    });
    await finish(s, draft);
    const binding = {
      model_id: m.model_id,
      base_revision: m.revision,
      transaction_id: draft.transaction_id,
    };
    const validation = await finish(
      s,
      call(s, "cad_validate", { ...binding, idempotency_key: id("validate") }),
    );
    assert.equal(validation.status, "checks_passed_within_profile");
    const commit = call(s, "cad_commit", {
      ...binding,
      validation_digest: validation.digest,
      idempotency_key: id("commit"),
    });
    const after = call(s, "cad_inspect", {
      model_id: m.model_id,
      revision: commit.revision,
      selection_handle: selection.selection_handle,
      rebind: true,
    });
    assert.ok(Math.abs(after.known_facts.dimensions.depth - 0.82) < 1e-8);
    assert.ok(
      Math.abs(after.known_facts.dimensions.remaining_wall - 2.18) < 1e-8,
    );
    const rendered = await finish(
      s,
      call(s, "cad_render", {
        model_id: m.model_id,
        revision: commit.revision,
        idempotency_key: id("render"),
      }),
    );
    assert.equal(rendered.metrics.cache_hits, 3);
    const old = s.store.revision(principal, m.model_id, m.revision),
      next = s.store.revision(principal, m.model_id, commit.revision);
    assert.equal(
      old.geometry.facts["feat-base"].geometry_hash,
      next.geometry.facts["feat-base"].geometry_hash,
    );
    assert.deepEqual(old.ir.structure, next.ir.structure);
    const denied = s.call({ ...principal, user: "other" }, "cad_structure", {
      model_id: m.model_id,
      kind: "project",
    });
    assert.equal(denied.errors[0].code, "ACCESS_DENIED");
  } finally {
    await env.close();
  }
});

test("a structure edit moves only its frame subtree and keeps another part protected", async () => {
  const env = setup(),
    s = env.service;
  try {
    const ir = ModelIR.parse({
      schema_version: "1",
      unit: "mm",
      features: ["left", "right"].map((name, i) => ({
        id: "shape-" + name,
        semantic_name: name,
        kind: "sphere",
        owner_part: "part-" + name,
        local_frame: "frame-" + name,
        parameters: { radius: { value: "1", unit: "mm" } },
        construction: { operator: "sphere" },
      })),
      outputs: ["shape-left", "shape-right"],
      constraints: [
        {
          id: "protect-right",
          kind: "protected_feature",
          feature_id: "shape-right",
        },
      ],
      structure: {
        project: { id: "project", semantic_name: "Gerät" },
        assemblies: [],
        frames: ["left", "right"].map((name, i) => ({
          id: "frame-" + name,
          semantic_name: name,
          parent: "world",
          translation: [String(i * 20), "0", "0"],
          axis: ["0", "0", "1"],
          angle: { value: "0", unit: "deg" },
        })),
        parts: ["left", "right"].map((name) => ({
          id: "part-" + name,
          semantic_name: name,
          local_frame: "frame-" + name,
          authoritative_representation: "brep",
          outputs: ["shape-" + name],
        })),
      },
    });
    const m = await importFixture(s, ir),
      structure = structuredClone(ir.structure!);
    structure.frames[0].translation[1] = "5";
    const patch = {
      model_id: m.model_id,
      base_revision: m.revision,
      idempotency_key: id("plan"),
      operations: [
        { op: "set_structure", expected_hash: hash(ir.structure), structure },
      ],
    };
    const plan = call(s, "cad_plan_edit", patch);
    assert.deepEqual(plan.dirty_features, ["shape-left"]);
    const draft = call(s, "cad_apply_patch", {
      ...patch,
      idempotency_key: id("apply"),
    });
    await finish(s, draft);
    const binding = {
      model_id: m.model_id,
      base_revision: m.revision,
      transaction_id: draft.transaction_id,
    };
    const validation = await finish(
      s,
      call(s, "cad_validate", { ...binding, idempotency_key: id("validate") }),
    );
    assert.equal(validation.status, "checks_passed_within_profile");
    const committed = call(s, "cad_commit", {
      ...binding,
      validation_digest: validation.digest,
      idempotency_key: id("commit"),
    });
    const old = s.store.revision(principal, m.model_id, m.revision),
      next = s.store.revision(principal, m.model_id, committed.revision);
    assert.equal(
      old.geometry.facts["shape-right"].geometry_hash,
      next.geometry.facts["shape-right"].geometry_hash,
    );
    assert.ok(
      Math.abs(
        next.geometry.facts["shape-left"].bounds[1] -
          old.geometry.facts["shape-left"].bounds[1] -
          5,
      ) < 1e-8,
    );
    assert.deepEqual(
      call(s, "cad_compare", {
        model_id: m.model_id,
        from_revision: m.revision,
        to_revision: committed.revision,
      }).changed_features,
      ["shape-left"],
    );
    const context = call(s, "cad_inspect", {
      model_id: m.model_id,
      revision: committed.revision,
      feature_id: "shape-left",
    });
    const withChild = structuredClone(next.ir.structure!);
    withChild.frames.push({
      id: "child-frame",
      semantic_name: "Lokales Detail",
      parent: "frame-left",
      translation: ["0", "0", "3"],
      axis: ["0", "0", "1"],
      angle: { value: "0", unit: "deg" },
    });
    const contextPatch = {
      model_id: m.model_id,
      base_revision: committed.revision,
      idempotency_key: id("context-plan"),
      operations: [
        {
          op: "set_structure",
          expected_hash: hash(next.ir.structure),
          structure: withChild,
        },
        {
          op: "set_feature_context",
          feature_id: "shape-left",
          expected_hash: context.context_hash,
          owner_part: "part-left",
          local_frame: "child-frame",
        },
      ],
    };
    call(s, "cad_plan_edit", contextPatch);
    const contextDraft = call(s, "cad_apply_patch", {
      ...contextPatch,
      idempotency_key: id("context-apply"),
    });
    const moved = await finish(s, contextDraft);
    assert.ok(
      Math.abs(
        moved.measurements.bounds[2] - old.geometry.aggregate.bounds[2],
      ) < 1e-8,
    );
    const candidate = s.store.transaction(
      principal,
      contextDraft.transaction_id,
    );
    assert.ok(
      Math.abs(candidate.result.facts["shape-left"].bounds[2] - 2) < 1e-8,
    );
    const checked = await finish(
      s,
      call(s, "cad_validate", {
        model_id: m.model_id,
        base_revision: committed.revision,
        transaction_id: contextDraft.transaction_id,
        idempotency_key: id("context-check"),
      }),
    );
    assert.equal(checked.status, "checks_passed_within_profile");
    call(s, "cad_commit", {
      model_id: m.model_id,
      base_revision: committed.revision,
      transaction_id: contextDraft.transaction_id,
      validation_digest: checked.digest,
      idempotency_key: id("context-commit"),
    });
  } finally {
    await env.close();
  }
});

test("an implicit part retains local protection while its preview and VDB carry the world frame", async () => {
  const env = setup(),
    s = env.service;
  try {
    const ir = ModelIR.parse({
      ...organic,
      features: organic.features.map((f) => ({
        ...f,
        local_frame: "field-frame",
        owner_part: "part-field",
      })),
      constraints: organic.constraints.map((c) => ({
        ...c,
        local_frame: "field-frame",
      })),
      structure: {
        project: { id: "project", semantic_name: "Organik" },
        assemblies: [],
        frames: [
          {
            id: "field-frame",
            semantic_name: "Lage",
            parent: "world",
            translation: ["100", "5", "2"],
            axis: ["0", "0", "1"],
            angle: { value: "90", unit: "deg" },
          },
        ],
        parts: [
          {
            id: "part-field",
            semantic_name: "Kappe",
            local_frame: "field-frame",
            authoritative_representation: "implicit",
            outputs: ["organic"],
          },
        ],
      },
    });
    const m = await importFixture(s, ir),
      inspect = call(s, "cad_inspect", {
        model_id: m.model_id,
        revision: m.revision,
        feature_id: "organic",
      });
    assert.deepEqual(inspect.known_facts.local_bounds, [-7, -7, -7, 7, 7, 7]);
    assert.ok(Math.abs(inspect.known_facts.bounds[0] - 93) < 1e-10);
    const render = await finish(
      s,
      call(s, "cad_render", {
        model_id: m.model_id,
        revision: m.revision,
        idempotency_key: id("render"),
      }),
    );
    const artifact = render.artifacts.find(
      (a: any) => a.mime === "application/json",
    );
    const mesh = JSON.parse(
      s.store
        .readBlob(s.store.getArtifact(principal, artifact.artifact_id).hash)
        .toString(),
    ).meshes[0];
    assert.ok(
      mesh.vertices.every(
        (p: number[]) =>
          Math.abs(p[0] - 100) < 5.1 &&
          Math.abs(p[1] - 5) < 5.1 &&
          Math.abs(p[2] - 2) < 5.1,
      ),
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
    const vdb = exported.artifacts.find(
      (a: any) => a.manifest?.filename === "model.vdb",
    );
    assert.ok(vdb);
    assert.equal(vdb.manifest.roundtrip.domain_frame, "field-frame");
    assert.equal(vdb.manifest.roundtrip.transform_roundtrip_error, 0);
    assert.equal(
      compileStructure(ir).structure.parts[0].authoritative_representation,
      "implicit",
    );
  } finally {
    await env.close();
  }
});
