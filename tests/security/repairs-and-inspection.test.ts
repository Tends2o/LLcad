import test from "node:test";
import assert from "node:assert/strict";
import { setup, call, finish, importFixture, principal } from "../helpers.js";
import { housing, sphere } from "../../scripts/fixtures.js";
import { id } from "../../packages/semantic-ir/hash.js";
import {
  PIPELINE_POLICY,
  MANDATORY,
  HOOK_CONTEXTS,
} from "../../hooks/server-registry/index.js";
const q = (value: string) => ({ value, unit: "mm" });

test("repair attempts are bounded, record cause and cost, and compare intent with the original candidate", async () => {
  const env = setup(),
    s = env.service;
  try {
    const m = await importFixture(s, housing);
    const patch = (depth: string, repair?: any) =>
      call(s, "cad_apply_patch", {
        model_id: m.model_id,
        base_revision: m.revision,
        idempotency_key: id("repair"),
        operations: [
          {
            op: "set_parameter",
            feature_id: "feat-groove-07",
            parameter: "depth",
            expected: q("0.80"),
            value: q(depth),
          },
        ],
        ...(repair ? { repair } : {}),
      });
    const first = patch("1.5");
    await finish(s, first);
    const failed = await finish(
      s,
      call(s, "cad_validate", {
        model_id: m.model_id,
        base_revision: m.revision,
        transaction_id: first.transaction_id,
        idempotency_key: id("v"),
      }),
    );
    assert.equal(failed.status, "failed");
    let previous = first.transaction_id;
    for (
      let attempt = 1;
      attempt <= PIPELINE_POLICY.repair_policy.max_candidate_retries;
      attempt++
    ) {
      const repaired = patch((0.9 + attempt * 0.01).toFixed(2), {
        of_transaction: previous,
        cause: "remaining wall below 2 mm",
      });
      assert.equal(repaired.repair_attempt.attempt, attempt);
      assert.equal(
        repaired.repair_attempt.remaining,
        PIPELINE_POLICY.repair_policy.max_candidate_retries - attempt,
      );
      assert.equal(
        repaired.repair_attempt.intent_comparison.same_targets,
        true,
      );
      assert.equal(repaired.repair_attempt.previous_attempts.length, attempt);
      await finish(s, repaired);
      previous = repaired.transaction_id;
    }
    const exhausted = s.call(principal, "cad_apply_patch", {
      model_id: m.model_id,
      base_revision: m.revision,
      idempotency_key: id("exhausted"),
      operations: [
        {
          op: "set_parameter",
          feature_id: "feat-groove-07",
          parameter: "depth",
          expected: q("0.80"),
          value: q("0.95"),
        },
      ],
      repair: { of_transaction: previous, cause: "still too deep" },
    });
    assert.equal(exhausted.errors[0].code, "BUDGET_EXCEEDED");
    assert.equal(exhausted.errors[0].details.repair_attempts.length, 4);
    assert.ok(
      exhausted.errors[0].details.repair_attempts.every(
        (a: any) => typeof a.cost_seconds === "number",
      ),
    );
    assert.equal(
      s.call(principal, "cad_apply_patch", {
        model_id: m.model_id,
        base_revision: m.revision,
        idempotency_key: id("identical"),
        operations: [
          {
            op: "set_parameter",
            feature_id: "feat-groove-07",
            parameter: "depth",
            expected: q("0.80"),
            value: q("1.5"),
          },
        ],
        repair: {
          of_transaction: first.transaction_id,
          cause: "retry identical",
        },
      }).errors[0].code,
      "CONSTRAINT_CONFLICT",
    );
    assert.equal(
      call(s, "cad_get_model", { model_id: m.model_id }).revision,
      m.revision,
    );
    assert.deepEqual(PIPELINE_POLICY.mandatory_gates, MANDATORY);
    assert.ok(Object.keys(HOOK_CONTEXTS).length === MANDATORY.length);
    const audit = s.store.all(
      "SELECT event,data FROM audit WHERE event IN ('on_failure','repair_attempt')",
    );
    assert.ok(audit.some((a: any) => a.event === "repair_attempt"));
  } finally {
    await env.close();
  }
});

test("spatial search uses the revision BVH, inspection sections trim payloads and anchors bind viewer hits to faces", async () => {
  const env = setup(),
    s = env.service;
  try {
    const m = await importFixture(s, housing);
    const boxed = call(s, "cad_find", {
      model_id: m.model_id,
      box: { min: ["14", "14", "0"], max: ["16", "16", "3"] },
    });
    assert.match(boxed.spatial_filter, /bvh_box_overlap/);
    assert.ok(boxed.matches.some((x: any) => x.feature_id === "feat-hole-01"));
    const far = call(s, "cad_find", {
      model_id: m.model_id,
      box: { min: ["100", "100", "100"], max: ["101", "101", "101"] },
    });
    assert.equal(far.total_matches, 0);
    assert.equal(
      s.call(principal, "cad_find", {
        model_id: m.model_id,
        box: { min: ["1", "1", "1"], max: ["0", "0", "0"] },
      }).errors[0].code,
      "INVALID_SCHEMA",
    );
    const full = call(s, "cad_inspect", {
      model_id: m.model_id,
      feature_id: "feat-base",
    });
    assert.ok(full.face_page.faces[0].adjacent_face_ids.length >= 3);
    assert.equal(full.quality_status.dimensional_status, "checks_passed");
    assert.equal(full.quality_status.topology_status, "checks_passed");
    assert.equal(full.quality_status.manufacturing_status, "not_certified");
    const slim = call(s, "cad_inspect", {
      model_id: m.model_id,
      feature_id: "feat-base",
      sections: ["constraints"],
    });
    assert.equal(slim.face_page.faces.length, 0);
    assert.equal(slim.face_page.total, full.face_page.total);
    assert.equal(slim.known_facts, null);
    assert.equal(slim.quality_status, null);
    assert.ok(JSON.stringify(slim).length < JSON.stringify(full).length);
    const noAdjacency = call(s, "cad_inspect", {
      model_id: m.model_id,
      feature_id: "feat-base",
      sections: ["faces"],
    });
    assert.equal(noAdjacency.face_page.faces[0].adjacent_face_ids, undefined);
    const top = full.face_page.faces.find(
      (f: any) => f.origins[0].role === "top",
    );
    const anchored = call(s, "cad_inspect", {
      model_id: m.model_id,
      revision: m.revision,
      feature_id: "feat-base",
      face_id: top.face_id,
      anchor: {
        point: ["1", "2", "3"],
        normal: ["0", "0", "1"],
        view_direction: ["0", "0", "-1"],
        barycentric: ["0.2", "0.3", "0.5"],
      },
    });
    assert.equal(anchored.selection_anchor.face_id, top.face_id);
    assert.equal(anchored.selection_anchor.view_relative.facing_camera, true);
    assert.equal(anchored.selection_anchor.local_frame, "world");
    const reused = call(s, "cad_inspect", {
      model_id: m.model_id,
      revision: m.revision,
      selection_handle: anchored.selection_handle,
    });
    assert.deepEqual(reused.selection_anchor.point_mm, [1, 2, 3]);
    assert.equal(
      s.call(principal, "cad_inspect", {
        model_id: m.model_id,
        revision: m.revision,
        feature_id: "feat-base",
        face_id: top.face_id,
        anchor: { point: ["500", "0", "0"] },
      }).errors[0].code,
      "OUT_OF_SCOPE",
    );
    assert.equal(
      s.call(principal, "cad_inspect", {
        model_id: m.model_id,
        feature_id: "feat-base",
        anchor: { point: ["1", "2", "3"] },
      }).errors[0].code,
      "INVALID_SCHEMA",
    );
    const candidate = await finish(
      s,
      call(s, "cad_apply_patch", {
        model_id: m.model_id,
        base_revision: m.revision,
        idempotency_key: id("draft"),
        operations: [
          {
            op: "set_parameter",
            feature_id: "feat-groove-07",
            parameter: "depth",
            expected: q("0.80"),
            value: q("0.82"),
          },
        ],
      }),
    );
    const draft = call(s, "cad_inspect", {
      model_id: m.model_id,
      revision: candidate.candidate_revision,
      feature_id: "feat-groove-07",
    });
    assert.equal(draft.quality_status.dimensional_status, "not_evaluated");
    assert.match(draft.quality_status.source, /candidate_or_draft/);
  } finally {
    await env.close();
  }
});

test("archives are refused at import before any decoding", async () => {
  const env = setup(),
    s = env.service;
  try {
    const m = await importFixture(s, sphere);
    const zip = Buffer.concat([
      Buffer.from([0x50, 0x4b, 0x03, 0x04]),
      Buffer.alloc(64),
    ]);
    const artifact = s.store.artifact(
      principal,
      zip,
      "application/octet-stream",
      null,
      null,
      { source: "upload" },
    );
    const empty = call(s, "cad_create_model", {
      name: "Ziel",
      idempotency_key: id("zip-target"),
    });
    const refused = s.call(principal, "cad_import", {
      model_id: empty.model_id,
      base_revision: empty.revision,
      artifact_id: artifact.artifact_id,
      format: "stl",
      source_unit: "mm",
      idempotency_key: id("zip"),
    });
    assert.equal(refused.errors[0].code, "OUT_OF_SCOPE");
    assert.equal(
      call(s, "cad_get_model", { model_id: m.model_id }).revision,
      m.revision,
    );
  } finally {
    await env.close();
  }
});
