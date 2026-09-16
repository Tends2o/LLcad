# Working on LLcad

Guidance for people and coding agents that change this repository.

## Using the server

- Model entirely through the MCP tools. Discover existing models with `cad_list_models`, narrow
  features with `cad_find` and `cad_inspect`, and resolve real ambiguity with further tool
  queries or one short question. Never ask the person to click in the viewer or look up IDs.
- Every change follows plan → candidate → validate → commit. Use only measured values and the
  server-generated validation digest. Do not weaken tolerances or protected constraints quietly.
- Native face handles are bound to a revision. Rebind only with an explicit target revision;
  never substitute a geometrically similar face after a split or merge error.
- When `cad_get_model` reports `build_compatibility.status = rebuild_required`, run `cad_rebuild`
  in plan mode with the current registry hash first, then compute an explicit candidate,
  validate it and commit it as a new revision. Old revisions stay untouched.
- The viewer is optional. `cad_viewer_open` and `cad_viewer_close` start and stop it on demand.

## Changing the code

- Run `npm run verify` after every source change (type check, integration tests, native
  geometry tests, MCP workflow, build, browser workflow). Changes to geometry, caching or
  resource behaviour also need `npm run benchmark`. Reports under `reports/` must belong to
  the current build hashes; `npm run release:check` enforces this.
- A change to the operator registry alters the registry hash. Rebuild and re-validate
  existing models afterwards (see `docs/api.md`, "Moving to a new build").
- Keep `npm run schemas` output committed: JSON schemas and the operator registry are derived
  from the Zod contracts in `packages/semantic-ir`.
- Only one process may open a data directory at a time; stop the service before running the
  demo, benchmarks or maintenance commands against the same directory.
- Never execute shell, Python or JavaScript code taken from model data. Workers run in
  Bubblewrap sandboxes without network access; do not add an unsandboxed fallback.
