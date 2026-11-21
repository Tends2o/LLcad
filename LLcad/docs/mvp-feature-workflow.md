# MVP first, then parallel feature refinement

The reconstruction agent first commits a recognizable coarse model: silhouette, proportions, main components, orientation and dominant colour regions. A full paid inventory is optional. Fine lettering and invisible engineering do not delay this first view.

## Fast visual inspection

Call `cad_reference_setup` with original reference artifacts, then `cad_reference_check` with `phase:"inspect"`. It needs no API key and no populated requirement inventory. An optional `views` array contains 1–3 `{id,label,camera,width?,height?}` entries. A single front view is the default. Poll `cad_reference_report`; inspect its real source/render image artifacts. There are no external vision calls or repeated native contact/dimension measurements. The report retains elapsed time, selected cameras, source hashes and frozen revision/material/renderer binding.

The viewer's **Quick view check** captures its current camera. **Final reference review** remains separate and may incur configured vision costs. Quick inspection never grants a final reference pass or export approval.

## Parallel refinement through MCP

`cad_feature_batch` requires a current completed inspection and a geometrically valid committed MVP. Its `mvp` object contains booleans `silhouette`, `proportions`, `dominant_colours`, `main_components`, `orientation` and a concrete `evidence` explanation. Weights are .25/.25/.20/.20/.10. Silhouette and proportions are mandatory; declared coarse-check coverage must reach .50. This is an orchestrator-assessed checklist coverage, **not measured 50% image accuracy**.

Supply `orchestrator_model` and 1–8 independent tasks. Each task contains:

- `id`, `title`, `description`, `kind` (geometry, edge, colour, marking, optics, curvature), and concrete `evidence`.
- Distinct `owned_feature_ids` to edit, plus optional `context_feature_ids` to read.
- `reference_ids` and `render_ids` from the inspection's source and viewer images.
- Optional `effort` (wins over everything else) and, for lanes only, `owned_part_ids`.

Overlapping owned features and ancestor/descendant edits across workers are rejected before dispatch. Each worker receives only its frozen dependency context and selected images.

### Worker model, effort and throughput

- **Model.** `worker.model` is kept exactly when given; any Claude or Codex model id works, `claude-fable-5-1` included, and `agy:<model>` runs through Antigravity (see [agy.md](agy.md)). Without it, Claude orchestrators get `claude-opus-5-5`, GPT/Codex orchestrators `gpt-6-astra` and Antigravity orchestrators (`agy:…`, Gemini) `agy:gemini-4-neon` (environment overrides `LLCAD_FEATURE_CLAUDE_MODEL`, `LLCAD_FEATURE_CODEX_MODEL`, `LLCAD_FEATURE_AGY_MODEL`). Unknown orchestrators fail closed. The model is never silently substituted; an agy family takes the task effort, clamped to the efforts the family offers.
- **Effort.** `task.effort` > `worker.effort` > a static table by kind: medium for geometry, edge and marking; high for optics and curvature; low for colour-only tasks (`worker.effort_by_kind` overrides it; xhigh and max are allowed). Jev is optional: when enabled it may pick the table level or a neighbour. If `jev_decide` fails, times out (`LLCAD_JEV_TIMEOUT_MS`, default 20 s) or is disabled (`worker.jev: false`, `LLCAD_FEATURE_JEV=off`), the table answers and the task goes on. `effort_source` (task, worker, jev or table) and the fallback reason are recorded.
- **Throughput.** `max_parallel` is 1–4 (default 2; the host caps it with `LLCAD_FEATURE_MAX_PARALLEL`). Each CLI worker needs about 0.4 GB, and they run inside the service's MemoryMax. `timeout_seconds` defaults to 600 (30–1800). A task may return up to 64 operations.

### Validated workers (`validation: "worker_candidate"`)

This is the default when the service knows its MCP URL (`MATHFORGE_PUBLIC_URL` + `/mcp`, or `LLCAD_WORKER_MCP_URL`; `off` disables it). `validation: "none"` keeps the old read-only proposal workers.

- **Access.** Each worker gets a short-lived designer grant on this one model: read and edit, never commit or export. It reaches it with a random bearer token that lives in memory only (for Claude in a 0600 `--mcp-config` file, for Codex in an environment variable, for agy in `LLCAD_MCP_TOKEN`, which the llcad stdio bridge then uses instead of the local key; never in argv).
- **Allowed tools.** The server enforces a tool allowlist: read tools, `cad_plan_edit`, `cad_apply_patch`, `cad_validate`, `cad_discard`, jobs, `cad_render`, and `cad_view`/`cad_gates` where the build has them. Every worker patch is checked against the task's ownership and the frozen base revision. `patch_ref` and `commit` are refused.
- **What the worker does.** It builds its own candidate from the frozen base with `cad_apply_patch {validate: true}`, iterates, measures, and returns the operations of its final candidate together with that candidate's `transaction_id`.
- **Server checks.** The server verifies that the transaction belongs to the worker, sits on the frozen base, and rebuilds exactly from the returned operations (IR hash). It then reports `validation.status` as passed, failed, not_validated or mismatch, with the validation digest.
- **Crops.** The server captures the candidate in the task's inspection views, one Chromium at a time, and stores before/after/pair crops of the changed region (`crops[]`; `LLCAD_FEATURE_CROPS=0` disables this).
- **Cleanup.** When the task ends, the grant is revoked and the worker's open candidates and jobs are aborted. After a restart, leftover worker grants are closed.

### Merge by reference

`cad_feature_batch_report` (optionally with `wait_ms` ≤ 110000) lists `waves`. These are patch documents of at most 64 operations, packed from the completed tasks in task order; each wave is one transaction:

```
cad_apply_patch {model_id, base_revision: <head>, idempotency_key: <new>,
                 patch_ref: {artifact_id: <waves[i].patch_artifact_id>},
                 validate: true, commit: "if_valid", wait_ms: 100000}
```

- **Order.** Apply the waves in order and look at the changed views after each commit.
- **Outputs.** In one wave, output edits of several lanes on the same existing part are combined into one `set_part_outputs`. Its expected hash is the part's outputs after the earlier waves.
- **Rebase check.** Before any batch artifact (wave or single task proposal) is applied to a newer head, the server checks that no feature the tasks read or changed was edited since the frozen base, that their new features do not exist yet, and that the part outputs are what the wave expects. Otherwise the call fails with `STALE_REVISION` and names the conflicts; `waves[].applicable` shows the same check on the current head.
- **Single tasks.** Per-task artifacts (`tasks[].proposal_artifact_id`) apply the same way when only some proposals are wanted.

`applied:false` stays explicit, and stale batches never complete as current.

### Lanes (opt-in)

Lanes are parallel LLM builders after a committed skeleton. Use them only when the user asked for parallel work, because the default is one agent.

1. The planner commits the skeleton: frames, envelope, one part slot per region with its colour, and the ledger.
2. It starts one batch with `lanes: true` and 2–3 tasks, one per lane (for example `pa` for head and optics, `pb` for grip and controls, `pc` for markings). Each lane has its `owned_part_ids`, its context features and an id namespace. Lanes may add features `<id>-…`, parts `pt-<id>-…` and frames `fr-<id>-…`, and may change the outputs of their own parts (own features only).
3. Merge by reference (waves above).
4. Run the gates (`cad_gates` or the skill's gate hook) and the multi-view look.

For in-process subagent lanes (the llcad-modelling skill), every lane writes its patch file against the frozen head. The helper then merges the lane files into wave files:

```
node $LLCAD_ROOT/dist/packages/feature-work/lanes-cli.js merge --out .llcad/waves --run .llcad/run.json [--base-ir head-ir.json] .llcad/lanes/*.json
```

It refuses when `run.json lanes` is empty (opt-in) and prints one `patch_ref {path}` call per wave. Its checks are the lane id prefixes and that no feature, part or frame is touched by two lanes.

Inspect the changed views again and continue with the next meaningful feature group. Reserve full `phase:"compare"` for final review. The existing final export guard remains unchanged.

## Evidence and limits

Native tests prove valid geometry, not visual resemblance. Contact distance proves declared contact, not exposed button faces. Use detailed viewer captures and surface visibility samples for occlusion defects; inspect inhomogeneous colour transitions against source close-ups, rather than relying on a single material label. Before/after visual evidence is the progress measure, not feature count.

The effort levels follow the documented effort sets of the providers ([GPT-6 Astra](https://developers.openai.com/api/docs/models/gpt-6-astra), [Claude effort](https://platform.claude.com/docs/en/build-with-claude/effort)). Availability still depends on the local account and quota. The runtime records provider failures rather than claiming an unavailable model ran successfully. Validated workers were smoke-tested with claude-opus-5-5 at low effort; the Codex MCP wiring (`mcp_servers.llcad.*`) has not been run live.
