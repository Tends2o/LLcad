import type { Store } from "./store.js";
import type { Principal } from "../policy/index.js";
import { hash } from "../semantic-ir/hash.js";
import { CadError, requireThat } from "../semantic-ir/errors.js";

const MAX_DEFLECTION_MM = 0.02;
const plain = (value: any) =>
  value &&
  value.unit === "mm" &&
  value.quality === "preview_only" &&
  value.clip == null &&
  value.resolution == null &&
  value.adaptive == null &&
  value.diagnostic_view == null;

/** Decode only enough of an immutable preview to verify complete output binding. */
function previewHeader(data: Buffer, binary: boolean) {
  if (!binary)
    return { header: JSON.parse(data.toString("utf8")), bytes: null };
  if (
    data.length < 16 ||
    data.toString("ascii", 0, 4) !== "MFPV" ||
    data.readUInt32LE(4) !== 1
  )
    return null;
  const length = data.readUInt32LE(8),
    pad = data.readUInt32LE(12);
  const base = 16 + length + pad;
  if (pad > 3 || base > data.length || base % 4 !== 0) return null;
  const header = JSON.parse(data.toString("utf8", 16, 16 + length));
  if (!header || header.format !== "mathforge-preview" || header.version !== 1)
    return null;
  return { header, bytes: data.length - base };
}

function matchesOutputs(
  decoded: ReturnType<typeof previewHeader>,
  outputs: string[],
  facts: Record<string, any>,
  deflection: number,
) {
  if (
    !decoded ||
    !plain(decoded.header) ||
    !Array.isArray(decoded.header.meshes)
  )
    return false;
  const meshes = decoded.header.meshes;
  if (meshes.length !== outputs.length) return false;
  const remaining = new Set(outputs);
  for (const mesh of meshes) {
    if (
      !mesh ||
      !remaining.delete(mesh.feature_id) ||
      mesh.geometry_hash !== facts[mesh.feature_id]?.geometry_hash ||
      mesh.quality !== "preview_only" ||
      mesh.clip != null ||
      mesh.resolution != null ||
      mesh.adaptive != null ||
      !Number.isFinite(mesh.deflection) ||
      mesh.deflection <= 0 ||
      mesh.deflection > deflection
    )
      return false;
    if (decoded.bytes === null) {
      if (!Array.isArray(mesh.vertices) || !Array.isArray(mesh.triangles))
        return false;
    } else {
      for (const [count, offset] of [
        [mesh.vertex_count, mesh.positions_offset],
        [mesh.triangle_count, mesh.indices_offset],
      ])
        if (
          !Number.isSafeInteger(count) ||
          count < 0 ||
          !Number.isSafeInteger(offset) ||
          offset < 0 ||
          offset % 4 !== 0 ||
          offset + count * 12 > decoded.bytes
        )
          return false;
    }
  }
  return remaining.size === 0;
}

/** Read an existing full preview for this exact revision, without compiling,
 *  enqueueing, warming, rewriting cache rows or changing the model head.
 *  This is display data only; native rebuild/edit checks remain untouched. */
export function readStoredPreview(
  store: Store,
  principal: Principal,
  model: string,
  revision: string,
) {
  requireThat(
    typeof revision === "string" && revision.length > 0,
    "INVALID_SCHEMA",
    "A concrete revision is required.",
  );
  let stored;
  try {
    stored = store.revision(principal, model, revision);
  } catch (error) {
    if (error instanceof CadError && error.code === "STALE_REVISION")
      return null;
    throw error;
  }
  const outputs: string[] = stored.ir.outputs ?? [];
  const facts = stored.geometry?.facts;
  if (
    !outputs.length ||
    new Set(outputs).size !== outputs.length ||
    !facts ||
    outputs.some((id) => !facts[id]?.geometry_hash || !facts[id]?.cache_key)
  )
    return null;
  // This is the source digest recorded by the existing tessellation-cache path.
  const sourceHash = hash(
    Object.fromEntries(
      outputs.map((id) => [
        id,
        {
          geometry_hash: facts[id].geometry_hash,
          cache_key: facts[id].cache_key,
        },
      ]),
    ),
  );
  const candidates = store
    .all(
      "SELECT id,manifest,created,mime FROM artifacts WHERE tenant=? AND model=? AND revision=?",
      principal.tenant,
      model,
      revision,
    )
    .flatMap((row: any) => {
      let manifest;
      try {
        manifest = JSON.parse(row.manifest);
      } catch {
        return [];
      }
      if (!manifest || typeof manifest !== "object") return [];
      const binary = manifest.filename === "preview.bin";
      if (
        !plain(manifest) ||
        manifest.model_id !== model ||
        manifest.revision !== revision ||
        manifest.source_geometry_hash !== sourceHash ||
        manifest.engine_build !== stored.geometry.engine_build ||
        !Number.isFinite(manifest.requested_deflection) ||
        manifest.requested_deflection <= 0 ||
        manifest.requested_deflection > MAX_DEFLECTION_MM ||
        (binary
          ? row.mime !== "model/x-mathforge-preview" ||
            manifest.encoding !== "mathforge-preview-binary-1"
          : manifest.filename !== "preview.json" ||
            row.mime !== "application/json")
      )
        return [];
      return [{ ...row, manifest, binary }];
    })
    .sort(
      (a: any, b: any) =>
        a.manifest.requested_deflection - b.manifest.requested_deflection ||
        Number(b.binary) - Number(a.binary) ||
        b.created.localeCompare(a.created) ||
        a.id.localeCompare(b.id),
    );
  for (const candidate of candidates) {
    // Repeat ordinary artifact authorization, including model sharing rules.
    const artifact = store.getArtifact(principal, candidate.id);
    try {
      const data = store.readBlob(artifact.hash); // Verifies content-addressed SHA-256.
      if (
        data.length !== artifact.size ||
        !matchesOutputs(
          previewHeader(data, candidate.binary),
          outputs,
          facts,
          candidate.manifest.requested_deflection,
        )
      )
        continue;
    } catch (error) {
      // An unavailable, damaged or malformed preview is not a usable fallback.
      if (
        error instanceof SyntaxError ||
        (error as any).code === "ENOENT" ||
        (error instanceof CadError && error.code === "INTEGRITY_FAILURE")
      )
        continue;
      throw error;
    }
    return {
      revision,
      stored: true as const,
      artifacts: [
        {
          artifact_id: artifact.id,
          uri: `cad://artifacts/${artifact.id}/manifest`,
          download: `/api/artifacts/${artifact.id}`,
          hash: artifact.hash,
          mime: artifact.mime,
          size: artifact.size,
          manifest: artifact.manifest,
        },
      ],
    };
  }
  return null;
}
