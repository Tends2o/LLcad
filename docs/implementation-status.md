# Implementation status

This map prevents the working local version from being mistaken for a full acceptance of
every goal in the original blueprint (`Mathematik_First_3D_MCP_Bauplan.md`).

| Phase | Implemented and tested | Still open |
|---|---|---|
| 0 – Contracts and references | Strict IR, units, error codes, role and scope boundary, capability contracts, technical, organic and instance fixtures | Target account and real host/OAuth parameters |
| 1 – Mathematical core | Native OCCT solids, indexed STL meshes with exact CGAL checks and the `watertight_solid` profile, profiles, strips, constructions, CSG, safe stored scalar and vector expressions with unit checks, measurements with declared proof strength (Chamfer/Hausdorff samples, exact extrema, IoU, wall thickness, moving clearance), rigorous interval arithmetic with gradient-flow certificates, dual contouring, implicit curvature, exact Bézier oracles, regularisation, null-result and scaling regressions, isolated workers, revisions and exports | Statics, general numerical error propagation across all operators and a manufacturing certification |
| 2 – MCP and transactions | 25 tools with versioned result contracts, transactional output checks and evidence bound to candidate, engine and geometry, private model discovery, two HTTP protocol adapters, stdio entry point, on-demand viewer, build rebuild, a complete workflow verified through MCP, deduplication, jobs with phases, heartbeats and budgets, mandatory gates including on_failure, on_cancel and before_publish, bounded repair chains, digest and CAS, OAuth token checks, metrics, private resources | Real remote OAuth connection and a broad independent LLM test set |
| 3 – Understanding and details | Versioned project, assembly and part structure with one geometric authority per part, hierarchical rigid frames, local native construction and world-space output, local GPU and GLB mesh origins, semantic IDs, stored native face provenance for registered operators, revision-bound face selection with semantic anchors and rebinding, detail packages with sections, neighbouring faces and per-entity quality status, SVG section and projection views, sensitivities, conditioning check, protected and change regions, BVH search and dirty graph | General OCAF history and provenance for unregistered builders; split and merge cases are rejected on purpose |
| 4 – CAD and inverse construction | Explicit NURBS curves and multi-span surfaces, shape-preserving knot refinement, native angle, curvature and clearance measurement, affine transformations, instance patterns with single-occurrence variants, loft with compatibility check, sweep with rotation-minimising frames, twist, end scaling and self-contact check, offsets, hole, groove, pocket, fillet, chamfer, shell, ISO thread profiles with runout and fit comparison, analytic inverse volume solution, SLSQP with robust losses, rank diagnostics and KKT sensitivity, UV trimming, sewing; real round trips and private export packages with error budget | Global solvability and optimality proofs, free trimming loops over multi-face NURBS and thread tolerance classes |
| 5 – Organic details | Field AST with primitives and gyroid, value units, variable shells, compact supports, local inverse deformation, sparse extraction with Lipschitz or interval pruning, marching tetrahedra or dual contouring, certified surface deviation between revisions, blend-free mating regions, change and compute regions, implicit curvature, patch joins with rational bounds, OpenVDB export and import as a spline field with a certificate against the source, mesh repair, remeshing, ARAP and primitive hypotheses for authoritative meshes | Global sub-cell topology certification and second-order certificates |
| 6 – Performance | Bounded instances, compiled field AST, cache subtree hashes, content cache, incremental invalidation, parallel disposable sandboxes with a pre-warmed pool, adaptive per-face tessellation, spatial excerpts and pixel LOD, gzip transfer, paging, durable fair job distribution, benchmarks with cold and warm starts and export/validation profiles, scaling regression | Large production data sets, multi-core benchmark on target hardware, GPU profile, broad independent LLM evaluation |
| 7 – Hardening | Project roles and feature scopes, job budgets, bound one-time consent, grant replacement and revocation, internal publication with a before_publish gate and short-lived Ed25519 links, retention policy, isolation, permission and error tests, JWT tests, real restore with migration up to store version 6, real worker cancellation, exclusive operating lock, tenant erasure, package checks, SBOM, licence and distribution check, runbook, deployment templates, release manifest | Production endpoint and identity provider, confidential partial views, external publication, erasure of external backups, external host acceptance, operator approval of the CGAL distribution question and possibly stronger parser VM isolation |

## Requirement audit

`docs/plan-audit.json` binds all 162 sections to the unchanged original text with concrete
criteria and a classification. The audit keeps missing or weak evidence open;
`npm run plan:check` and `npm run release:check` must not report full acceptance while
requirements are open. Partial mathematical proofs are not an approval of the whole
application. Newly proven building blocks are summarised in
[completion-work.md](completion-work.md); their contracts are in
[intervals-and-certificates.md](intervals-and-certificates.md), [measures.md](measures.md),
[imports.md](imports.md), [threads.md](threads.md), [mesh-operators.md](mesh-operators.md),
[diagnostic-views.md](diagnostic-views.md),
[publication-and-retention.md](publication-and-retention.md) and [viewer.md](viewer.md).

Explicitly not provable locally and therefore open: real remote OAuth acceptance with a
target host, production approval, external publication, a 16-core benchmark machine and a
broad independent LLM evaluation.

## Evidence kept in the repository

- `reports/verification.json` documents the last executed gate run (type check, tests,
  native geometry, MCP workflow, build, Chromium) with the registry and implementation hashes
  it belongs to.
- `reports/benchmark.json` contains run times, machine data, build hash, sample counts and
  measurement limits.
- `reports/browser.json` comes from an executed Chromium workflow.
- `reports/mcp-workflow.json` proves the complete tool workflow with zero browser
  interactions.
- `reports/demo/report.json` comes from the executed 20 µm detail correction.
- `reports/npm-audit.json`, `python-audit.json`, `license-check.json`,
  `license-inventory.json` and the SBOM files document the checked dependencies.

Machine-specific acceptance runs (model rebuilds after build changes, migrations of a live
store, screenshots and logs) are produced by the gate scripts but not versioned; every
rebuild keeps all old revisions readable.

## Definition of done

The agent drives the tools; the local CAD service computes their mathematical constructions
deterministically, keeps immutable revisions and enforces its registered check gates. **The
complete definition of done from section 28 of the blueprint is not reached yet.** Local
transports and the complete workflow are verified; that does not replace a broad general LLM
evaluation or a remote OAuth acceptance, and the extensions listed above remain open.
`release:check` therefore must not report production readiness.

No public deployment, no external model shipment and no machine connection has been carried
out. Project sharing and its limits are described in [project-access.md](project-access.md);
grants are tested only with synthetic actors. Mesh validation with `watertight_solid`, its
limits and real export round trips are described in [mesh-quality.md](mesh-quality.md).
