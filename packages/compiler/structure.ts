import { ModelIR, ModelStructure } from "../semantic-ir/schema.js";
import { hash } from "../semantic-ir/hash.js";
import { quantity } from "../semantic-ir/units.js";
import { requireThat } from "../semantic-ir/errors.js";
type Matrix = number[][];
export type Placement = {
  rotation: Matrix;
  translation: number[];
  path: string[];
  hash: string;
};
const identity = [
  [1, 0, 0],
  [0, 1, 0],
  [0, 0, 1],
];
const mv = (m: Matrix, v: number[]) =>
  m.map((row) => row.reduce((sum, n, i) => sum + n * v[i], 0));
const mm = (a: Matrix, b: Matrix) =>
  a.map((row) =>
    b[0].map((_, j) => row.reduce((sum, n, k) => sum + n * b[k][j], 0)),
  );
export function structureOf(ir: ModelIR) {
  if (ir.structure) return ir.structure;
  const parts = new Map<string, any>();
  for (const f of ir.features) {
    const prior = parts.get(f.owner_part);
    requireThat(
      !prior ||
        prior.authoritative_representation === f.authoritative_representation,
      "OUT_OF_SCOPE",
      "Ein Teil darf nur eine maßgebliche geometrische Repräsentation besitzen. Verschiedene Repräsentationen benötigen getrennte Teile.",
    );
    if (!prior)
      parts.set(f.owner_part, {
        id: f.owner_part,
        semantic_name: f.owner_part,
        local_frame: "world",
        authoritative_representation: f.authoritative_representation,
        outputs: [],
      });
    if (ir.outputs.includes(f.id)) parts.get(f.owner_part).outputs.push(f.id);
  }
  return ModelStructure.parse({
    project: { id: "project-main", semantic_name: "Modellprojekt" },
    frames: [],
    assemblies: [],
    parts: [...parts.values()],
  });
}
export const contextHash = (f: ModelIR["features"][number]) =>
  hash({ owner_part: f.owner_part, local_frame: f.local_frame });
export function compileStructure(ir: ModelIR) {
  const structure = structureOf(ir);
  const ids = [
    structure.project.id,
    ...structure.frames.map((f) => f.id),
    ...structure.assemblies.map((a) => a.id),
    ...structure.parts.map((p) => p.id),
  ];
  requireThat(
    new Set(ids).size === ids.length &&
      !ids.includes("world") &&
      ir.features.every((f) => !ids.includes(f.id)),
    "INVALID_SCHEMA",
    "Struktureinheiten benötigen eindeutige IDs; world ist reserviert.",
  );
  const placements: Record<string, Placement> = {
    world: {
      rotation: identity,
      translation: [0, 0, 0],
      path: [],
      hash: hash("world"),
    },
  };
  const frames = new Map(structure.frames.map((f) => [f.id, f]));
  const visiting = new Set<string>();
  const frame = (fid: string, depth = 0): Placement => {
    if (Object.hasOwn(placements, fid)) return placements[fid];
    const f = frames.get(fid);
    requireThat(f, "INVALID_SCHEMA", "Unbekannter lokaler Bezugsrahmen.");
    requireThat(
      !visiting.has(fid),
      "CONSTRAINT_CONFLICT",
      "Zyklische Bezugsrahmen.",
    );
    requireThat(
      depth < 16,
      "BUDGET_EXCEEDED",
      "Bezugsrahmenhierarchie ist zu tief.",
    );
    visiting.add(fid);
    const parent = frame(f.parent, depth + 1);
    requireThat(
      parent.path.length < 16,
      "BUDGET_EXCEEDED",
      "Bezugsrahmenhierarchie ist zu tief.",
    );
    const axis = f.axis.map(Number),
      norm = Math.hypot(...axis),
      origin = f.translation.map(Number),
      angle = quantity(f.angle, "angle");
    requireThat(
      [...axis, ...origin].every(
        (v) => Number.isFinite(v) && Math.abs(v) <= 1e6,
      ) &&
        norm >= 1e-12 &&
        Math.abs(angle) <= 2 * Math.PI,
      "GEOMETRY_INVALID",
      "Ungültige starre Rahmenplatzierung.",
    );
    const u = axis.map((n) => n / norm),
      c = Math.cos(angle),
      s = Math.sin(angle),
      cross = [
        [0, -u[2], u[1]],
        [u[2], 0, -u[0]],
        [-u[1], u[0], 0],
      ];
    const local = identity.map((row, i) =>
      row.map((v, j) => v * c + (1 - c) * u[i] * u[j] + cross[i][j] * s),
    );
    const rotation = mm(parent.rotation, local),
      translation = mv(parent.rotation, origin).map(
        (n, i) => n + parent.translation[i],
      );
    requireThat(
      translation.every((v) => Number.isFinite(v) && Math.abs(v) <= 1e6),
      "GEOMETRY_INVALID",
      "Zusammengesetzter Rahmen überschreitet den Weltkoordinatenbereich.",
    );
    const gram = mm(
      rotation,
      rotation[0].map((_, i) => rotation.map((row) => row[i])),
    );
    requireThat(
      gram.every((row, i) =>
        row.every((v, j) => Math.abs(v - identity[i][j]) <= 1e-12),
      ),
      "PRECISION_UNSUPPORTED",
      "Zusammengesetzte Rahmendrehung ist numerisch nicht orthogonal.",
    );
    const det =
      rotation[0][0] *
        (rotation[1][1] * rotation[2][2] - rotation[1][2] * rotation[2][1]) -
      rotation[0][1] *
        (rotation[1][0] * rotation[2][2] - rotation[1][2] * rotation[2][0]) +
      rotation[0][2] *
        (rotation[1][0] * rotation[2][1] - rotation[1][1] * rotation[2][0]);
    requireThat(
      Math.abs(det - 1) <= 1e-12,
      "PRECISION_UNSUPPORTED",
      "Bezugsrahmen muss orientierungserhaltend bleiben.",
    );
    const result = {
      rotation,
      translation,
      path: [...parent.path, fid],
      hash: hash({
        parent: parent.hash,
        translation: f.translation,
        axis: f.axis,
        angle: f.angle,
      }),
    };
    placements[fid] = result;
    visiting.delete(fid);
    return result;
  };
  structure.frames.forEach((f) => frame(f.id));
  const descendant = (child: string, parent: string) => {
    frame(child);
    frame(parent);
    return (
      child === parent ||
      parent === "world" ||
      placements[child].path.includes(parent)
    );
  };
  const assemblies = new Map(structure.assemblies.map((a) => [a.id, a]));
  const checked = new Set<string>(),
    checking = new Set<string>();
  const assemblyDepth = new Map<string, number>();
  const assembly = (aid: string, depth = 0) => {
    if (checked.has(aid)) return;
    const a = assemblies.get(aid);
    requireThat(a, "INVALID_SCHEMA", "Unbekannte Baugruppe.");
    requireThat(
      !checking.has(aid) && depth < 16,
      "CONSTRAINT_CONFLICT",
      "Zyklische oder zu tiefe Baugruppenstruktur.",
    );
    checking.add(aid);
    frame(a.local_frame);
    if (a.parent_assembly) {
      assembly(a.parent_assembly, depth + 1);
      requireThat(
        descendant(
          a.local_frame,
          assemblies.get(a.parent_assembly)!.local_frame,
        ),
        "INVALID_SCHEMA",
        "Baugruppenrahmen muss im übergeordneten Rahmen liegen.",
      );
    }
    const totalDepth =
      (a.parent_assembly ? assemblyDepth.get(a.parent_assembly)! : 0) + 1;
    requireThat(
      totalDepth <= 16,
      "BUDGET_EXCEEDED",
      "Baugruppenstruktur ist zu tief.",
    );
    assemblyDepth.set(aid, totalDepth);
    checking.delete(aid);
    checked.add(aid);
  };
  structure.assemblies.forEach((a) => assembly(a.id));
  const parts = new Map(structure.parts.map((p) => [p.id, p]));
  for (const p of parts.values()) {
    frame(p.local_frame);
    if (p.assembly) {
      assembly(p.assembly);
      requireThat(
        descendant(p.local_frame, assemblies.get(p.assembly)!.local_frame),
        "INVALID_SCHEMA",
        "Teilrahmen muss im Baugruppenrahmen liegen.",
      );
    }
    requireThat(
      new Set(p.outputs).size === p.outputs.length &&
        p.outputs.every((id) =>
          ir.features.some((f) => f.id === id && f.owner_part === p.id),
        ),
      "INVALID_SCHEMA",
      "Teilausgaben müssen eindeutige eigene Features sein.",
    );
  }
  for (const f of ir.features) {
    const p = parts.get(f.owner_part);
    requireThat(
      p && p.authoritative_representation === f.authoritative_representation,
      "OUT_OF_SCOPE",
      "Feature benötigt ein eigenes Teil mit genau passender geometrischer Hoheit.",
    );
    requireThat(
      descendant(f.local_frame, p.local_frame),
      "INVALID_SCHEMA",
      "Featurerahmen muss im Bezugsrahmen seines Teils liegen.",
    );
  }
  requireThat(
    ir.outputs.every((id) =>
      structure.parts.some((p) => p.outputs.includes(id)),
    ),
    "INVALID_SCHEMA",
    "Modellausgaben müssen als Teilausgaben registriert sein.",
  );
  return { structure, placements, structure_hash: hash(ir.structure ?? null) };
}
