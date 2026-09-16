import test from "node:test";
import assert from "node:assert/strict";
import { setup, call, finish, importFixture } from "../helpers.js";
import { organic } from "../../scripts/fixtures.js";
import { id, hash } from "../../packages/semantic-ir/hash.js";

const source = (organic.features[0].construction as any).expression;
test("a change_region constraint admits compact edits inside the region and refuses global or outside changes", async () => {
  const env = setup(),
    s = env.service;
  try {
    const ir = {
      ...organic,
      constraints: [
        {
          id: "allowed-zone",
          kind: "change_region",
          feature_id: "organic",
          center: ["5", "0", "0"],
          radius: "1.5",
          compute_margin: "0.5",
        },
      ],
    };
    const model = await importFixture(s, ir);
    const attempt = async (revision: string, before: any, after: any) => {
      const draft = call(s, "cad_apply_patch", {
        model_id: model.model_id,
        base_revision: revision,
        idempotency_key: id("region"),
        operations: [
          {
            op: "set_field",
            feature_id: "organic",
            expected_hash: hash(before),
            expression: after,
          },
        ],
      });
      await finish(s, draft);
      return finish(
        s,
        call(s, "cad_validate", {
          model_id: model.model_id,
          base_revision: revision,
          transaction_id: draft.transaction_id,
          idempotency_key: id("validate"),
        }),
      );
    };
    const inside = {
      op: "local_field_delta",
      source,
      center: ["5", "0", "0"],
      radius: "1",
      amplitude: "0.03",
    };
    const ok = await attempt(model.revision, source, inside);
    const check = ok.checks.find((c: any) => c.check_id === "allowed-zone");
    assert.equal(ok.status, "checks_passed_within_profile");
    assert.equal(check.guarantee, "exact_for_declared_domain");
    assert.equal(check.measured.differing_supports, 1);
    assert.equal(check.measured.compute_region.radius, "2");
    assert.ok(ok.error_budget.entries.length >= 1);
    const outside = await attempt(model.revision, source, {
      ...inside,
      center: ["0", "5", "0"],
    });
    assert.equal(outside.status, "failed");
    assert.equal(
      outside.checks.find((c: any) => c.check_id === "allowed-zone").status,
      "failed",
    );
    const global = await attempt(model.revision, source, {
      ...source,
      radius: "5.2",
    });
    assert.equal(
      global.checks.find((c: any) => c.check_id === "allowed-zone").measured
        .global_change,
      true,
    );
    assert.equal(global.status, "failed");
  } finally {
    await env.close();
  }
});
