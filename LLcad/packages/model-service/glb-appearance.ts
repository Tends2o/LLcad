import { requireThat } from "../semantic-ir/errors.js";
/** Add viewer PBR finishes without changing a byte of the checked geometry buffer. */
export function applyGLBAppearance(buffer: Buffer, ir: any, mask: any): Buffer {
  requireThat(
    buffer.toString("ascii", 0, 4) === "glTF" &&
      buffer.readUInt32LE(4) === 2 &&
      buffer.readUInt32LE(8) === buffer.length,
    "INTEGRITY_FAILURE",
    "Invalid GLB header.",
  );
  const length = buffer.readUInt32LE(12);
  requireThat(
    buffer.readUInt32LE(16) === 0x4e4f534a && 20 + length + 8 <= buffer.length,
    "INTEGRITY_FAILURE",
    "GLB does not contain valid JSON.",
  );
  const doc = JSON.parse(buffer.subarray(20, 20 + length).toString()),
    tail = buffer.subarray(20 + length);
  const linear = (hex: string) =>
    [1, 3, 5].map((i) => {
      const s = parseInt(hex.slice(i, i + 2), 16) / 255;
      return s <= 0.04045 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
    });
  const finite = (v: any, fallback: number, min = 0, max = 1) =>
    typeof v === "number" && Number.isFinite(v)
      ? Math.max(min, Math.min(max, v))
      : fallback;
  const materials: any[] = [];
  const used = new Set<string>(doc.extensionsUsed ?? []);
  for (const node of doc.nodes ?? []) {
    if (node.mesh === undefined) continue;
    const feature = ir.features.find(
      (f: any) => f.id === (node.extras?.feature_id ?? node.name),
    );
    const entry = mask.parts[feature?.owner_part];
    if (!entry) continue;
    const transmission = finite(
        entry.transmission,
        entry.finish === "clear glass" ? 0.95 : 0,
      ),
      glass = transmission > 0;
    const colour =
      typeof entry.colour === "string" && /^#[a-f0-9]{6}$/i.test(entry.colour)
        ? entry.colour
        : "#9aa3a8";
    const opacity = glass ? 1 : finite(entry.opacity, 1);
    const m: any = {
      name: entry.name,
      pbrMetallicRoughness: {
        baseColorFactor: [...linear(colour), opacity],
        metallicFactor: finite(entry.metalness, 0.1),
        roughnessFactor: finite(entry.roughness, 0.55),
      },
      doubleSided: true,
      alphaMode: opacity < 1 ? "BLEND" : "OPAQUE",
    };
    if (entry.emissive && /^#[a-f0-9]{6}$/i.test(entry.emissive)) {
      m.emissiveFactor = linear(entry.emissive).map(
        (v) => v * finite(entry.emissive_intensity, 0.4),
      );
    }
    if (glass) {
      used.add("KHR_materials_transmission");
      used.add("KHR_materials_ior");
      used.add("KHR_materials_volume");
      m.extensions = {
        KHR_materials_transmission: { transmissionFactor: transmission },
        KHR_materials_volume: {
          thicknessFactor: finite(entry.thickness_mm, 2, 0, 100) / 1000,
        },
        KHR_materials_ior: { ior: finite(entry.ior, 1.52, 1, 2.333) },
      };
    }
    const index = materials.push(m) - 1;
    for (const primitive of doc.meshes[node.mesh].primitives)
      primitive.material = index;
  }
  doc.materials = materials;
  if (used.size) doc.extensionsUsed = [...used];
  // Visual-only decals travel as data for viewers (never as geometry, never in STEP).
  if (Array.isArray(ir.decals) && ir.decals.length)
    doc.asset = {
      ...(doc.asset ?? { version: "2.0" }),
      extras: { ...(doc.asset?.extras ?? {}), llcad_decals: ir.decals },
    };
  const json = Buffer.from(JSON.stringify(doc)),
    padded = Buffer.alloc(Math.ceil(json.length / 4) * 4, 0x20);
  json.copy(padded);
  const head = Buffer.from(buffer.subarray(0, 20));
  head.writeUInt32LE(20 + padded.length + tail.length, 8);
  head.writeUInt32LE(padded.length, 12);
  const result = Buffer.concat([head, padded, tail]);
  requireThat(
    result.subarray(20 + padded.length).equals(tail),
    "INTEGRITY_FAILURE",
    "The geometry buffer was changed during material export.",
  );
  return result;
}
