import { ModelIR, Feature } from "../semantic-ir/schema.js";

const q = (n: number) => ({ value: String(n), unit: "mm" as const });
const feature = (
  id: string,
  operator: string,
  dimensions: Record<string, number>,
  extra = {},
) =>
  Feature.parse({
    id,
    semantic_name: id,
    kind: operator,
    parameters: Object.fromEntries(
      Object.entries(dimensions).map(([k, v]) => [k, q(v)]),
    ),
    construction: { operator, ...extra },
  });

/** Public reference specification. Neither corruption cases nor an answer key. */
export const body = feature("housing", "box", {
  width: 40,
  depth: 80,
  height: 8,
  x: -20,
  y: -40,
  z: 0,
});
export const wheel = feature("wheel", "cylinder", {
  radius: 8,
  height: 8,
  x: 28,
  y: 20,
  z: 0,
});
export const label = feature(
  "label",
  "strip",
  { width: 1.5, height: 0.6 },
  {
    paths: [
      [
        ["-7", "-3", "8.1"],
        ["7", "11", "8.1"],
      ],
      [
        ["-7", "11", "8.1"],
        ["7", "-3", "8.1"],
      ],
    ],
  },
);
export const referenceIR = ModelIR.parse({
  schema_version: "1",
  unit: "mm",
  features: [body, wheel, label],
  outputs: [body.id, wheel.id, label.id],
});

export const viewContract = {
  version: 1,
  scope: "synthetic registered front view, not a product reconstruction",
  view: {
    width: 640,
    height: 704,
    bounds_mm: [-50, 50, -55, 55],
    direction: "+Z",
  },
  colors: {
    housing: [150, 160, 170],
    wheel: [110, 125, 140],
    label: [20, 25, 30],
  },
  checks: [
    {
      id: "housing-width",
      metric: "relative_width_error",
      region_mm: [-24, 24, -35, -20],
      limit: 0.02,
    },
    {
      id: "wheel",
      metric: "foreground_xor_fraction",
      region_mm: [20.5, 39, 9, 31],
      limit: 0.02,
    },
    {
      id: "label",
      metric: "foreground_xor_fraction",
      threshold: 60,
      region_mm: [-10, 10, -6, 14],
      limit: 0,
    },
    {
      id: "whole-view",
      metric: "changed_pixel_fraction",
      region_mm: [-50, 50, -55, 55],
      limit: 0.02,
    },
    {
      id: "unexpected-foreground",
      metric: "unexpected_foreground_fraction",
      region_mm: [-50, 50, -55, 55],
      limit: 0,
    },
  ],
};
