import test from "node:test";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { setup, call, finish, principal } from "../helpers.js";
import { id } from "../../packages/semantic-ir/hash.js";

function assemblyStep() {
  const dir = mkdtempSync(join(tmpdir(), "mathforge-step-"));
  try {
    const run = spawnSync(
      ".venv/bin/python",
      ["scripts/assembly-fixture.py", join(dir, "asm.step")],
      { encoding: "utf8", timeout: 120000 },
    );
    assert.equal(run.status, 0, run.stderr);
    return readFileSync(join(dir, "asm.step"));
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
}

test("a STEP assembly imports with its occurrence tree as frames, assemblies, parts and component features", async () => {
  const env = setup(),
    s = env.service;
  try {
    const upload = s.store.artifact(
      principal,
      assemblyStep(),
      "application/step",
      null,
      null,
      { source: "upload" },
    );
    const m = call(s, "cad_create_model", {
      name: "Baugruppe",
      idempotency_key: id("create"),
    });
    const probe = call(s, "cad_import", {
      model_id: m.model_id,
      base_revision: m.revision,
      artifact_id: upload.artifact_id,
      format: "step",
      source_unit: "mm",
      structure: "preserve",
      idempotency_key: id("probe"),
    });
    assert.equal(probe.status, "queued");
    const probed = await s.jobs.wait(principal, probe.job_id);
    assert.equal(probed.status, "succeeded", JSON.stringify(probed.error));
    const nodes = probed.result.structure.nodes;
    assert.equal(nodes.length, 6);
    assert.deepEqual(
      nodes.map((n: any) => [n.kind, n.name]),
      [
        ["assembly", "Baugruppe"],
        ["part", "Quader an Ort"],
        ["part", "Zylinder gedreht"],
        ["assembly", "Unterbaugruppe versetzt"],
        ["part", "Stift 1"],
        ["part", "Stift 2"],
      ],
    );
    assert.equal(nodes[4].prototype_entry, nodes[5].prototype_entry);
    assert.deepEqual(nodes[2].transform.axis, [1, 0, 0]);
    assert.ok(Math.abs(nodes[2].transform.angle_deg - 90) < 1e-9);
    const continuation = probed.result.continuation;
    assert.equal(continuation.status, "queued", JSON.stringify(continuation));
    const candidate = await finish(s, continuation);
    assert.equal(candidate.changed_features.length, 4);
    const validation = await finish(
      s,
      call(s, "cad_validate", {
        model_id: m.model_id,
        base_revision: m.revision,
        transaction_id: continuation.transaction_id,
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
      model_id: m.model_id,
      base_revision: m.revision,
      transaction_id: continuation.transaction_id,
      validation_digest: validation.digest,
      idempotency_key: id("commit"),
    });
    const parts = call(s, "cad_structure", {
      model_id: m.model_id,
      revision: commit.revision,
      kind: "part",
    });
    assert.equal(parts.total_matches, 4);
    const cylinder = parts.entries.find(
      (e: any) => e.semantic_name === "Zylinder gedreht",
    );
    // Prototype cylinder [-3,3]x[-3,3]x[0,12], rotated 90° about x and moved to x=20.
    assert.ok(
      Math.abs(cylinder.world_bounds[0] - 17) < 1e-6 &&
        Math.abs(cylinder.world_bounds[3] - 23) < 1e-6,
      JSON.stringify(cylinder.world_bounds),
    );
    assert.ok(
      Math.abs(cylinder.world_bounds[1] + 12) < 1e-6 &&
        Math.abs(cylinder.world_bounds[4]) < 1e-6,
      JSON.stringify(cylinder.world_bounds),
    );
    assert.equal(cylinder.placement.path.length, 1);
    const pins = parts.entries.filter((e: any) =>
      e.semantic_name.startsWith("Stift"),
    );
    assert.equal(pins.length, 2);
    assert.ok(
      pins.every((p: any) => Math.abs(p.world_bounds[1] - 29) < 1e-6),
      JSON.stringify(pins.map((p: any) => p.world_bounds)),
    );
    assert.equal(pins[1].placement.path.length, 2);
    const assemblies = call(s, "cad_structure", {
      model_id: m.model_id,
      revision: commit.revision,
      kind: "assembly",
    });
    assert.equal(assemblies.total_matches, 2);
    assert.ok(
      assemblies.entries.some((e: any) => e.definition.parent_assembly),
    );
    const model = call(s, "cad_get_model", {
      model_id: m.model_id,
      revision: commit.revision,
    });
    assert.ok(model.features.every((f: any) => f.operator === "imported"));
    assert.equal(new Set(model.features.map((f: any) => f.owner_part)).size, 4);
    for (const f of model.features) {
      const inspected = call(s, "cad_inspect", {
        model_id: m.model_id,
        revision: commit.revision,
        feature_id: f.id,
      });
      assert.match(inspected.construction_summary.component, /^0(:\d+)+$/);
      assert.equal(inspected.purpose.status, "imported");
    }
    const exported = await finish(
      s,
      call(s, "cad_export", {
        model_id: m.model_id,
        revision: commit.revision,
        format: "step",
        idempotency_key: id("export"),
      }),
    );
    assert.ok(
      exported.artifacts.some((a: any) => a.manifest.filename === "model.step"),
    );
    const flat = call(s, "cad_create_model", {
      name: "Flach",
      idempotency_key: id("flat"),
    });
    const flatDraft = call(s, "cad_import", {
      model_id: flat.model_id,
      base_revision: flat.revision,
      artifact_id: upload.artifact_id,
      format: "step",
      source_unit: "mm",
      idempotency_key: id("flat-import"),
    });
    assert.equal((await finish(s, flatDraft)).changed_features.length, 1);
    const stl = call(s, "cad_create_model", {
      name: "Netz",
      idempotency_key: id("stl"),
    });
    assert.equal(
      s.call(principal, "cad_import", {
        model_id: stl.model_id,
        base_revision: stl.revision,
        artifact_id: upload.artifact_id,
        format: "stl",
        source_unit: "mm",
        structure: "preserve",
        idempotency_key: id("stl-structure"),
      }).errors[0].code,
      "OUT_OF_SCOPE",
    );
  } finally {
    await env.close();
  }
});

test("a stale base revision fails the continuation honestly while the probe report stays readable", async () => {
  const env = setup(),
    s = env.service;
  try {
    const upload = s.store.artifact(
      principal,
      assemblyStep(),
      "application/step",
      null,
      null,
      { source: "upload" },
    );
    const m = call(s, "cad_create_model", {
      name: "Baugruppe",
      idempotency_key: id("create"),
    });
    const original = s.jobs.continuation!;
    s.jobs.continuation = (p, request, report) => {
      s.store.run(
        "UPDATE models SET head=? WHERE id=?",
        "revision-moved-elsewhere",
        m.model_id,
      );
      return original(p, request, report);
    };
    const probe = call(s, "cad_import", {
      model_id: m.model_id,
      base_revision: m.revision,
      artifact_id: upload.artifact_id,
      format: "step",
      source_unit: "mm",
      structure: "preserve",
      idempotency_key: id("probe"),
    });
    const probed = await s.jobs.wait(principal, probe.job_id);
    assert.equal(probed.status, "succeeded");
    assert.equal(probed.result.continuation.status, "failed");
    assert.equal(probed.result.continuation.error.code, "STALE_REVISION");
    assert.equal(probed.result.structure.parts, 4);
    assert.equal(s.store.get("SELECT COUNT(*) AS n FROM transactions").n, 0);
  } finally {
    await env.close();
  }
});
