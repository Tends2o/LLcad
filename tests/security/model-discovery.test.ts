import test from "node:test";
import assert from "node:assert/strict";
import { setup, call, principal } from "../helpers.js";
import { id } from "../../packages/semantic-ir/hash.js";

test("MCP model discovery is private, paged and does not need remembered IDs or viewer access", async () => {
  const env = setup(),
    s = env.service;
  try {
    const create = (name: string, p = principal) =>
      call(
        s,
        "cad_create_model",
        { name, purpose: "Pumpenhalter", idempotency_key: id("create") },
        p,
      );
    const own = [create("LLcad A"), create("LLcad B")];
    create("LLcad fremd", { ...principal, user: "bob" });
    create("LLcad anderer Mandant", { ...principal, tenant: "tenant-b" });
    const first = call(s, "cad_list_models", { query: "llcad", limit: 1 });
    assert.equal(first.total, 2);
    assert.equal(first.next_offset, 1);
    const second = call(s, "cad_list_models", {
      query: "llcad",
      limit: 1,
      offset: first.next_offset,
    });
    assert.equal(second.next_offset, null);
    assert.deepEqual(
      new Set([...first.models, ...second.models].map((m: any) => m.model_id)),
      new Set(own.map((m) => m.model_id)),
    );
    assert.equal(
      call(s, "cad_list_models", { query: "Pumpenhalter" }).total,
      2,
    );
    assert.equal(
      call(s, "cad_list_models", { query: "%' OR 1=1 --" }).total,
      0,
    );
    assert.equal(
      s.call({ ...principal, scopes: [] }, "cad_list_models", {}).errors[0]
        .code,
      "ACCESS_DENIED",
    );
    assert.equal(
      s.call(principal, "cad_list_models", { tenant: "tenant-b" }).errors[0]
        .code,
      "INVALID_SCHEMA",
    );
  } finally {
    await env.close();
  }
});
