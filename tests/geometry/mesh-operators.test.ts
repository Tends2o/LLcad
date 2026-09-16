import test from "node:test";
import assert from "node:assert/strict";
import { setup, call, finish, importFixture, principal } from "../helpers.js";
import { ModelIR } from "../../packages/semantic-ir/schema.js";
import { id } from "../../packages/semantic-ir/hash.js";
import { meshSTL } from "../../scripts/mesh-fixtures.js";
import { organic } from "../../scripts/fixtures.js";
import { compile } from "../../packages/compiler/index.js";
const q = (value: string, unit = "mm") => ({ value, unit });

test("explicit mesh repair turns an open STL into a checked watertight body with a loss report", async () => {
  const env = setup(),
    s = env.service;
  try {
    const asset = s.store.artifact(
      principal,
      meshSTL(true),
      "model/stl",
      null,
      null,
      { source: "test" },
    );
    const ir = ModelIR.parse({
      schema_version: "1",
      unit: "mm",
      profile: "watertight_solid",
      features: [
        {
          id: "raw",
          semantic_name: "Offenes Netz",
          kind: "imported",
          authoritative_representation: "mesh",
          parameters: {},
          construction: {
            operator: "imported",
            format: "stl",
            source_unit: "mm",
            artifact_id: asset.artifact_id,
          },
        },
        {
          id: "repaired",
          semantic_name: "Gefülltes Netz",
          kind: "mesh_repair",
          authoritative_representation: "mesh",
          parameters: {},
          construction: { operator: "mesh_repair", fill_holes_max_edges: 8 },
          depends_on: ["raw"],
        },
      ],
      outputs: ["repaired"],
    });
    const m = await importFixture(s, ir);
    const facts = s.store.revision(principal, m.model_id).geometry.facts;
    assert.equal(facts.repaired.operation_report.filled_holes, 1);
    assert.equal(facts.repaired.mesh_quality.watertight_solid, true);
    assert.equal(facts.raw.mesh_quality.watertight_solid, false);
    assert.deepEqual(facts.repaired.operation_report.topology_changes, [
      "filled_holes",
    ]);
    const inspected = call(s, "cad_inspect", {
      model_id: m.model_id,
      feature_id: "repaired",
    });
    assert.equal(
      inspected.known_facts.operation_report.method,
      "explicit_indexed_mesh_repair",
    );
    const exported = await finish(
      s,
      call(s, "cad_export", {
        model_id: m.model_id,
        revision: m.revision,
        format: "stl",
        idempotency_key: id("stl"),
      }),
    );
    assert.ok(
      exported.artifacts.some((a: any) => a.manifest.filename === "model.stl"),
    );
  } finally {
    await env.close();
  }
});

test("tessellation and isosurface extraction are explicit conversions with reports; remesh, ARAP and primitive fits run on the result", async () => {
  const env = setup(),
    s = env.service;
  try {
    const ir = ModelIR.parse({
      schema_version: "1",
      unit: "mm",
      profile: "watertight_solid",
      features: [
        {
          id: "cyl",
          semantic_name: "Zylinder",
          kind: "cylinder",
          owner_part: "part-cad",
          parameters: { radius: q("3"), height: q("8") },
          construction: { operator: "cylinder" },
        },
        {
          id: "cyl-mesh",
          semantic_name: "Tesselliertes Netz",
          kind: "tessellate",
          owner_part: "part-mesh",
          authoritative_representation: "mesh",
          parameters: { deflection: q("0.02") },
          construction: { operator: "tessellate" },
          depends_on: ["cyl"],
        },
        {
          id: "remeshed",
          semantic_name: "Lokal neu vernetzt",
          kind: "remesh_region",
          owner_part: "part-mesh",
          authoritative_representation: "mesh",
          parameters: {
            radius: q("2"),
            target_edge: q("0.4"),
            iterations: q("2", "1"),
            x: q("3"),
            z: q("4"),
          },
          construction: { operator: "remesh_region" },
          depends_on: ["cyl-mesh"],
        },
        {
          id: "dented",
          semantic_name: "Lokal verformt",
          kind: "local_mesh_deform",
          owner_part: "part-mesh",
          authoritative_representation: "mesh",
          parameters: {
            radius: q("1.5"),
            iterations: q("4", "1"),
            x: q("3"),
            z: q("4"),
          },
          construction: {
            operator: "local_mesh_deform",
            handles: [
              { point: ["3", "0", "4"], displacement: ["-0.2", "0", "0"] },
            ],
          },
          depends_on: ["remeshed"],
        },
      ],
      outputs: ["dented"],
    });
    const m = await importFixture(s, ir);
    const facts = s.store.revision(principal, m.model_id).geometry.facts;
    const conversion = facts["cyl-mesh"].conversion_report;
    assert.equal(conversion.source_representation, "brep");
    assert.equal(conversion.target_representation, "mesh");
    assert.equal(conversion.certified_bound_or_null, null);
    assert.ok(conversion.measured_error <= 0.02 + 1e-9);
    assert.equal(
      conversion.measured_error_guarantee,
      "sampled_triangle_centroids",
    );
    assert.equal(facts["cyl-mesh"].mesh_quality.watertight_solid, true);
    assert.ok(
      facts.remeshed.operation_report.splits +
        facts.remeshed.operation_report.collapses >
        0,
    );
    assert.equal(
      facts.dented.operation_report.method,
      "as_rigid_as_possible_local_global_uniform_weights",
    );
    assert.ok(
      Math.abs(facts.dented.operation_report.max_displacement_mm - 0.2) < 1e-9,
    );
    assert.equal(facts.dented.mesh_quality.watertight_solid, true);
    const fit = await finish(
      s,
      call(s, "cad_measure", {
        model_id: m.model_id,
        feature_id: "cyl-mesh",
        metric: "fit_primitives",
        idempotency_key: id("fit"),
      }),
    );
    assert.equal(fit.guarantee, "sampled");
    const cylinder = fit.measurements.hypotheses.find(
      (h: any) => h.kind === "cylinder",
    );
    assert.ok(Math.abs(cylinder.radius_mm - 3) < 1e-6);
    assert.equal(cylinder.status, "hypothesis");
    assert.equal(
      fit.measurements.construction_history,
      "unknown_not_reconstructed",
    );
    assert.equal(
      s.call(principal, "cad_measure", {
        model_id: m.model_id,
        feature_id: "cyl",
        metric: "fit_primitives",
        idempotency_key: id("brep"),
      }).errors[0].code,
      "OUT_OF_SCOPE",
    );
    const field = structuredClone(organic) as any;
    field.features[0].owner_part = "part-field";
    field.features.push({
      id: "organic-mesh",
      semantic_name: "Extrahierte Isofläche",
      kind: "extract_isosurface",
      owner_part: "part-mesh",
      authoritative_representation: "mesh",
      parameters: {},
      construction: { operator: "extract_isosurface" },
      depends_on: ["organic"],
    });
    field.outputs = ["organic-mesh"];
    field.constraints = [];
    const o = await importFixture(s, field);
    const report = s.store.revision(principal, o.model_id).geometry.facts[
      "organic-mesh"
    ].conversion_report;
    assert.equal(report.source_representation, "implicit");
    assert.equal(
      report.measured_error_guarantee,
      "sampled_first_order_vertex_distance_estimate",
    );
    assert.deepEqual(report.unresolved_regions, ["subcell_topology"]);
    assert.throws(
      () =>
        compile({
          ...ir,
          features: ir.features.map((f) =>
            f.id === "remeshed" ? { ...f, depends_on: ["cyl"] } : f,
          ),
        }),
      (e: any) => e.code === "OUT_OF_SCOPE",
    );
  } finally {
    await env.close();
  }
});
