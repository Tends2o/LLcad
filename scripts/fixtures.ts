import { mkdirSync, writeFileSync } from "node:fs";
import { ModelIR } from "../packages/semantic-ir/schema.js";
const q = (value: string, unit = "mm") => ({ value, unit });
const feature = (
  id: string,
  name: string,
  operator: string,
  parameters: Record<string, unknown>,
  depends_on: string[] = [],
  extra = {},
) => ({
  id,
  semantic_name: name,
  kind: operator,
  parameters,
  construction: { operator, ...extra },
  depends_on,
});
export const housing = ModelIR.parse({
  schema_version: "1",
  unit: "mm",
  features: [
    feature("feat-base", "Gehäuseboden", "box", {
      width: q("40"),
      depth: q("40"),
      height: q("3"),
      x: q("-20"),
      y: q("-20"),
    }),
    feature(
      "feat-groove-07",
      "innere Dichtungsnut",
      "groove",
      { radius: q("10"), width: q("1.20"), depth: q("0.80"), z: q("3") },
      ["feat-base"],
    ),
    feature(
      "feat-hole-01",
      "Befestigungsbohrung",
      "hole",
      { radius: q("1.5"), depth: q("3"), x: q("15"), y: q("15"), z: q("3") },
      ["feat-groove-07"],
    ),
  ],
  outputs: ["feat-hole-01"],
  constraints: [
    {
      id: "constraint-width",
      kind: "protected_parameter",
      feature_id: "feat-groove-07",
      parameter: "width",
    },
    {
      id: "constraint-hole-radius",
      kind: "protected_parameter",
      feature_id: "feat-hole-01",
      parameter: "radius",
    },
    {
      id: "constraint-hole-x",
      kind: "protected_parameter",
      feature_id: "feat-hole-01",
      parameter: "x",
    },
    {
      id: "constraint-hole-y",
      kind: "protected_parameter",
      feature_id: "feat-hole-01",
      parameter: "y",
    },
    {
      id: "constraint-outer",
      kind: "protected_bounds",
      feature_id: "feat-hole-01",
      tolerance: q("0.00001"),
    },
    {
      id: "constraint-wall",
      kind: "minimum",
      feature_id: "feat-groove-07",
      metric: "remaining_wall",
      target: q("2.00"),
    },
  ],
});
export const sphere = ModelIR.parse({
  schema_version: "1",
  unit: "mm",
  features: [
    feature("sphere", "Analytische Kugel", "sphere", { radius: q("10") }),
  ],
  outputs: ["sphere"],
});
export const organic = ModelIR.parse({
  schema_version: "1",
  unit: "mm",
  profile: "render_surface",
  features: [
    {
      id: "organic",
      semantic_name: "Organischer Grundkörper",
      kind: "field",
      authoritative_representation: "implicit",
      parameters: {},
      construction: {
        operator: "field",
        expression: { op: "sphere", center: ["0", "0", "0"], radius: "5" },
        domain: { min: ["-7", "-7", "-7"], max: ["7", "7", "7"] },
        cell_size: q("0.75"),
      },
    },
  ],
  outputs: ["organic"],
  constraints: [
    {
      id: "remote-protected",
      kind: "protected_region",
      feature_id: "organic",
      min: ["-7", "-7", "-7"],
      max: ["-2", "7", "7"],
    },
  ],
});
export const assembly = ModelIR.parse({
  schema_version: "1",
  unit: "mm",
  features: [
    feature("pin", "Stift", "cylinder", { radius: q("0.5"), height: q("2") }),
    feature(
      "pattern",
      "Stiftreihe",
      "pattern",
      { count: q("100", "1"), dx: q("2") },
      ["pin"],
    ),
  ],
  outputs: ["pattern"],
});
if (process.argv[1]?.endsWith("fixtures.ts"))
  for (const [path, data] of Object.entries({
    "fixtures/micro-details/housing.json": housing,
    "fixtures/analytic/sphere.json": sphere,
    "fixtures/organic/sphere.json": organic,
    "fixtures/assemblies/pins.json": assembly,
  }))
    writeFileSync(path, JSON.stringify(data, null, 2) + "\n");
