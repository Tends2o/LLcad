import test from "node:test";
import assert from "node:assert/strict";
import { setup, call, finish, importFixture, principal } from "../helpers.js";
import { housing, organic } from "../../scripts/fixtures.js";
import { id } from "../../packages/semantic-ir/hash.js";
const q = (value: string) => ({ value, unit: "mm" });

test("section and hidden-line views are native diagnostic artifacts with labels, dimensions and scale", async () => {
  const env = setup(),
    s = env.service;
  try {
    const m = await importFixture(s, housing);
    const section = await finish(
      s,
      call(s, "cad_render", {
        model_id: m.model_id,
        revision: m.revision,
        idempotency_key: id("section"),
        view: {
          kind: "section",
          origin: ["0", "0", "2.6"],
          normal: ["0", "1", "0"],
          discretization: q("0.01"),
        },
      }),
    );
    const svg = section.artifacts.find(
      (a: any) => a.manifest.filename === "view.svg",
    );
    const report = section.artifacts.find(
      (a: any) => a.manifest.filename === "view.json",
    );
    assert.equal(svg.mime, "image/svg+xml");
    const text = s.store.readBlob(svg.hash).toString();
    assert.match(text, /<svg/);
    assert.match(text, /Befestigungsbohrung/);
    assert.match(text, /40 mm/);
    assert.match(text, /Maßstab/);
    assert.equal(svg.manifest.diagnostic_view.view, "section");
    assert.equal(svg.manifest.diagnostic_view.certified_surface_bound, null);
    assert.ok(Math.abs(svg.manifest.diagnostic_view.extents_mm[2] - 20) < 1e-9);
    assert.ok(
      JSON.parse(s.store.readBlob(report.hash).toString()).polylines >= 8,
    );
    const top = await finish(
      s,
      call(s, "cad_render", {
        model_id: m.model_id,
        revision: m.revision,
        idempotency_key: id("top"),
        view: {
          kind: "orthographic",
          direction: ["0", "0", "1"],
          hidden_lines: true,
        },
      }),
    );
    const topSvg = top.artifacts.find(
      (a: any) => a.manifest.filename === "view.svg",
    );
    assert.equal(topSvg.manifest.diagnostic_view.view, "orthographic");
    assert.ok(topSvg.manifest.diagnostic_view.hidden_polylines >= 1);
    assert.match(s.store.readBlob(topSvg.hash).toString(), /stroke-dasharray/);
    const escaped = await importFixture(s, {
      ...housing,
      features: housing.features.map((f) =>
        f.id === "feat-base"
          ? { ...f, semantic_name: "Boden <script>alert(1)</script>" }
          : f,
      ),
    });
    const guarded = await finish(
      s,
      call(s, "cad_render", {
        model_id: escaped.model_id,
        revision: escaped.revision,
        feature_id: "feat-base",
        idempotency_key: id("escape"),
        view: { kind: "orthographic", direction: ["1", "0", "0"] },
      }),
    );
    const guardedText = s.store
      .readBlob(
        guarded.artifacts.find((a: any) => a.manifest.filename === "view.svg")
          .hash,
      )
      .toString();
    assert.ok(!guardedText.includes("<script>"));
    assert.match(guardedText, /&lt;script&gt;/);
    const o = await importFixture(s, organic);
    const refused = await s.jobs.wait(
      principal,
      call(s, "cad_render", {
        model_id: o.model_id,
        revision: o.revision,
        idempotency_key: id("field-view"),
        view: { kind: "section", normal: ["0", "0", "1"] },
      }).job_id,
    );
    assert.equal(refused.status, "failed");
    assert.equal(refused.error.code, "OUT_OF_SCOPE");
    assert.equal(
      s.call(principal, "cad_render", {
        model_id: m.model_id,
        idempotency_key: id("bad"),
        view: { kind: "section", normal: ["0", "0", "0"] },
      }).errors[0].code,
      "INVALID_SCHEMA",
    );
  } finally {
    await env.close();
  }
});

test("adaptive per-face tessellation reports resolution, curvature policy and spatial excerpts", async () => {
  const env = setup(),
    s = env.service;
  try {
    const m = await importFixture(s, housing);
    const adaptive = await finish(
      s,
      call(s, "cad_render", {
        model_id: m.model_id,
        revision: m.revision,
        idempotency_key: id("adaptive"),
        adaptive: { target_error: q("0.005"), feature_factor: "0.25" },
        region: { center: ["10", "0", "3"], radius: "3" },
      }),
    );
    const preview = adaptive.artifacts.find(
      (a: any) => a.manifest.filename === "preview.json",
    );
    assert.ok(
      preview.manifest.resolution.absolute_resolution_mm <= 0.005 + 1e-12,
    );
    assert.ok(
      preview.manifest.resolution.minimum_feature_resolved_mm >=
        2 * preview.manifest.resolution.absolute_resolution_mm - 1e-12,
    );
    assert.equal(preview.manifest.resolution.certified_surface_bound, null);
    assert.equal(preview.manifest.clip.radius, 3);
    const data = JSON.parse(s.store.readBlob(preview.hash).toString());
    assert.ok(
      data.meshes[0].clip.triangles_after <
        data.meshes[0].clip.triangles_before,
    );
    assert.ok(data.meshes[0].triangles.length > 0);
    const plain = await finish(
      s,
      call(s, "cad_render", {
        model_id: m.model_id,
        revision: m.revision,
        idempotency_key: id("plain"),
        adaptive: { target_error: q("0.02") },
      }),
    );
    const faces = JSON.parse(
      s.store.readBlob(plain.artifacts[0].hash).toString(),
    ).meshes[0].face_ranges;
    const curved = faces.find((f: any) => f.curvature_max_per_mm > 0);
    assert.ok(curved.edge_length_heuristic_mm > 0);
    assert.equal(curved.sagitta_valid, true);
    assert.ok(curved.deflection_mm <= 0.02);
    assert.equal(
      s.call(principal, "cad_render", {
        model_id: m.model_id,
        idempotency_key: id("too-fine"),
        adaptive: { target_error: q("0.000001") },
      }).errors[0].code,
      "PRECISION_UNSUPPORTED",
    );
  } finally {
    await env.close();
  }
});
