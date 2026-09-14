import test from "node:test";
import assert from "node:assert/strict";
import { compile, applyPatch } from "../../packages/compiler/index.js";
import { ModelIR, Patch } from "../../packages/semantic-ir/schema.js";
import { hash, id } from "../../packages/semantic-ir/hash.js";
import { setup, call, finish, importFixture, principal } from "../helpers.js";

function fixture(circular = true) {
  return ModelIR.parse({
    schema_version: "1",
    unit: "mm",
    features: [
      {
        id: "source",
        semantic_name: "Gemeinsame Kugelform",
        kind: "sphere",
        parameters: {
          radius: { value: "1", unit: "mm" },
          x: { value: "10", unit: "mm" },
        },
        construction: { operator: "sphere" },
      },
      {
        id: "many",
        semantic_name: circular
          ? "Vier Kugeln im Kreis"
          : "Vier Kugeln in Reihe",
        kind: "pattern",
        depends_on: ["source"],
        parameters: {
          count: { value: "4", unit: "1" },
          ...(circular
            ? { angle: { value: "360", unit: "deg" } }
            : { dx: { value: "5", unit: "mm" } }),
        },
        construction: circular
          ? {
              operator: "circular_pattern",
              axis: ["0", "0", "1"],
              origin: ["0", "0", "0"],
            }
          : { operator: "pattern" },
      },
    ],
    outputs: ["many"],
    constraints: [
      { id: "protect-source", kind: "protected_feature", feature_id: "source" },
    ],
  });
}

test("pattern contracts reject invalid variants and multiply nested instance budgets before native execution", () => {
  assert.equal(compile(fixture()).estimate.instances, 4);
  for (const change of [
    (f: any) => (f.construction.axis = ["0", "0", "0"]),
    (f: any) => (f.parameters.angle = { value: "360", unit: "mm" }),
    (f: any) => (f.parameters.angle.value = "0"),
    (f: any) => (f.parameters.count.value = "2.5"),
    (f: any) =>
      (f.construction.overrides = [{ index: 4, translation: ["0", "0", "1"] }]),
    (f: any) => (f.construction.overrides = [{ index: 1 }]),
    (f: any) => (f.construction.overrides = [{ index: 1, source: "missing" }]),
    (f: any) =>
      (f.construction.overrides = [
        { index: 1, translation: ["0", "0", "1"] },
        { index: 1, translation: ["1", "0", "0"] },
      ]),
    (f: any) =>
      (f.construction.overrides = [
        { index: 1, translation: ["1000001", "0", "0"] },
      ]),
  ]) {
    const ir = fixture();
    change(ir.features[1]);
    assert.throws(() => compile(ir));
  }
  for (const circular of [true, false]) {
    const ir = fixture(circular);
    ir.features[1].parameters.count.value = "100";
    ir.features.push({
      ...structuredClone(ir.features[1]),
      id: "nested",
      depends_on: ["many"],
    });
    ir.outputs = ["nested"];
    assert.throws(
      () => compile(ir),
      (e: any) => e.code === "BUDGET_EXCEEDED",
    );
  }
  const ir = fixture(),
    before = hash(ir.features[1].construction);
  assert.throws(() =>
    applyPatch(
      ir,
      Patch.parse({
        model_id: "model",
        base_revision: "revision",
        idempotency_key: id("patch"),
        operations: [
          {
            op: "set_pattern_occurrence",
            feature_id: "many",
            expected_hash: before,
            index: 8,
            override: { translation: ["0", "0", "1"] },
          },
        ],
      }),
    ),
  );
});

test("arbitrary-axis circular geometry survives repeated worker cache reads and native face selection", async () => {
  const env = setup(),
    s = env.service;
  try {
    const ir = fixture();
    ir.features[1].construction = {
      operator: "circular_pattern",
      axis: ["1", "2", "3"],
      origin: ["3", "-2", "5"],
    };
    ir.features[1].parameters.angle = { value: "-170", unit: "deg" };
    const m = await importFixture(s, ir);
    const inspect = () =>
      call(s, "cad_inspect", {
        model_id: m.model_id,
        revision: m.revision,
        feature_id: "many",
        face_limit: 16,
      });
    const original = inspect();
    assert.equal(original.face_page.faces.length, 4);
    assert.deepEqual(
      original.face_page.faces.map((f: any) => f.origins[0].occurrences),
      [0, 1, 2, 3].map((index) => [{ feature_id: "many", index }]),
    );
    for (let n = 0; n < 3; n++) {
      const rendered = await finish(
        s,
        call(s, "cad_render", {
          model_id: m.model_id,
          revision: m.revision,
          idempotency_key: id("cache-render"),
        }),
      );
      assert.ok(rendered.metrics.cache_hits >= 2);
      assert.deepEqual(inspect().face_page.faces, original.face_page.faces);
      const selected = call(s, "cad_inspect", {
        model_id: m.model_id,
        revision: m.revision,
        feature_id: "many",
        face_id: original.face_page.faces[2].face_id,
      });
      assert.deepEqual(selected.selected_face.origins[0].occurrences, [
        { feature_id: "many", index: 2 },
      ]);
    }
  } finally {
    await env.close();
  }
});

test("LLM tools can move, specialize and restore one circular occurrence while preserving the shared source", async () => {
  const env = setup(),
    s = env.service;
  try {
    let m = await importFixture(s, fixture());
    const original = s.store.revision(principal, m.model_id, m.revision);
    const inspect = () =>
      call(s, "cad_inspect", {
        model_id: m.model_id,
        revision: m.revision,
        feature_id: "many",
        face_limit: 16,
      });
    const initial = inspect();
    assert.equal(initial.pattern_contract.index_base, 0);
    assert.equal(initial.face_page.faces.length, 4);
    assert.ok(
      initial.available_edit_operations.some(
        (o: any) => o.op === "set_pattern_occurrence",
      ),
    );
    const selected = call(s, "cad_inspect", {
      model_id: m.model_id,
      revision: m.revision,
      feature_id: "many",
      face_id: initial.face_page.faces[1].face_id,
    });
    const commit = async (operations: any[]) => {
      const patch = {
        model_id: m.model_id,
        base_revision: m.revision,
        idempotency_key: id("plan"),
        operations,
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
        call(s, "cad_validate", {
          ...binding,
          idempotency_key: id("validate"),
        }),
      );
      assert.equal(validation.status, "checks_passed_within_profile");
      const result = call(s, "cad_commit", {
        ...binding,
        validation_digest: validation.digest,
        idempotency_key: id("commit"),
      });
      m = { ...m, ...result };
      return inspect();
    };
    let current = await commit([
      {
        op: "set_pattern_occurrence",
        feature_id: "many",
        expected_hash: initial.construction_hash,
        index: 1,
        override: { translation: ["0", "0", "2"] },
      },
    ]);
    for (const i of [0, 2, 3])
      assert.equal(
        current.face_page.faces[i].fingerprint,
        initial.face_page.faces[i].fingerprint,
      );
    assert.ok(
      Math.abs(
        current.face_page.faces[1].center[2] -
          initial.face_page.faces[1].center[2] -
          2,
      ) < 1e-9,
    );
    const rebound = call(s, "cad_inspect", {
      model_id: m.model_id,
      revision: m.revision,
      selection_handle: selected.selection_handle,
      rebind: true,
    });
    assert.equal(
      rebound.selected_face.fingerprint,
      current.face_page.faces[1].fingerprint,
    );
    assert.equal(
      s.call(principal, "cad_plan_edit", {
        model_id: m.model_id,
        base_revision: m.revision,
        idempotency_key: id("stale"),
        operations: [
          {
            op: "set_pattern_occurrence",
            feature_id: "many",
            expected_hash: initial.construction_hash,
            index: 1,
            override: null,
          },
        ],
      }).errors[0].code,
      "STALE_REVISION",
    );
    const variant = {
      ...structuredClone(original.ir.features[0]),
      id: "variant",
      semantic_name: "Einzelvariante mit größerem Radius",
    };
    variant.parameters.radius.value = "1.5";
    current = await commit([
      { op: "add_feature", feature: variant },
      {
        op: "set_pattern_occurrence",
        feature_id: "many",
        expected_hash: current.construction_hash,
        index: 1,
        override: { source: "variant", translation: ["0", "0", "2"] },
      },
    ]);
    assert.ok(
      Math.abs(
        current.known_facts.volume - ((3 + 1.5 ** 3) * 4 * Math.PI) / 3,
      ) < 1e-8,
    );
    for (const i of [0, 2, 3])
      assert.equal(
        current.face_page.faces[i].fingerprint,
        initial.face_page.faces[i].fingerprint,
      );
    const changed = s.store.revision(principal, m.model_id, m.revision);
    assert.deepEqual(
      changed.ir.features.find((f: any) => f.id === "source"),
      original.ir.features[0],
    );
    assert.equal(
      changed.geometry.facts.source.geometry_hash,
      original.geometry.facts.source.geometry_hash,
    );
    assert.deepEqual(
      changed.ir.features.find((f: any) => f.id === "many").depends_on,
      ["source", "variant"],
    );
    current = await commit([
      {
        op: "set_pattern_occurrence",
        feature_id: "many",
        expected_hash: current.construction_hash,
        index: 1,
        override: null,
      },
    ]);
    assert.deepEqual(
      current.face_page.faces.map((f: any) => f.fingerprint),
      initial.face_page.faces.map((f: any) => f.fingerprint),
    );
    assert.deepEqual(
      s.store
        .revision(principal, m.model_id, m.revision)
        .ir.features.find((f: any) => f.id === "many").depends_on,
      ["source"],
    );
    assert.deepEqual(
      s.store.revision(principal, m.model_id, original.id).geometry.aggregate,
      original.geometry.aggregate,
    );
  } finally {
    await env.close();
  }
});
