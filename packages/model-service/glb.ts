import { requireThat } from "../semantic-ir/errors.js";
import { relativePositions } from "../derived-geometry/coordinates.js";
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
  for (const m of preview.meshes) {
    const local = relativePositions(m.vertices, 1000),
      v = local.positions;
    const t = new Uint32Array(m.triangles.flat());
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
      translation: [
        local.origin[0] / 1000,
        local.origin[2] / 1000,
        -local.origin[1] / 1000,
      ],
      extras: {
        feature_id: m.feature_id,
        source_coordinate_system: "right_handed_z_up",
        vertex_coordinates: "relative_to_mesh_origin",
        mesh_origin_mm: local.origin,
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
  // Read back the actual serialized vertex/index bytes and node transforms.
  // glTF applies the quaternion first, then the Y-up metre translation.
  const binaryStart = 28 + buffer.readUInt32LE(12);
  let maxError = 0,
    verticesChecked = 0,
    indicesChecked = 0;
  const restoredMeshes: { vertices: number[][]; triangles: number[][] }[] = [];
  for (const [i, node] of restored.nodes.entries()) {
    const restoredVertices: number[][] = [];
    const source = preview.meshes[i],
      primitive = restored.meshes[node.mesh].primitives[0];
    const accessor = restored.accessors[primitive.attributes.POSITION],
      view = restored.bufferViews[accessor.bufferView];
    const [qx, qy, qz, qw] = node.rotation;
    for (let j = 0; j < accessor.count; j++) {
      const p = [0, 1, 2].map((k) =>
        buffer.readFloatLE(binaryStart + view.byteOffset + 12 * j + 4 * k),
      );
      const [x, y, z] = p;
      const uv = [qy * z - qz * y, qz * x - qx * z, qx * y - qy * x];
      const uuv = [
        qy * uv[2] - qz * uv[1],
        qz * uv[0] - qx * uv[2],
        qx * uv[1] - qy * uv[0],
      ];
      const world = p.map(
        (v, k) => v + 2 * (qw * uv[k] + uuv[k]) + node.translation[k],
      );
      const restoredMM = [world[0] * 1000, -world[2] * 1000, world[1] * 1000];
      restoredVertices.push(restoredMM);
      for (let k = 0; k < 3; k++)
        maxError = Math.max(
          maxError,
          Math.abs(restoredMM[k] - source.vertices[j][k]),
        );
      verticesChecked++;
    }
    const indices = restored.accessors[primitive.indices],
      indexView = restored.bufferViews[indices.bufferView];
    requireThat(
      indices.count === source.triangles.length * 3 &&
        accessor.count === source.vertices.length,
      "INTEGRITY_FAILURE",
      "GLB-Geometrieanzahl hat sich beim Roundtrip verändert.",
    );
    for (let j = 0; j < indices.count; j++) {
      requireThat(
        buffer.readUInt32LE(binaryStart + indexView.byteOffset + 4 * j) ===
          source.triangles[Math.floor(j / 3)][j % 3],
        "INTEGRITY_FAILURE",
        "GLB-Dreiecksindex hat sich beim Roundtrip verändert.",
      );
      indicesChecked++;
    }
    restoredMeshes.push({
      vertices: restoredVertices,
      triangles: source.triangles,
    });
  }
  requireThat(
    Number.isFinite(maxError),
    "PRECISION_UNSUPPORTED",
    "GLB-Roundtrip enthält nichtendliche Koordinaten.",
  );
  return {
    buffer,
    restored_meshes: restoredMeshes,
    report: {
      status: "checks_passed_within_profile",
      method: "GLB_serialized_positions_indices_and_node_transform_roundtrip",
      max_coordinate_error_mm: maxError,
      coordinate_storage:
        "per_mesh_local_float32_metres_with_JSON_node_translation",
      vertices_checked: verticesChecked,
      indices_checked: indicesChecked,
      consumer_numeric_precision_certified: false,
      mesh_count: meshes.length,
      certified_surface_bound: null,
    },
  };
}
