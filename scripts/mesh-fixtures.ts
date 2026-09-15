import { ModelIR } from "../packages/semantic-ir/schema.js";
export function meshSTL(open = false, reverse = false, size = 1, offset = 0) {
  const vertices = [
    [0, 0, 0],
    [1, 0, 0],
    [1, 1, 0],
    [0, 1, 0],
    [0, 0, 1],
    [1, 0, 1],
    [1, 1, 1],
    [0, 1, 1],
  ].map((p) => p.map((v) => offset + v * size));
  const triangles = [
    [0, 2, 1],
    [0, 3, 2],
    [4, 5, 6],
    [4, 6, 7],
    [0, 1, 5],
    [0, 5, 4],
    [1, 2, 6],
    [1, 6, 5],
    [2, 3, 7],
    [2, 7, 6],
    [3, 0, 4],
    [3, 4, 7],
  ];
  if (open) triangles.pop();
  return (
    "solid test\n" +
    triangles
      .map(
        (t) =>
          "facet normal 0 0 0\nouter loop\n" +
          (reverse ? t.toReversed() : t)
            .map((i) => "vertex " + vertices[i].join(" "))
            .join("\n") +
          "\nendloop\nendfacet",
      )
      .join("\n") +
    "\nendsolid test\n"
  );
}
export function meshIR(artifact_id: string) {
  return ModelIR.parse({
    schema_version: "1",
    unit: "mm",
    profile: "watertight_solid",
    features: [
      {
        id: "mesh-body",
        semantic_name: "Importierter Netzkörper",
        kind: "imported",
        authoritative_representation: "mesh",
        parameters: {},
        construction: {
          operator: "imported",
          format: "stl",
          source_unit: "mm",
          artifact_id,
        },
      },
    ],
    outputs: ["mesh-body"],
  });
}
