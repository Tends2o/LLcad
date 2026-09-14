import test from "node:test";
import assert from "node:assert/strict";
import {
  compactSupportDisjoint,
  sameFieldInRegion,
} from "../../packages/compiler/field-regions.js";

test("compact support comparison resolves decimal tangency beyond double precision", () => {
  const region = { min: ["0", "0", "0"], max: ["1", "1", "1"] };
  const source = { op: "sphere", center: ["0", "0", "0"], radius: "2" };
  const delta = {
    op: "local_field_delta",
    source,
    center: ["2", "0", "0"],
    radius: "1",
    amplitude: "0.1",
  };
  assert.ok(compactSupportDisjoint(delta, region));
  assert.equal(
    compactSupportDisjoint(
      { ...delta, center: ["1.999999999999999999999999999999", "0", "0"] },
      region,
    ),
    false,
  );
  assert.ok(
    compactSupportDisjoint(
      { ...delta, center: ["2.000000000000000000000000000001", "0", "0"] },
      region,
    ),
  );
  const deform = {
    op: "local_deform",
    source,
    center: ["5", "0", "0"],
    radius: "3",
    displacement: ["0.6", "0.8", "0"],
  };
  assert.ok(compactSupportDisjoint(deform, region));
  assert.equal(
    compactSupportDisjoint(
      { ...deform, center: ["4.999999999999999999999999999999", "0", "0"] },
      region,
    ),
    false,
  );
  assert.equal(
    compactSupportDisjoint({ ...deform, center: ["1", "0", "0"] }, region),
    false,
  );
});

test("editing or removing an existing remote detail preserves the whole protected region", () => {
  const region = { min: ["-7", "-7", "-7"], max: ["-2", "7", "7"] };
  const source = { op: "sphere", center: ["0", "0", "0"], radius: "5" };
  const delta = {
    op: "local_field_delta",
    source,
    center: ["5", "0", "0"],
    radius: "1",
    amplitude: "0.04",
  };
  assert.ok(sameFieldInRegion(delta, { ...delta, amplitude: "0.02" }, region));
  assert.ok(sameFieldInRegion(delta, source, region));
  assert.equal(
    sameFieldInRegion(
      delta,
      { ...delta, source: { ...source, radius: "5.01" } },
      region,
    ),
    false,
  );
  assert.equal(
    sameFieldInRegion(delta, { ...delta, center: ["-2", "0", "0"] }, region),
    false,
  );
  assert.equal(
    sameFieldInRegion(
      delta,
      { op: "offset", source: delta, distance: "0.01" },
      region,
    ),
    false,
  );
});
