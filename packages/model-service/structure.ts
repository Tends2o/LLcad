import { compileStructure } from "../compiler/structure.js";
export function structurePage(revision: any, args: any) {
  const compiled = compileStructure(revision.ir),
    s = compiled.structure;
  const allParts = (assembly: string): Set<string> => {
    const assemblies = new Set([assembly]);
    for (let i = 0; i < s.assemblies.length; i++)
      for (const a of s.assemblies)
        if (a.parent_assembly && assemblies.has(a.parent_assembly))
          assemblies.add(a.id);
    return new Set(
      s.parts
        .filter((p) => p.assembly && assemblies.has(p.assembly))
        .map((p) => p.id),
    );
  };
  const entries =
    args.kind === "project"
      ? [s.project]
      : args.kind === "assembly"
        ? s.assemblies
        : args.kind === "part"
          ? s.parts
          : s.frames;
  const tokens = args.query.toLocaleLowerCase().split(/\s+/).filter(Boolean);
  const matches = entries.filter(
    (e) =>
      (!args.entity_id || e.id === args.entity_id) &&
      tokens.every((t: string) =>
        `${e.id} ${e.semantic_name} ${e.purpose?.value ?? ""}`
          .toLocaleLowerCase()
          .includes(t),
      ),
  );
  return {
    revision: revision.id,
    unit: revision.ir.unit,
    structure_hash: compiled.structure_hash,
    stored_explicitly: !!revision.ir.structure,
    kind: args.kind,
    total_matches: matches.length,
    entries: matches
      .slice(args.offset, args.offset + args.limit)
      .map((entity: any) => {
        const partIds =
          args.kind === "part"
            ? new Set([entity.id])
            : args.kind === "assembly"
              ? allParts(entity.id)
              : new Set(s.parts.map((p) => p.id));
        const features = revision.ir.features.filter(
          (f: any) =>
            partIds.has(f.owner_part) &&
            (args.kind !== "frame" ||
              f.local_frame === entity.id ||
              compiled.placements[f.local_frame].path.includes(entity.id)),
        );
        const outputIds =
          args.kind === "project"
            ? revision.ir.outputs
            : args.kind === "frame"
              ? features.map((f: any) => f.id)
              : s.parts
                  .filter((p) => partIds.has(p.id))
                  .flatMap((p) => p.outputs);
        const bounds = outputIds
          .map((id: string) => revision.geometry?.facts?.[id]?.bounds)
          .filter(Boolean);
        const worldBounds = bounds.length
          ? [
              ...Array.from({ length: 3 }, (_, i) =>
                Math.min(...bounds.map((b: number[]) => b[i])),
              ),
              ...Array.from({ length: 3 }, (_, i) =>
                Math.max(...bounds.map((b: number[]) => b[i + 3])),
              ),
            ]
          : null;
        return {
          entity_id: entity.id,
          semantic_name: entity.semantic_name,
          definition: entity,
          unit: revision.ir.unit,
          world_bounds: worldBounds,
          bounds_coverage:
            args.kind === "frame"
              ? "features_in_frame_subtree"
              : "declared_outputs",
          feature_count: features.length,
          part_count:
            args.kind === "frame"
              ? new Set(features.map((f: any) => f.owner_part)).size
              : partIds.size,
          revision_quality: revision.quality,
          output_status:
            outputIds.length &&
            outputIds.every(
              (id: string) => revision.geometry?.facts?.[id]?.valid,
            )
              ? "evaluated_with_revision_profile"
              : "not_evaluated",
          relationships: [
            ...new Set(
              features.flatMap((f: any) =>
                f.depends_on.filter(
                  (id: string) => !features.some((g: any) => g.id === id),
                ),
              ),
            ),
          ],
          provenance: entity.purpose ?? null,
          placement:
            args.kind === "frame"
              ? compiled.placements[entity.id]
              : compiled.placements[entity.local_frame ?? "world"],
        };
      }),
    next_offset:
      args.offset + args.limit < matches.length
        ? args.offset + args.limit
        : null,
    counts: {
      assemblies: s.assemblies.length,
      parts: s.parts.length,
      frames: s.frames.length,
    },
  };
}
