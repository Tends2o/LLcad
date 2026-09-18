import { relativePositions } from "../derived-geometry/coordinates.js";
/** The compact twin of preview.json: one JSON header, then per mesh the Float32
 *  positions relative to the mesh origin and the Uint32 triangle indices.
 *  Same content, about a third of the bytes, and the browser uploads the
 *  arrays straight to the GPU instead of parsing hundreds of thousands of
 *  number lists. preview.json stays the authoritative artifact. */
export const PREVIEW_BINARY_MIME = "model/x-mathforge-preview";
export const PREVIEW_BINARY_MAGIC = "MFPV";
export type PackedMesh = { meta: Record<string, unknown>; chunk: Buffer };
/** One mesh as its header entry (everything but the coordinates) and its bytes. */
export function packMesh(m: any): PackedMesh {
  const local = relativePositions(m.vertices),
    positions = local.positions,
    indices = new Uint32Array(m.triangles.length * 3);
  for (let i = 0; i < m.triangles.length; i++) {
    const t = m.triangles[i];
    indices[3 * i] = t[0];
    indices[3 * i + 1] = t[1];
    indices[3 * i + 2] = t[2];
  }
  const { vertices: _v, triangles: _t, ...rest } = m;
  return {
    meta: {
      ...rest,
      origin: local.origin,
      coordinate_rounding_mm: local.maxCoordinateError,
      vertex_count: positions.length / 3,
      triangle_count: m.triangles.length,
    },
    chunk: Buffer.concat([
      Buffer.from(positions.buffer, positions.byteOffset, positions.byteLength),
      Buffer.from(indices.buffer, indices.byteOffset, indices.byteLength),
    ]),
  };
}
/** The file: 16-byte head, JSON header, padding, then the mesh bytes in order. */
export function packPreview(
  summary: Record<string, unknown>,
  parts: PackedMesh[],
): Buffer {
  let offset = 0;
  const meshes = parts.map(({ meta, chunk }) => {
    const entry = {
      ...meta,
      positions_offset: offset,
      indices_offset: offset + (meta.vertex_count as number) * 12,
    };
    offset += chunk.length;
    return entry;
  });
  const header = Buffer.from(
    JSON.stringify({
      format: "mathforge-preview",
      version: 1,
      ...summary,
      meshes,
    }),
    "utf8",
  );
  const pad = (4 - (header.length % 4)) % 4,
    head = Buffer.alloc(16);
  head.write(PREVIEW_BINARY_MAGIC, 0, "ascii");
  head.writeUInt32LE(1, 4);
  head.writeUInt32LE(header.length, 8);
  head.writeUInt32LE(pad, 12);
  return Buffer.concat([
    head,
    header,
    Buffer.alloc(pad),
    ...parts.map((p) => p.chunk),
  ]);
}
export function packPreviewBinary(preview: any): Buffer {
  const { meshes, ...summary } = preview;
  return packPreview(summary, meshes.map(packMesh));
}
