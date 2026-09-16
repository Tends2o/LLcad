import test from "node:test";
import assert from "node:assert/strict";
import { setup, call, finish, importFixture, principal } from "../helpers.js";
import { ModelIR } from "../../packages/semantic-ir/schema.js";
import { id } from "../../packages/semantic-ir/hash.js";
import { isoBasicProfile, threadFit } from "../../packages/compiler/threads.js";
import { compile } from "../../packages/compiler/index.js";
const q = (value: string, unit = "mm") => ({ value, unit });

test("ISO basic thread profiles are derived from a versioned library without claiming conformity", () => {
  const m6 = isoBasicProfile("M6");
  assert.equal(m6.pitch_mm, 1);
  assert.ok(Math.abs(m6.minor_diameter_mm - 4.917468) < 1e-6);
  assert.ok(Math.abs(m6.pitch_diameter_mm - 5.350481) < 1e-6);
  assert.ok(Math.abs(m6.values.tooth_depth - 0.541266) < 1e-6);
  assert.equal(m6.values.crest_width, 0.125);
  assert.equal(m6.conformity, "basic_profile_only_not_certified");
  assert.equal(isoBasicProfile("M8x1").pitch_source, "explicit_fine_pitch");
  assert.throws(
    () => isoBasicProfile("M7"),
    (e: any) => e.code === "OUT_OF_SCOPE",
  );
  assert.throws(
    () => isoBasicProfile("6mm"),
    (e: any) => e.code === "INVALID_SCHEMA",
  );
  const thread = (fid: string, mode: string, depends: string[] = []) => ({
    id: fid,
    semantic_name: fid,
    kind: "thread",
    parameters: { height: q("6") },
    construction: {
      operator: "thread",
      mode,
      handedness: "right",
      standard: "iso_metric_basic",
      designation: "M6",
    },
    depends_on: depends,
  });
  const plan = compile(
    ModelIR.parse({
      schema_version: "1",
      unit: "mm",
      features: [thread("ext", "external")],
      outputs: ["ext"],
    }),
  );
  assert.equal(plan.features[0].thread_profile.designation, "M6");
  assert.ok(Math.abs(plan.features[0].values.root_radius - 2.458734) < 1e-6);
  assert.throws(
    () =>
      compile(
        ModelIR.parse({
          schema_version: "1",
          unit: "mm",
          features: [
            {
              ...thread("ext", "external"),
              parameters: { height: q("6"), pitch: q("1") },
            },
          ],
          outputs: ["ext"],
        }),
      ),
    (e: any) => e.code === "INVALID_SCHEMA",
  );
  const fit = threadFit(
    thread("ext", "external") as any,
    thread("int", "internal", ["base"]) as any,
  );
  assert.equal(fit.designation_match, true);
  assert.equal(fit.tolerance_class_fit_modeled, false);
  assert.equal(fit.conformity, "not_certified");
});

test("ISO thread with runout, offset solid and twisted sweep build through the isolated worker with reports", async () => {
  const env = setup(),
    s = env.service;
  try {
    const ir = ModelIR.parse({
      schema_version: "1",
      unit: "mm",
      features: [
        {
          id: "bolt",
          semantic_name: "M6-Außengewinde",
          kind: "thread",
          parameters: { height: q("6"), runout: q("1") },
          construction: {
            operator: "thread",
            mode: "external",
            handedness: "right",
            standard: "iso_metric_basic",
            designation: "M6",
          },
        },
        {
          id: "nut-body",
          semantic_name: "Mutterrohling",
          kind: "box",
          parameters: {
            width: q("12"),
            depth: q("12"),
            height: q("6"),
            x: q("-6"),
            y: q("-6"),
          },
          construction: { operator: "box" },
        },
        {
          id: "nut",
          semantic_name: "M6-Innengewinde",
          kind: "thread",
          parameters: { height: q("6") },
          construction: {
            operator: "thread",
            mode: "internal",
            handedness: "right",
            standard: "iso_metric_basic",
            designation: "M6",
          },
          depends_on: ["nut-body"],
        },
        {
          id: "shell-base",
          semantic_name: "Grundquader",
          kind: "box",
          parameters: {
            width: q("4"),
            depth: q("4"),
            height: q("4"),
            x: q("20"),
          },
          construction: { operator: "box" },
        },
        {
          id: "grown",
          semantic_name: "Versetzter Körper",
          kind: "offset_solid",
          parameters: { distance: q("1") },
          construction: { operator: "offset_solid" },
          depends_on: ["shell-base"],
        },
        {
          id: "profile",
          semantic_name: "Kreisprofil",
          kind: "circle",
          parameters: { radius: q("1"), x: q("40") },
          construction: { operator: "circle" },
        },
        {
          id: "path",
          semantic_name: "Pfad",
          kind: "line",
          parameters: {},
          construction: {
            operator: "line",
            start: ["40", "0", "0"],
            end: ["40", "0", "8"],
          },
        },
        {
          id: "twisted",
          semantic_name: "Verdrehter Sweep",
          kind: "sweep",
          parameters: {
            twist: q("90", "deg"),
            scale_end: q("0.5", "1"),
            sections: q("8", "1"),
          },
          construction: { operator: "sweep", frame: "rotation_minimizing" },
          depends_on: ["profile", "path"],
        },
      ],
      outputs: ["bolt", "nut", "grown", "twisted"],
    });
    const m = await importFixture(s, ir);
    const facts = s.store.revision(principal, m.model_id).geometry.facts;
    assert.equal(
      facts.twisted.construction_report.frame,
      "rotation_minimizing_double_reflection",
    );
    assert.ok(facts.grown.volume > 64);
    assert.ok(facts.bolt.volume > 0);
    assert.ok(facts.nut.volume < 12 * 12 * 6);
    const fit = call(s, "cad_measure", {
      model_id: m.model_id,
      metric: "thread_fit",
      feature_id: "bolt",
      other_feature_id: "nut",
    });
    assert.equal(fit.measurements.designation_match, true);
    assert.equal(fit.measurements.pitch_match, true);
    assert.equal(fit.measurements.tolerance_class_fit_modeled, false);
    assert.equal(
      s.call(principal, "cad_measure", {
        model_id: m.model_id,
        metric: "thread_fit",
        feature_id: "bolt",
        other_feature_id: "grown",
      }).errors[0].code,
      "OUT_OF_SCOPE",
    );
    const inspected = call(s, "cad_inspect", {
      model_id: m.model_id,
      feature_id: "grown",
    });
    assert.ok(
      inspected.face_page.faces.every((f: any) => f.origins.length > 0),
    );
    const preview = await finish(
      s,
      call(s, "cad_render", {
        model_id: m.model_id,
        feature_id: "twisted",
        idempotency_key: id("render"),
      }),
    );
    assert.ok(preview.artifacts.length > 0);
  } finally {
    await env.close();
  }
});
