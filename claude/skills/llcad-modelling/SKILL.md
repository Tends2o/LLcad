---
name: llcad-modelling
description: >-
  Build, modify or reconstruct CAD models with the LLcad MCP server (mcp__llcad tools), above all
  one-shot reconstruction of a real product (handheld devices, fixtures, housings and similar
  parts) from photos, turntable video, datasheets and SVG logos. Use whenever the task is to
  model, rebuild, correct or extend an LLcad model, write LLcad patches, run a reference reconstruction
  or check a model against references. Gives the phase plan, the Python DSL client that writes patches,
  the operator semantics, known pitfalls, and hooks that wait for jobs, lint patches, inject gate
  results and hold the run contract.
allowed-tools:
  - mcp__llcad
  - Bash(python3 ${CLAUDE_SKILL_DIR}/scripts/*)
  - Bash(python3 .llcad/gen/*)
hooks:
  PreToolUse:
    - matcher: "mcp__llcad__cad_apply_patch|mcp__llcad__cad_plan_edit"
      hooks:
        - type: command
          command: 'python3 "${CLAUDE_PLUGIN_ROOT:-/root/.claude/skills/llcad-modelling}/scripts/lint_hook.py"'
          timeout: 15
  PostToolUse:
    - matcher: "mcp__llcad__cad_(apply_patch|validate|measure|render|export|rebuild|import|revert|solve_constraints|job_get)"
      hooks:
        - type: command
          command: 'python3 "${CLAUDE_PLUGIN_ROOT:-/root/.claude/skills/llcad-modelling}/scripts/wait_hook.py"'
          timeout: 130
    - matcher: "mcp__llcad__cad_(commit|apply_patch|discard|job_get)|Bash"
      hooks:
        - type: command
          command: 'python3 "${CLAUDE_PLUGIN_ROOT:-/root/.claude/skills/llcad-modelling}/scripts/gates_hook.py"'
          timeout: 200
    - matcher: "mcp__llcad__.*|Bash"
      hooks:
        - type: command
          command: 'python3 "${CLAUDE_PLUGIN_ROOT:-/root/.claude/skills/llcad-modelling}/scripts/time_signal.py"'
          timeout: 5
  UserPromptSubmit:
    - hooks:
        - type: command
          command: 'python3 "${CLAUDE_PLUGIN_ROOT:-/root/.claude/skills/llcad-modelling}/scripts/time_signal.py"'
          timeout: 5
  Stop:
    - hooks:
        - type: command
          command: 'python3 "${CLAUDE_PLUGIN_ROOT:-/root/.claude/skills/llcad-modelling}/scripts/stop_gate.py"'
          timeout: 200
  SessionStart:
    - matcher: "compact|resume"
      hooks:
        - type: command
          command: 'python3 "${CLAUDE_PLUGIN_ROOT:-/root/.claude/skills/llcad-modelling}/scripts/sessionstart_ledger.py"'
          timeout: 10
---

# LLcad modelling

Wall time in LLcad runs is model output, not kernel time (96 % generation in the measured 2 h builds;
patch JSON 37 %, polling 16 %, source reading 18 %). This skill removes those turns: compute in
Python, submit few large patches by reference, let hooks wait and gate, look at the result, stop on a
deterministic floor. Skill dir: `${CLAUDE_SKILL_DIR}` (= `~/.claude/skills/llcad-modelling`).

| file | use |
|---|---|
| `reference/operators.md` | every operator: frame, anchor, parameters, one checked example (generated from the registry) |
| `reference/pitfalls.md` | error codes and the fix for each; read when a call fails |
| `reference/recipes.md` | grip loft, tilted head, knurl, ring text, logo, keys, LED window, lens, shell+seam, colours |
| `scripts/llcad.py` | DSL client + CLI (`caps`, `head`, `create`, `lint`, `submit`, `schema`, `structure`, `gates`, `job`, `call`) |
| `scripts/ledger.py` | run contract + feature ledger in `.llcad/` |
| `scripts/crop.py` | reference / render / diff crop triples with part labels for the judge |
| `templates/` | `run.json`, `scope.md`, `assumptions.md`, `run-settings.json` (hooks for `claude -p --settings`) |
| `briefing.md` | this skill condensed, for `--append-system-prompt-file` |

Never read LLcad source (workers/*.py, compiler) to learn semantics: `operators.md` and
`llcad.py schema <operator|patch-op|feature>` answer it from the deployed build.

## 0 Setup (one Bash call)

```bash
S=~/.claude/skills/llcad-modelling/scripts
python3 $S/llcad.py caps                       # reachable? new operators? patch_ref / wait_ms / cad_gates?
python3 $S/llcad.py create "Hand lamp T1 - exterior" --profile precision_cad   # or use the given model
python3 $S/ledger.py init --model <id> --budget 60 --product "Hand lamp T1" --in "exterior,controls,markings" --out "variants,accessories"
```
`.llcad/run.json` is the run contract (scope, budget, acceptance, assumptions file, max_stop_blocks);
the Stop hook enforces it. Fill `.llcad/scope.md` in 5 minutes. Generators go to `.llcad/gen/*.py`,
patches to `.llcad/patches/` (never `cat` them).

## 1 Phases (time box for 60 min; expected ~50)

| clock | phase | output | gate |
|---|---|---|---|
| 0–3 | P0 Reference Pack: sources (hash, date), dimensions with tolerance and class [A]/[B]/[C], registered views, CSV contours, part inventory with colours, markings from original vectors, ≥6 landmarks/view (≥2 asymmetric) | `refs/`, pack JSON | datasheet dims vs silhouette scale ±1 % |
| 3–10 | P1 Plan: parts, frames, named dimensions, interfaces, colours, keep-outs, id prefixes; ledger items = acceptance tests of every visible detail | `.llcad/plan.md`, ledger | plan complete, no free-floating parts |
| 10–19 | P2 MVP from ONE generator: frames, envelope, all part slots with colours, silhouette and proportions | 1–3 patches, commit | G1: gates floor, envelope ±2 %, **4-view look** |
| 19–32 | P3 Details by ledger priority: controls, windows, optics, markings (text/SVG operators) | few large patches | per detail: visible, ΔE, position mm |
| 32–37 | P4 Integrate, commit, full gate pack | commit | G2: gates + contacts + coincident faces + name vs content |
| 35–40 | P5 Judge: fresh-context subagent on crop triples, semantic items only | defect list | advisory; a finding needs a metric |
| 40–48 | P6 ≤2 fix rounds, largest residual first | commit | G3 on failed items |
| 48–50 | P7 Report + assumptions list; export only at the very end | report | Stop hook: contract met or budget exit |

Record the phase: `ledger.py phase "P3 details"` (re-injected after compaction).
Effort: plan and judge at xhigh; mechanical patch writing needs less. Never think more than ~20k
tokens in one turn: write the plan into `.llcad/plan.md` and compute in Python instead.

## 2 Turn economy (the rules that halve the run)

- **Compute in Python, never in your head**: coordinates, trigonometry, section tables, glyph
  placement, NURBS poles. "Exclusively LLcad" means building in LLcad, not computing without tools.
- **Few large patches**: one generator per phase, 30–64 ops per patch; the DSL splits at 64.
- **Submit by reference**: the generator prints `cad_apply_patch {..."patch_ref":{"path":...}}` (MCP-native,
  default) — pass exactly that. Without patch_ref on the server use `--http` (same service,
  apply→wait→validate→commit in one Bash call). Never paste or re-type patch JSON.
- **No polling**: `wait_ms` / the wait hook return terminal results. If a result still says
  `running`, call `cad_job_get` once — never loops.
- **Batch independent calls in one message** (render views, measures, exports). Worker pool = 2.
- **Brief reads**: `llcad.py head <model>` (≤300 B), `detail:"brief"` where tools have it.
- **Do not read or print** tools/list, big IR, patch files or source code.
- Export (STEP/STL/GLB) only once, from the final revision, all formats in one turn.

## 3 DSL (scripts/llcad.py)

```python
import os, sys; sys.path.insert(0, os.path.expanduser("~/.claude/skills/llcad-modelling/scripts"))
from llcad import Build, Frame, rrect_pts, slot_pts, ellipse_pts, read_csv
b = Build("p2-mvp", part="pt-body")                        # name -> patch files p2-mvp-01.json ...
head = b.tilted_head_frame("fr-head", pivot=(0, 0, 150), tilt_deg=-14)   # structure frame, 0 features
core = b.box("p01-core", 30, 20, 100, at=(-15, -10, 0))    # box: MIN corner
g = b.grip_loft_csv("p01-grip", "refs/grip.csv", z="z", w="w", d="d", r_col="r")   # equal counts
k = b.control_key("p05-key", 8, 4, 1, proud=0.3, at=Frame.plane("front", (0, -10, 120)), part="pt-key", housing_part="pt-body")
body = b.cut("p01-body", g, k["recess"])
t = b.text("p07-label", "LLcad", 2.8, 0.1, at=Frame.plane("back", (0, 10.2, 40)), part="pt-print")
b.part("pt-body", "Grip", colour="#dddddb", material="Plastic satin", outputs=[body])
b.part("pt-key", "Key", colour="#141416", material="Plastic matte", outputs=[k["cap"]])
b.part("pt-print", "Lettering", colour="#4d4c49", material="Print matte", outputs=[t])
b.finish()     # python3 .llcad/gen/p2.py --model <id> [--http] [--gates] [--lint] [--offline]
```
- Every operator: `b.feat(op, id, deps, **fields)` — fields routed by the deployed schema (parameters
  become quantities, numbers become decimal strings). New operators (text, svg_emblem, sketch, draft,
  split_by_plane, macro) are used natively when `llcad.py caps` lists them, else lowered (fontTools
  outlines, SVG parser, polygons, affine placement) with a note in the summary.
- Helpers: box cbox cyl cone sphere torus · profile strip circle nurbs_curve rounded_rect slot ellipse ·
  extrude(direction) revolve revolve_rz loft sweep · union cut common group · hole pocket groove ·
  fillet chamfer shell · move instance rotate affine place mirror(normal) pattern cpattern ·
  text svg_emblem · ring knurl_ring lens(seat) indicator_row control_key usb_c led_window grip_loft_csv ·
  frame tilted_head_frame assembly part output · set_parameter set_variable set_model_settings
  prune_unreachable instantiate_fragment set_configurations set_decals raw.
- The client lints before anything is sent (CCW, loft counts, curve outputs, id clashes, owners,
  frames, 64 ops) and prints a ≤1 KB JSON summary. Structure hashes are chained locally.
- Feature ids `<prefix>-<name>` (p01-…, one prefix per part/lane); part ids `pt-…`; frame ids `fr-…`.
  Structure ids must never equal feature ids.

## 4 Conventions and pitfalls (details: reference/pitfalls.md)

- box = min corner; cylinder/cone = base centre, +Z; sphere/torus = centre; hole/pocket/groove cut
  DOWN from z and must remove material; transform rotates about Z through the ORIGIN.
- profile: planar, CCW seen from +Z for extrude (clockwise = negative volume); revolve takes the
  profile in the XZ half-plane about local Z; extrude is +Z of the local frame only (place the solid).
- loft sections need equal segment counts; union/difference ≤16 inputs (tree them); mirror = x→−x.
- Outputs are solids only; every model output is in some part's outputs; features belong to a part.
- set_structure needs the current structure hash; ≤64 ops per patch; a fresh idempotency_key per
  attempt (hooks renew keys after failures).
- Break edges (fillet/chamfer) on single bodies before booleans; avoid near-tangent cuts (≥0.5 mm off
  or exactly on); tessellation 0.12–0.18 mm for exports.
- Prints and markings: relief ≥0.05 mm (0.1 preferred) or inset, never 0.01 mm coplanar layers
  (z-fighting); logos from the ORIGINAL SVG vectors, device colour measured on product images.
- Colours: part names `Material #RRGGBB · Name` (the viewer parses the hex).
- Variants/states/accessories only when asked, and then as configurations — never displaced copies.
- Model name says what the model contains; no "complete" while P0 items are open.

## 5 Frames instead of transform chains

A tilted head, a side panel or a ring face is a structure frame (`b.frame` / `tilted_head_frame`),
and its features use `frame="fr-head"`: zero extra features, inputs from other frames are
transformed automatically. Python `Frame` objects place single solids (`at=Frame(...)`,
`Frame.plane("front"|"back"|"side"|"left"|"top"|"bottom", origin)`, `.moved()`, `.rotated()`,
`.then()`); sketches stay local and the extruded SOLID is placed.

## 6 Mandatory look (after every commit of P2, P4, P6)

Read four views of the whole model (front, side, back, iso) — `cad_view` when available (images
+ per-part visible px, ΔE), else `cad_reference_check phase inspect` / viewer captures. Check:
nothing outside the device, no parts side by side that belong inside, keys and windows visible,
logo position/orientation/colour, no z-fighting, name matches content. Keep images out of the
orchestrator when possible (≤20 images per request): the judge subagent reads the crops.

## 7 Gates and judge: floor, not finish

- After each commit the gates hook injects the ranked residual table (cad_gates; fallback model
  quality). `floor_passed` is a floor: then fix the largest residual or justify it as source-limited.
  Stop when the floor passed and two fidelity passes are done, or the residual improves <2 %, or the
  time box says so. Never report "done" from a gate alone.
- Judge = fresh-context subagent (Agent tool) with `crop.py triple`/`sheet` images: registered crop
  triples, set-of-mark part labels, 2–5 binary questions per ledger item, "name the largest
  remaining difference and where". Its findings are candidates; confirm each with a measurement.
- Ledger: `ledger.py add|set` items with evidence bound to a revision (`--evidence`, `--revision`).

## 8 Stop and report

The Stop hook blocks while any of these hold: an uncommitted candidate, gate floor not passed on
the head, assumptions list missing/template, model name vs content mismatch, out-of-scope parts
(and open P0 items when `acceptance.ledger` is `required`). It gives up after `max_stop_blocks`
(default 6) or when the time budget is exhausted and writes `.llcad/stop-report.md`; a deliberate
abort is `.llcad/giveup.md`. Final report: model id + head revision, what matches (with numbers),
residuals, assumptions ([C] items), out-of-scope items, exports. Short; no self-praise.

## 9 Lanes (opt-in only)

Only when the user asks for parallel work or the plan has ≥3 independent regions after a committed
skeleton (frames, envelope, one part slot per region, ledger): 2–3 subagents, each with its own id
prefix (`run.json lanes: {"<agent_type>": "pB-"}`), building a patch FILE against the frozen head
(never committing). Merge by reference: `node $LLCAD_ROOT/dist/packages/feature-work/lanes-cli.js
merge --out .llcad/waves --run .llcad/run.json .llcad/lanes/*.json` (refuses without run.json lanes,
checks prefixes and cross-lane edits, ≤64 ops per wave), then apply each wave with
`cad_apply_patch {patch_ref: {path}, validate: true, commit: "if_valid"}` in order, then gates and the
4-view look. Server-side alternative: `cad_feature_batch {lanes: true, tasks: one per lane with
owned_part_ids}`: validated workers (own candidate, never commit) return patches plus crops; apply
the report's `waves[].patch_artifact_id` by `patch_ref {artifact_id}`. The lint hook denies ids
outside a lane's prefix. Kernel slots stay 2; LLM lanes are the gain. Default is one agent.

## 10 Hooks (wired in this frontmatter and in templates/run-settings.json)

| hook | event | effect |
|---|---|---|
| `lint_hook.py` | PreToolUse apply/plan | deny with reason (64 ops, CW profile, loft counts, curve outputs, id clash, prefix); add/renew idempotency key; numbers→strings |
| `wait_hook.py` | PostToolUse async tools | waits ≤100 s, replaces queued output with the compact terminal result (+ optional auto-validate) |
| `gates_hook.py` | PostToolUse commit / DSL Bash | runs gates on the new head, ≤2 KB residual table as context |
| `time_signal.py` | PostToolUse, UserPromptSubmit | "elapsed X / budget Y" at 5-min steps and 50/75/90/100 % |
| `stop_gate.py` | Stop | run contract gate (see 8) |
| `sessionstart_ledger.py` | SessionStart compact/resume | re-injects run state + ledger |

Environment: `LLCAD_URL` (default http://127.0.0.1:4310), `LLCAD_TOKEN_FILE` (default
/root/projects/LLcad/data/local-token; read by scripts, never printed), `LLCAD_ROOT` (deployed tree
for schemas), `LLCAD_HOOK_ALLOW=1` (let the lint hook auto-approve its rewrite when mcp__llcad is
allowed), `LLCAD_HOOK_WAIT_S`, `LLCAD_LINT_CW=warn`, `LLCAD_GATES_HOOK=0`.
Viewer-launched agents: pass this skill dir (it is loaded from `~/.claude/skills/llcad-modelling`),
`--append-system-prompt-file ~/.claude/skills/llcad-modelling/briefing.md` and
`--settings ~/.claude/skills/llcad-modelling/templates/run-settings.json` (use the settings file
OR invoke this skill, not both: otherwise every hook runs twice).
Regenerate `reference/operators.md` after operator changes:
`cd /root/projects/LLcad && npx tsx scripts/gen-operator-docs.ts --out ~/.claude/skills/llcad-modelling/reference/operators.md`.
