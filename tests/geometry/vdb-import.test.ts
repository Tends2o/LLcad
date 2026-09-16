import test from "node:test";
import assert from "node:assert/strict";
import { setup, call, finish, importFixture, principal } from "../helpers.js";
import { organic } from "../../scripts/fixtures.js";
import { id } from "../../packages/semantic-ir/hash.js";
const q = (value: string) => ({ value, unit: "mm" });

async function exportedGrid(s: any) {
  const m = await importFixture(s, organic);
  const exported = await finish(
    s,
    call(s, "cad_export", {
      model_id: m.model_id,
      revision: m.revision,
      format: "vdb",
      idempotency_key: id("vdb"),
    }),
  );
  const file = exported.artifacts.find(
    (a: any) => a.manifest.filename === "model.vdb",
  );
  const bytes = s.store.readBlob(file.hash);
  return s.store.artifact(principal, bytes, "application/x-vdb", null, null, {
    source: "upload",
  });
}

test("an OpenVDB grid imports as a cubic B-spline implicit field with measured bounds and renders", async () => {
  const env = setup(),
    s = env.service;
  try {
    const upload = await exportedGrid(s);
    const target = call(s, "cad_create_model", {
      name: "Volumenimport",
      idempotency_key: id("create"),
    });
    const draft = call(s, "cad_import", {
      model_id: target.model_id,
      base_revision: target.revision,
      artifact_id: upload.artifact_id,
      format: "vdb",
      source_unit: "mm",
      idempotency_key: id("import"),
    });
    const candidate = await finish(s, draft);
    const fid = candidate.changed_features[0];
    const facts = s.store.revision(
      principal,
      target.model_id,
      draft.candidate_revision,
    ).geometry.facts[fid];
    assert.equal(facts.field_semantics, "sampled_implicit_cubic_bspline");
    assert.equal(facts.value_unit, "length");
    assert.ok(
      facts.lipschitz_bound > 0.9 && facts.lipschitz_bound <= 6,
      String(facts.lipschitz_bound),
    );
    assert.deepEqual(facts.import_report.voxel_size_mm, [0.75, 0.75, 0.75]);
    assert.equal(facts.import_report.grid_name, "organic");
    assert.equal(facts.import_report.continuous_distance_certificate, null);
    assert.equal(facts.import_report.value_unit_assumed, false);
    assert.ok(
      facts.bounds[0] >= -7.1 &&
        facts.bounds[0] <= -5 &&
        facts.bounds[3] >= 5 &&
        facts.bounds[3] <= 7.1,
      JSON.stringify(facts.bounds),
    );
    const validation = await finish(
      s,
      call(s, "cad_validate", {
        model_id: target.model_id,
        base_revision: target.revision,
        transaction_id: draft.transaction_id,
        idempotency_key: id("validate"),
      }),
    );
    assert.equal(
      validation.status,
      "checks_passed_within_profile",
      JSON.stringify(
        validation.checks.filter((c: any) => c.status !== "passed"),
      ),
    );
    const commit = call(s, "cad_commit", {
      model_id: target.model_id,
      base_revision: target.revision,
      transaction_id: draft.transaction_id,
      validation_digest: validation.digest,
      idempotency_key: id("commit"),
    });
    const preview = await finish(
      s,
      call(s, "cad_render", {
        model_id: target.model_id,
        revision: commit.revision,
        idempotency_key: id("render"),
      }),
    );
    const mesh = JSON.parse(
      s.store.readBlob(preview.artifacts[0].hash).toString(),
    ).meshes[0];
    assert.ok(mesh.triangles.length > 100);
    const worst = Math.max(
      ...mesh.vertices.map((v: number[]) => Math.abs(Math.hypot(...v) - 5)),
    );
    assert.ok(worst < 0.3, "sampled sphere surface deviates by " + worst);
    const capabilities = call(s, "cad_capabilities", {});
    assert.ok(capabilities.formats.import.includes("vdb"));
    assert.ok(capabilities.field_operators.includes("sampled_grid"));
    const bogus = s.store.artifact(
      principal,
      Buffer.from("not a vdb file at all"),
      "application/x-vdb",
      null,
      null,
      { source: "upload" },
    );
    const other = call(s, "cad_create_model", {
      name: "Ziel",
      idempotency_key: id("create-2"),
    });
    assert.equal(
      s.call(principal, "cad_import", {
        model_id: other.model_id,
        base_revision: other.revision,
        artifact_id: bogus.artifact_id,
        format: "vdb",
        source_unit: "mm",
        idempotency_key: id("bogus"),
      }).errors[0].code,
      "INVALID_SCHEMA",
    );
  } finally {
    await env.close();
  }
});

test("sampled grids compose with analytic fields, certify against their source and verify claimed bounds", async () => {
  const env = setup(),
    s = env.service;
  try {
    const upload = await exportedGrid(s);
    const grid = (lipschitz: string, value_unit = "length") => ({
      op: "sampled_grid",
      artifact_id: upload.artifact_id,
      lipschitz,
      value_unit,
    });
    // Certificate cells small against the voxel size keep the first-order mean-value bound tight.
    const domain = { min: ["-6", "-6", "-6"], max: ["6", "6", "6"] };
    const field = (expression: any, cell = "0.5") => ({
      ...organic,
      features: [
        {
          ...organic.features[0],
          construction: {
            ...organic.features[0].construction,
            expression,
            domain,
            cell_size: q(cell),
          },
        },
      ],
      constraints: [],
    });
    const m = await importFixture(s, field(grid("6"), "0.25"));
    const stored = s.store.revision(principal, m.model_id, m.revision).ir
      .features[0].construction.expression;
    const analytic = call(s, "cad_apply_patch", {
      model_id: m.model_id,
      base_revision: m.revision,
      idempotency_key: id("analytic"),
      operations: [
        {
          op: "set_field",
          feature_id: "organic",
          expected_hash: (
            await import("../../packages/semantic-ir/hash.js")
          ).hash(stored),
          expression: { op: "sphere", center: ["0", "0", "0"], radius: "5" },
        },
      ],
    });
    await finish(s, analytic);
    const deviation = await finish(
      s,
      call(s, "cad_measure", {
        model_id: m.model_id,
        revision: analytic.candidate_revision,
        feature_id: "organic",
        metric: "surface_deviation",
        other_revision: m.revision,
        maximum_deviation: q("0.1"),
        idempotency_key: id("deviation"),
      }),
    );
    assert.equal(
      deviation.guarantee,
      "bounded",
      JSON.stringify(deviation.measurements),
    );
    assert.ok(
      deviation.certified_error_bound !== null &&
        deviation.certified_error_bound <= 0.1,
    );
    assert.ok(deviation.certified_error_bound > 0);
    assert.equal(deviation.measurements.failed_cells, 0);
    const composed = await importFixture(
      s,
      field({
        op: "difference",
        a: grid("6"),
        b: { op: "box", center: ["0", "0", "0"], half_size: ["2", "2", "9"] },
      }),
    );
    const preview = await finish(
      s,
      call(s, "cad_render", {
        model_id: composed.model_id,
        revision: composed.revision,
        idempotency_key: id("composed"),
      }),
    );
    const mesh = JSON.parse(
      s.store.readBlob(preview.artifacts[0].hash).toString(),
    ).meshes[0];
    assert.ok(
      mesh.vertices.every(
        (v: number[]) => Math.abs(v[0]) >= 1.9 || Math.abs(v[1]) >= 1.9,
      ),
    );
    const weak = call(s, "cad_create_model", {
      name: "Schwache Schranke",
      idempotency_key: id("weak"),
    });
    const weakUpload = s.store.artifact(
      principal,
      JSON.stringify(field(grid("0.2"))),
      "application/json",
      weak.model_id,
      weak.revision,
      {},
    );
    const refused = await s.jobs.wait(
      principal,
      call(s, "cad_import", {
        model_id: weak.model_id,
        base_revision: weak.revision,
        artifact_id: weakUpload.artifact_id,
        format: "ir",
        source_unit: "mm",
        idempotency_key: id("weak-import"),
      }).job_id,
    );
    assert.equal(refused.status, "failed");
    assert.equal(refused.error.code, "CONSTRAINT_CONFLICT");
    const foreign = { ...principal, user: "bob" };
    const bobModel = call(
      s,
      "cad_create_model",
      { name: "Fremd", idempotency_key: id("bob") },
      foreign,
    );
    const bobUpload = s.store.artifact(
      foreign,
      JSON.stringify(field(grid("6"))),
      "application/json",
      bobModel.model_id,
      bobModel.revision,
      {},
    );
    assert.equal(
      s.call(foreign, "cad_import", {
        model_id: bobModel.model_id,
        base_revision: bobModel.revision,
        artifact_id: bobUpload.artifact_id,
        format: "ir",
        source_unit: "mm",
        idempotency_key: id("bob-import"),
      }).errors[0].code,
      "ACCESS_DENIED",
    );
  } finally {
    await env.close();
  }
});
