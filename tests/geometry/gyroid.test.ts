import test from "node:test";
import assert from "node:assert/strict";
import {
  fieldContract,
  fieldValueUnit,
} from "../../packages/compiler/index.js";
import { ModelIR } from "../../packages/semantic-ir/schema.js";
import { setup, call, finish, importFixture } from "../helpers.js";
import { id } from "../../packages/semantic-ir/hash.js";

const gyroid = {
  op: "gyroid",
  period: "2",
  origin: ["0", "0", "0"],
  threshold: "0.1",
};
test("dimensionless lattice fields require explicit value-unit conversion before length-valued CSG and offsets", () => {
  assert.equal(fieldValueUnit(gyroid), "dimensionless");
  assert.deepEqual(fieldContract(gyroid), {
    semantics: "general_implicit",
    lipschitz: 11,
  });
  const sphere = { op: "sphere", center: ["0", "0", "0"], radius: "2" };
  assert.throws(
    () => fieldContract({ op: "intersection", a: sphere, b: gyroid }),
    (e: any) => e.code === "UNIT_MISMATCH",
  );
  assert.throws(
    () => fieldContract({ op: "offset", source: gyroid, distance: "1" }),
    (e: any) => e.code === "UNIT_MISMATCH",
  );
  const converted = {
    op: "convert_field_unit",
    source: gyroid,
    to: "length",
    reference_length: { value: "0.5", unit: "mm" },
  };
  assert.equal(fieldValueUnit(converted), "length");
  assert.deepEqual(fieldContract(converted), {
    semantics: "general_implicit",
    lipschitz: 5.5,
  });
  assert.equal(
    fieldContract({ op: "intersection", a: sphere, b: converted }).semantics,
    "general_implicit",
  );
  assert.throws(
    () => fieldContract({ ...converted, to: "dimensionless" }),
    (e: any) => e.code === "UNIT_MISMATCH",
  );
  assert.throws(
    () =>
      fieldContract({
        ...converted,
        reference_length: { value: "1", unit: "rad" },
      }),
    (e: any) => e.code === "UNIT_MISMATCH",
  );
  let extreme: any = gyroid;
  for (let i = 0; i < 8; i++)
    extreme = {
      op: "convert_field_unit",
      source: extreme,
      to: i % 2 === 0 ? "length" : "dimensionless",
      reference_length: {
        value: i % 2 === 0 ? "1000000" : "0.00001",
        unit: "mm",
      },
    };
  assert.throws(
    () => fieldContract(extreme),
    (e: any) => e.code === "BUDGET_EXCEEDED",
  );
});

test("lattice preview and VDB preserve separate coordinate and field-value units through real workers", async () => {
  const env = setup(),
    s = env.service;
  try {
    const ir = ModelIR.parse({
      schema_version: "1",
      unit: "mm",
      profile: "render_surface",
      features: [
        {
          id: "lattice",
          semantic_name: "Periodisches Gyroidgitter",
          kind: "implicit_lattice",
          authoritative_representation: "implicit",
          parameters: {},
          construction: {
            operator: "field",
            expression: gyroid,
            domain: { min: ["0", "0", "0"], max: ["4", "4", "4"] },
            cell_size: { value: "0.5", unit: "mm" },
          },
        },
      ],
      outputs: ["lattice"],
    });
    const m = await importFixture(s, ir),
      binding = { model_id: m.model_id, revision: m.revision };
    const inspected = call(s, "cad_inspect", {
      ...binding,
      feature_id: "lattice",
    });
    assert.equal(inspected.known_facts.value_unit, "dimensionless");
    assert.equal(inspected.known_facts.field_semantics, "general_implicit");
    const preview = await finish(
      s,
      call(s, "cad_render", { ...binding, idempotency_key: id("render") }),
    );
    const mesh = JSON.parse(
      s.store.readBlob(preview.artifacts[0].hash).toString(),
    ).meshes[0];
    assert.equal(mesh.value_unit, "dimensionless");
    assert.equal(mesh.field_semantics, "general_implicit");
    assert.ok(mesh.triangles.length > 100);
    const exported = await finish(
      s,
      call(s, "cad_export", {
        ...binding,
        format: "vdb",
        idempotency_key: id("vdb"),
      }),
    );
    const vdb = exported.artifacts.find(
      (a: any) => a.manifest.filename === "model.vdb",
    );
    assert.equal(vdb.manifest.roundtrip.value_unit, "dimensionless");
    assert.equal(vdb.manifest.roundtrip.sample_roundtrip_error, 0);
    assert.equal(vdb.manifest.roundtrip.continuous_distance_certificate, null);
  } finally {
    await env.close();
  }
});
