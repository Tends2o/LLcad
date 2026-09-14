import { requireThat } from "../semantic-ir/errors.js";
/** glTF stores metre coordinates, explicitly converted from the authoritative millimetres. */
export function packGLB(preview: any) {
  const chunks: Buffer[] = [],
    views: any[] = [],
    accessors: any[] = [],
    meshes: any[] = [],
    nodes: any[] = [];
  let offset = 0;
  const append = (b: Buffer, target: number) => {
    const aligned = Buffer.alloc(Math.ceil(b.length / 4) * 4);
    b.copy(aligned);
    const index = views.length;
    views.push({ buffer: 0, byteOffset: offset, byteLength: b.length, target });
    chunks.push(aligned);
    offset += aligned.length;
    return index;
  };
  let maxError = 0;
  for (const m of preview.meshes) {
    const v = new Float32Array(
      m.vertices.flatMap((p: number[]) => p.map((x) => x / 1000)),
    );
    const t = new Uint32Array(m.triangles.flat());
    for (let i = 0; i < v.length; i++)
      maxError = Math.max(
        maxError,
        Math.abs(v[i] * 1000 - m.vertices[Math.floor(i / 3)][i % 3]),
      );
    const min = [Infinity, Infinity, Infinity],
      max = [-Infinity, -Infinity, -Infinity];
    for (let i = 0; i < v.length; i++) {
      min[i % 3] = Math.min(min[i % 3], v[i]);
      max[i % 3] = Math.max(max[i % 3], v[i]);
    }
    const pa = accessors.length;
    accessors.push({
      bufferView: append(Buffer.from(v.buffer), 34962),
      componentType: 5126,
      count: v.length / 3,
      type: "VEC3",
      min,
      max,
    });
    const ia = accessors.length;
    accessors.push({
      bufferView: append(Buffer.from(t.buffer), 34963),
      componentType: 5125,
      count: t.length,
      type: "SCALAR",
    });
    nodes.push({
      mesh: meshes.length,
      name: m.feature_id,
      rotation: [-Math.SQRT1_2, 0, 0, Math.SQRT1_2],
      extras: {
        feature_id: m.feature_id,
        source_coordinate_system: "right_handed_z_up",
      },
    });
    meshes.push({
      primitives: [{ attributes: { POSITION: pa }, indices: ia, mode: 4 }],
    });
  }
  const doc = {
    asset: { version: "2.0", generator: "MathForge 3D" },
    buffers: [{ byteLength: offset }],
    bufferViews: views,
    accessors,
    meshes,
    nodes,
    scenes: [{ nodes: nodes.map((_, i) => i) }],
    scene: 0,
  };
  const json = Buffer.from(JSON.stringify(doc));
  const jsonChunk = Buffer.alloc(Math.ceil(json.length / 4) * 4, 0x20);
  json.copy(jsonChunk);
  const bin = Buffer.concat(chunks);
  const header = Buffer.alloc(12);
  header.write("glTF");
  header.writeUInt32LE(2, 4);
  header.writeUInt32LE(12 + 8 + jsonChunk.length + 8 + bin.length, 8);
  const jh = Buffer.alloc(8);
  jh.writeUInt32LE(jsonChunk.length);
  jh.writeUInt32LE(0x4e4f534a, 4);
  const bh = Buffer.alloc(8);
  bh.writeUInt32LE(bin.length);
  bh.writeUInt32LE(0x004e4942, 4);
  const buffer = Buffer.concat([header, jh, jsonChunk, bh, bin]);
  const restored = JSON.parse(
    buffer.subarray(20, 20 + buffer.readUInt32LE(12)).toString(),
  );
  requireThat(
    restored.asset.version === "2.0" &&
      buffer.readUInt32LE(8) === buffer.length,
    "GEOMETRY_INVALID",
    "GLB-Roundtrip ungültig.",
  );
  return {
    buffer,
    report: {
      status: "checks_passed_within_profile",
      method: "GLB_structure_and_float32_roundtrip",
      max_coordinate_error_mm: maxError,
      mesh_count: meshes.length,
      certified_surface_bound: null,
    },
  };
}
