# Documentation

## Guides (English)

| Document | Content |
|---|---|
| [Getting started](getting-started.md) | Requirements, installation, running the server, data directory, updating |
| [Connecting MCP clients](mcp-clients.md) | stdio and HTTP configuration for OpenCode, Hermes and any `mcpServers` client |
| [Tools and protocol](api.md) | The 25 tools, the change workflow, build upgrades, face selection, resources, HTTP details |
| [Viewer](viewer.md) | Scope of the browser viewer and the `cad_viewer_open` / `cad_viewer_close` tools |
| [Architecture](architecture.md) | Modules, atomic transactions, sandboxed workers, storage |
| [Operations](operating-runbook.md) | Deployment templates, backup and restore, failure handling, retention |
| [Compatibility](compatibility-matrix.md) | Protocol versions, transports and what has and has not been verified |
| [Implementation status](implementation-status.md) | What is built and tested, what remains open before a production release |
| [Strips](strips.md) | The `strip` operator: thick polylines with pads as one solid (traces, ribs, channels) |
| [Licensing](licensing.md) | The project licence (GPL-3.0-or-later) and the licences of the geometry kernel and other dependencies |

## Engineering notes (German)

Detailed contracts and proofs of individual subsystems. They are written in German and
reference the original blueprint `Mathematik_First_3D_MCP_Bauplan.md`.

| Document | Content |
|---|---|
| [mathematical-contracts.md](mathematical-contracts.md) | Numerical contracts of operators and measurements |
| [math-foundations.md](math-foundations.md) | Decimal quantities, units, expressions |
| [intervals-and-certificates.md](intervals-and-certificates.md) | Interval arithmetic and surface deviation certificates for implicit fields |
| [measures.md](measures.md) | Measurement kinds and their proof strength |
| [primitive-distance-contracts.md](primitive-distance-contracts.md) | Distance contracts of the field primitives |
| [compiled-fields.md](compiled-fields.md), [transforms-and-fields.md](transforms-and-fields.md), [vector-expressions.md](vector-expressions.md) | Implicit field graphs, transformations and typed vector expressions |
| [nurbs.md](nurbs.md), [threads.md](threads.md), [patterns.md](patterns.md) | Rational curves and surfaces, ISO thread profiles, linear and circular patterns |
| [structure-and-frames.md](structure-and-frames.md), [face-provenance.md](face-provenance.md) | Projects, assemblies, parts, local frames and native face provenance |
| [imports.md](imports.md), [export-package.md](export-package.md), [mesh-operators.md](mesh-operators.md), [mesh-quality.md](mesh-quality.md) | STEP, STL and OpenVDB import, export packages, mesh operators and mesh validation |
| [diagnostic-views.md](diagnostic-views.md), [preview-coordinates.md](preview-coordinates.md) | SVG section and projection views, preview coordinate precision |
| [result-contracts.md](result-contracts.md) | Versioned tool result contracts and validation evidence |
| [project-access.md](project-access.md), [publication-and-retention.md](publication-and-retention.md), [threat-model.md](threat-model.md), [storage-durability.md](storage-durability.md) | Sharing, publication, retention, threat model and storage guarantees |
| [completion-work.md](completion-work.md), [plan-audit.json](plan-audit.json) | Requirement audit against the blueprint |
