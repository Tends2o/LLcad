/** Geometric regression corpus (Bauplan 24.2): boundary classes with declared expectations.
 *  Every case is synthetic; no user geometry is included. Expectations name the exact outcome:
 *  a validated revision with measurable facts, or a specific honest error code. */
import { ModelIR } from "../../packages/semantic-ir/schema.js";
const q = (value: string, unit = "mm") => ({ value, unit });
const feature = (
  id: string,
  operator: string,
  parameters: Record<string, unknown>,
  depends_on: string[] = [],
  extra: Record<string, unknown> = {},
) => ({
  id,
  semantic_name: id,
  kind: operator,
  parameters,
  construction: { operator, ...extra },
  depends_on,
});
const model = (
  features: any[],
  outputs: string[],
  extra: Record<string, unknown> = {},
) =>
  ModelIR.parse({
    schema_version: "1",
    unit: "mm",
    features,
    outputs,
    ...extra,
  });
export type Expectation =
  | {
      outcome: "validated";
      facts?: (facts: any, aggregate: any) => boolean;
      roundtrip?: boolean;
    }
  | { outcome: "error"; code: string };
export const CORPUS: {
  name: string;
  class: string;
  ir: any;
  expect: Expectation;
}[] = [
  {
    name: "strip with crossing paths, round joints and a pad is one solid",
    class: "strip_union_connected",
    ir: model(
      [
        feature(
          "trace",
          "strip",
          { width: q("1"), height: q("0.5") },
          [],
          {
            paths: [
              [
                ["0", "0", "2"],
                ["10", "0", "2"],
              ],
              [
                ["5", "-3", "2"],
                ["5", "3", "2"],
              ],
            ],
            pads: [{ center: ["0", "0", "2"], width: "2", depth: "2" }],
          },
        ),
      ],
      ["trace"],
    ),
    expect: {
      outcome: "validated",
      facts: (f) =>
        f.trace.valid &&
        f.trace.solids === 1 &&
        Math.abs(f.trace.volume - (18 + (3 * Math.PI) / 8) * 0.5) < 1e-6 &&
        Math.abs(f.trace.dimensions.height - 0.5) < 1e-9,
      roundtrip: true,
    },
  },
  {
    name: "strip whose paths do not touch is refused",
    class: "strip_union_disconnected",
    ir: model(
      [
        feature("trace", "strip", { width: q("1"), height: q("0.5") }, [], {
          paths: [
            [
              ["0", "0", "0"],
              ["4", "0", "0"],
            ],
            [
              ["0", "5", "0"],
              ["4", "5", "0"],
            ],
          ],
        }),
      ],
      ["trace"],
    ),
    expect: { outcome: "error", code: "GEOMETRY_INVALID" },
  },
  {
    name: "coplanar cut leaves a valid solid",
    class: "tangential_coplanar_intersection",
    ir: model(
      [
        feature("base", "box", {
          width: q("10"),
          depth: q("10"),
          height: q("10"),
        }),
        feature("tool", "box", {
          width: q("4"),
          depth: q("4"),
          height: q("10"),
          x: q("3"),
          y: q("3"),
        }),
        feature("cut", "difference", {}, ["base", "tool"]),
      ],
      ["cut"],
    ),
    expect: {
      outcome: "validated",
      facts: (f) => Math.abs(f.cut.volume - 840) < 1e-6 && f.cut.solids === 1,
      roundtrip: true,
    },
  },
  {
    name: "face-contact intersection is an explicit null result",
    class: "tangential_coplanar_intersection",
    ir: model(
      [
        feature("a", "box", { width: q("4"), depth: q("4"), height: q("4") }),
        feature("b", "box", {
          width: q("4"),
          depth: q("4"),
          height: q("4"),
          x: q("4"),
        }),
        feature("touch", "intersection", {}, ["a", "b"]),
      ],
      ["touch"],
    ),
    expect: { outcome: "error", code: "GEOMETRY_INVALID" },
  },
  {
    name: "sliver pocket leaves a short edge",
    class: "short_edges",
    ir: model(
      [
        feature("base", "box", {
          width: q("10"),
          depth: q("10"),
          height: q("10"),
        }),
        feature(
          "sliver",
          "pocket",
          {
            width: q("9.99"),
            length: q("4"),
            depth: q("2"),
            x: q("5"),
            y: q("5"),
            z: q("10"),
          },
          ["base"],
        ),
      ],
      ["sliver"],
    ),
    expect: {
      outcome: "validated",
      facts: (f) =>
        f.sliver.valid && f.sliver.volume < 1000 && f.sliver.volume > 900,
    },
  },
  {
    name: "thin shell wall of 50 micrometres",
    class: "thin_walls",
    ir: model(
      [
        feature("block", "box", {
          width: q("10"),
          depth: q("10"),
          height: q("10"),
        }),
        feature("thin", "shell", { thickness: q("0.05") }, ["block"], {
          opening: "top",
        }),
      ],
      ["thin"],
    ),
    expect: {
      outcome: "validated",
      facts: (f) => f.thin.valid && f.thin.volume < 30 && f.thin.volume > 20,
      roundtrip: true,
    },
  },
  {
    name: "small hole of 0.1 mm diameter",
    class: "small_holes",
    ir: model(
      [
        feature("plate", "box", {
          width: q("5"),
          depth: q("5"),
          height: q("1"),
        }),
        feature(
          "pin-hole",
          "hole",
          {
            radius: q("0.05"),
            depth: q("1"),
            x: q("2.5"),
            y: q("2.5"),
            z: q("1"),
          },
          ["plate"],
        ),
      ],
      ["pin-hole"],
    ),
    expect: {
      outcome: "validated",
      facts: (f) =>
        Math.abs(f["pin-hole"].dimensions.depth - 1) < 1e-6 &&
        f["pin-hole"].volume < 25,
    },
  },
  {
    name: "tight fillet on a small block",
    class: "tight_fillets",
    ir: model(
      [
        feature("block", "box", {
          width: q("6"),
          depth: q("6"),
          height: q("6"),
        }),
        feature("rounded", "fillet", { radius: q("0.2") }, ["block"], {
          edge_selector: "all",
        }),
      ],
      ["rounded"],
    ),
    expect: {
      outcome: "validated",
      facts: (f) =>
        f.rounded.valid && f.rounded.volume < 216 && f.rounded.volume > 214,
      roundtrip: true,
    },
  },
  {
    name: "detail at large world coordinates through a local frame",
    class: "large_coordinates",
    ir: model(
      [
        {
          ...feature("base", "box", {
            width: q("10"),
            depth: q("10"),
            height: q("10"),
          }),
          owner_part: "part-far",
          local_frame: "frame-far",
        },
        {
          ...feature(
            "nick",
            "groove",
            { radius: q("3"), width: q("0.2"), depth: q("0.02"), z: q("10") },
            ["base"],
          ),
          owner_part: "part-far",
          local_frame: "frame-far",
        },
      ],
      ["nick"],
      {
        structure: {
          project: { id: "project-far", semantic_name: "Weit entfernt" },
          frames: [
            {
              id: "frame-far",
              semantic_name: "Ferner Rahmen",
              parent: "world",
              translation: ["500000", "-400000", "300000"],
              axis: ["0", "0", "1"],
              angle: q("0", "deg"),
            },
          ],
          assemblies: [],
          parts: [
            {
              id: "part-far",
              semantic_name: "Fernes Teil",
              local_frame: "frame-far",
              authoritative_representation: "brep",
              outputs: ["nick"],
            },
          ],
        },
      },
    ),
    expect: {
      outcome: "validated",
      facts: (f) =>
        Math.abs(f.nick.dimensions.depth - 0.02) < 1e-9 &&
        Math.abs(f.nick.bounds[0] - 500000) < 1e-6,
    },
  },
  {
    name: "internal cavity keeps two shells in one solid",
    class: "cavities",
    ir: model(
      [
        feature("outer", "box", {
          width: q("10"),
          depth: q("10"),
          height: q("10"),
        }),
        feature("void", "box", {
          width: q("4"),
          depth: q("4"),
          height: q("4"),
          x: q("3"),
          y: q("3"),
          z: q("3"),
        }),
        feature("hollow", "difference", {}, ["outer", "void"]),
      ],
      ["hollow"],
    ),
    expect: {
      outcome: "validated",
      facts: (f) =>
        Math.abs(f.hollow.volume - 936) < 1e-6 && f.hollow.solids === 1,
      roundtrip: true,
    },
  },
  {
    name: "open NURBS surface in the render profile",
    class: "open_surfaces",
    ir: model(
      [
        feature("sheet", "nurbs_surface", {}, [], {
          poles: [
            [
              ["0", "0", "0"],
              ["0", "10", "1"],
            ],
            [
              ["10", "0", "1"],
              ["10", "10", "0"],
            ],
          ],
          weights: [
            ["1", "1"],
            ["1", "1"],
          ],
        }),
      ],
      ["sheet"],
      { profile: "render_surface" },
    ),
    expect: {
      outcome: "validated",
      facts: (f) => f.sheet.valid && f.sheet.area > 100,
    },
  },
  {
    name: "two disjoint components in one output",
    class: "components",
    ir: model(
      [
        feature("left", "box", {
          width: q("3"),
          depth: q("3"),
          height: q("3"),
        }),
        feature("right", "box", {
          width: q("3"),
          depth: q("3"),
          height: q("3"),
          x: q("10"),
        }),
        feature("pair", "union", {}, ["left", "right"]),
      ],
      ["pair"],
    ),
    expect: {
      outcome: "validated",
      facts: (f) => f.pair.solids === 2 && Math.abs(f.pair.volume - 54) < 1e-9,
      roundtrip: true,
    },
  },
  {
    name: "mirrored asymmetric body keeps its volume",
    class: "mirroring",
    ir: model(
      [
        feature("wedge", "cone", {
          radius: q("3"),
          top_radius: q("1"),
          height: q("5"),
          x: q("4"),
        }),
        feature("mirrored", "mirror", {}, ["wedge"]),
      ],
      ["mirrored"],
    ),
    expect: {
      outcome: "validated",
      facts: (f) =>
        Math.abs(f.mirrored.volume - (Math.PI * 5 * (9 + 3 + 1)) / 3) < 1e-6 &&
        f.mirrored.bounds[3] <= -1 + 1e-9,
    },
  },
  {
    name: "linear instances of a shared source",
    class: "instances",
    ir: model(
      [
        feature("pin", "cylinder", { radius: q("0.5"), height: q("2") }),
        feature("row", "pattern", { count: q("5", "1"), dx: q("2") }, ["pin"]),
      ],
      ["row"],
    ),
    expect: {
      outcome: "validated",
      facts: (f) =>
        f.row.solids === 5 &&
        Math.abs(f.row.volume - 5 * Math.PI * 0.25 * 2) < 1e-6,
      roundtrip: true,
    },
  },
  {
    name: "difference removing everything",
    class: "null_results",
    ir: model(
      [
        feature("a", "box", { width: q("4"), depth: q("4"), height: q("4") }),
        feature("b", "box", {
          width: q("6"),
          depth: q("6"),
          height: q("6"),
          x: q("-1"),
          y: q("-1"),
          z: q("-1"),
        }),
        feature("nothing", "difference", {}, ["a", "b"]),
      ],
      ["nothing"],
    ),
    expect: { outcome: "error", code: "GEOMETRY_INVALID" },
  },
];
