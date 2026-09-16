import test from "node:test";
import assert from "node:assert/strict";
import { setup, call, finish, principal } from "../helpers.js";
import { ModelIR } from "../../packages/semantic-ir/schema.js";
import { compile } from "../../packages/compiler/index.js";
import { id } from "../../packages/semantic-ir/hash.js";
const q = (value: string, unit = "mm") => ({ value, unit });

function bracket(minimumWall: string, holeRadius: string, overhang?: string) {
  return ModelIR.parse({
    schema_version: "1",
    unit: "mm",
    profile: "manufacturing_candidate",
    manufacturing: {
      process: "fdm",
      minimum_wall: q(minimumWall),
      minimum_hole_diameter: q("3"),
      ...(overhang ? { maximum_overhang: q(overhang, "deg") } : {}),
    },
    features: [
      {
        id: "base",
        semantic_name: "Grundplatte",
        kind: "box",
        parameters: { width: q("20"), depth: q("20"), height: q("4") },
        construction: { operator: "box" },
      },
      {
        id: "pocket",
        semantic_name: "Tasche",
        kind: "box",
        parameters: {
          width: q("16"),
          depth: q("16"),
          height: q("2.5"),
          x: q("2"),
          y: q("2"),
          z: q("1.5"),
        },
        construction: { operator: "box" },
      },
      {
        id: "tray",
        semantic_name: "Schale",
        kind: "difference",
        parameters: {},
        construction: { operator: "difference" },
        depends_on: ["base", "pocket"],
      },
      {
        id: "mount",
        semantic_name: "Befestigungsbohrung",
        kind: "hole",
        parameters: {
          radius: q(holeRadius),
          depth: q("1.5"),
          x: q("10"),
          y: q("10"),
          z: q("1.5"),
        },
        construction: { operator: "hole" },
        depends_on: ["tray"],
      },
    ],
    outputs: ["mount"],
  });
}

async function candidate(s: any, ir: any) {
  const m = call(s, "cad_create_model", {
    name: "Fertigungskandidat",
    profile: "manufacturing_candidate",
    idempotency_key: id("create"),
  });
  const upload = s.store.artifact(
    principal,
    JSON.stringify(ir),
    "application/json",
    m.model_id,
    m.revision,
    {},
  );
  const draft = call(s, "cad_import", {
    model_id: m.model_id,
    base_revision: m.revision,
    artifact_id: upload.artifact_id,
    format: "ir",
    source_unit: "mm",
    idempotency_key: id("import"),
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
  return { m, draft, validation };
}

test("manufacturing_candidate samples explicit process rules and never certifies manufacturing", async () => {
  const env = setup(),
    s = env.service;
  try {
    const ok = await candidate(s, bracket("1", "2", "60"));
    assert.equal(
      ok.validation.status,
      "checks_passed_within_profile",
      JSON.stringify(
        ok.validation.checks.filter((c: any) => c.status !== "passed"),
      ),
    );
    const wall = ok.validation.checks.find(
      (c: any) => c.check_id === "manufacturing-wall-mount",
    );
    assert.equal(wall.guarantee, "sampled");
    assert.ok(
      wall.measured.minimum_mm >= 1 && wall.measured.minimum_mm <= 1.5 + 1e-9,
      String(wall.measured.minimum_mm),
    );
    const hole = ok.validation.checks.find(
      (c: any) => c.check_id === "manufacturing-hole-mount",
    );
    assert.equal(hole.status, "passed");
    assert.equal(hole.measured, 4);
    const overhang = ok.validation.checks.find(
      (c: any) => c.check_id === "manufacturing-overhang-mount",
    );
    assert.equal(overhang.status, "passed");
    assert.equal(overhang.guarantee, "sampled");
    assert.ok(
      ok.validation.checks.some(
        (c: any) => c.check_id === "solid" && c.status === "passed",
      ),
    );
    const commit = call(s, "cad_commit", {
      model_id: ok.m.model_id,
      base_revision: ok.m.revision,
      transaction_id: ok.draft.transaction_id,
      validation_digest: ok.validation.digest,
      idempotency_key: id("commit"),
    });
    const inspected = call(s, "cad_inspect", {
      model_id: ok.m.model_id,
      revision: commit.revision,
      feature_id: "mount",
      sections: ["quality"],
    });
    assert.equal(
      inspected.quality_status.manufacturing_status,
      "rules_sampled",
    );
    assert.equal(inspected.quality_status.dimensional_status, "checks_passed");
    const thin = await candidate(s, bracket("2", "1"));
    assert.equal(thin.validation.status, "failed");
    assert.equal(
      thin.validation.checks.find(
        (c: any) => c.check_id === "manufacturing-wall-mount",
      ).status,
      "failed",
    );
    assert.equal(
      thin.validation.checks.find(
        (c: any) => c.check_id === "manufacturing-hole-mount",
      ).status,
      "failed",
    );
    assert.throws(
      () => compile({ ...bracket("1", "2"), manufacturing: undefined }),
      /manufacturing/,
    );
    assert.throws(
      () => compile({ ...bracket("1", "2"), profile: "precision_cad" }),
      /Fertigungsregeln/,
    );
    const capabilities = call(s, "cad_capabilities", {});
    assert.equal(capabilities.manufacturing_candidate.certification, false);
  } finally {
    await env.close();
  }
});
