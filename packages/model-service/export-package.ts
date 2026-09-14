import type { Store } from "./store.js";
import type { Principal } from "../policy/index.js";
import { hash } from "../semantic-ir/hash.js";
import { requireThat } from "../semantic-ir/errors.js";
import { IMPLEMENTATION_HASH } from "../compiler/build.js";

type Artifact = ReturnType<Store["artifact"]>;
/** Private, revision-bound components. Data in sidecars is never executable. */
export function exportPackage(
  store: Store,
  p: Principal,
  revision: any,
  geometry: Artifact[],
  format: string,
) {
  const row = store.get(
    "SELECT validation,plan FROM transactions WHERE committed_revision=? AND tenant=? AND owner=? AND state='committed'",
    revision.id,
    p.tenant,
    p.user,
  );
  requireThat(
    row,
    "VALIDATION_REQUIRED",
    "Der gespeicherte Übernahmenachweis für das Exportpaket fehlt.",
  );
  const validation = JSON.parse(row.validation),
    { digest, ...body } = validation;
  requireThat(
    digest === hash(body) &&
      body.ir_hash === revision.ir_hash &&
      body.geometry_digest === hash(revision.geometry.facts),
    "INTEGRITY_FAILURE",
    "Exportrevision und gespeicherter Prüfnachweis stimmen nicht überein.",
  );
  const save = (filename: string, value: unknown) =>
    store.artifact(
      p,
      JSON.stringify(value, null, 2) + "\n",
      "application/json",
      revision.model,
      revision.id,
      {
        filename,
        package_schema_version: "1",
        model_id: revision.model,
        revision: revision.id,
        kind: "export_sidecar",
        unit: "mm",
      },
    );
  const sources = [
    ...new Set<string>(
      revision.ir.features
        .filter((f: any) => f.construction.operator === "imported")
        .map((f: any) => f.construction.artifact_id),
    ),
  ].map((aid) => {
    const source = store.getArtifact(p, aid);
    // Verify the original asset, including its content hash, before advertising it.
    store.readBlob(source.hash);
    return {
      artifact_id: aid,
      sha256: source.hash,
      byte_length: source.size,
      mime: source.mime,
      download: `/api/artifacts/${aid}`,
    };
  });
  const ir = save("model.ir.json", revision.ir);
  const report = save("validation.json", {
    schema_version: "1",
    model_id: revision.model,
    revision: revision.id,
    committed_candidate_revision:
      validation.candidate_revision ?? validation.candidate,
    validation,
    roundtrips: geometry.map((a) => ({
      filename: a.manifest.filename,
      report: a.manifest.roundtrip ?? null,
    })),
    refinement_reports: JSON.parse(row.plan).refinement_reports ?? [],
  });
  const components = [...geometry, ir, report];
  const manifest = save("manifest.json", {
    schema_version: "1",
    package_kind: "LLcad_revision_export",
    model_id: revision.model,
    revision: revision.id,
    parent_revision: revision.parent,
    source_ir_hash: revision.ir_hash,
    source_geometry_hash: hash(revision.geometry.facts),
    source_registry_hash: validation.registry_hash,
    source_policy_hash: validation.policy_hash,
    exporter_implementation_hash: IMPLEMENTATION_HASH,
    format,
    source_unit: "mm",
    exported_geometry_unit: format === "glb" ? "m" : "mm",
    coordinate_convention: {
      source: "right_handed_z_up",
      geometry:
        format === "glb"
          ? "right_handed_y_up_via_node_rotation"
          : "right_handed_z_up",
    },
    precision: {
      ir_scalars: "decimal_strings",
      native_scalar_storage: "IEEE754_binary64",
      model_tolerance: revision.ir.tolerance,
      requested_tessellation_mm:
        geometry[0]?.manifest.requested_deflection ?? null,
      certified_surface_error_bound_mm: null,
    },
    error_budget: {
      requested_model_tolerance: revision.ir.tolerance,
      parameter_to_kernel_certified_bound_mm: null,
      kernel_certified_bound_mm: null,
      tessellation_certified_bound_mm: null,
      combined_certified_bound_mm: null,
      measured_export_losses: "validation.json:roundtrips",
      interpretation:
        "A requested tolerance or sampled roundtrip error is not a certified combined surface bound.",
    },
    quality: geometry[0]?.manifest.quality ?? revision.quality,
    validation_digest: digest,
    omitted_from_geometry:
      format === "ir"
        ? []
        : [
            "feature_history",
            "constraints",
            "purpose",
            "parameter_sources",
            "assumptions",
          ],
    semantic_sidecar: {
      filename: "model.ir.json",
      preserves: [
        "feature_ids",
        "parameters",
        "purpose",
        "lineage",
        "constraints",
        "assumptions",
        "parameter_sources",
        "authoritative_representation",
      ],
      imported_source_assets_required: sources.length > 0,
    },
    source_assets: sources,
    components: components.map((a) => ({
      filename: a.manifest.filename,
      artifact_id: a.artifact_id,
      sha256: a.hash,
      byte_length: a.size,
      mime: a.mime,
      download: a.download,
    })),
    publication: "private_authenticated_artifacts_only",
  });
  return {
    artifacts: [...components, manifest],
    package_manifest: {
      artifact_id: manifest.artifact_id,
      uri: manifest.uri,
      download: manifest.download,
      hash: manifest.hash,
    },
  };
}
