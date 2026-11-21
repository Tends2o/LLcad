# Antigravity (agy) as an agent runtime

LLcad runs its AI stages with Claude Code by default. A model id `agy:<model>` runs the
same stage through the Antigravity CLI (`agy`) instead: viewer stages (plan/intake,
proposal, change, build, drawing) and `cad_feature_batch` workers.

## Machine setup (outside this repository)

| Piece | Where | What |
| --- | --- | --- |
| MCP server | `~/.gemini/config/mcp_config.json` | `llcad` = stdio bridge (`agy mcp add llcad <node> <LLcad/bin/llcad-bridge.mjs>`) |
| Plugin `llcad` | `~/.gemini/config/plugins/llcad/` | skill `llcad-modelling` (agy variant), `rules/AGENTS.md`, `hooks.json` |
| Hook adapter | `…/skills/llcad-modelling/scripts/agy_hook.py` | runs lint, job wait, gates, time signal and stop gate for agy |
| Grant | `~/.gemini/antigravity-cli/settings.json` | `permissions.allow` contains `mcp(llcad/*)` |

`scripts/doctor.py` of the agy skill checks all of it (`ok: true`).

Plugin MCP servers load only for a signed-in Antigravity account; the global
`mcp_config.json` always loads. That is why `llcad` is registered globally.

## Models and effort

`GET /api/agent/models` lists this machine's agy model families (`agy models`, cached ten
minutes, a built-in fallback list when the CLI cannot be asked). The viewer offers them as
`agy:<family>` in *AI model* and in every *Model · <stage>* select ("same as AI model" =
empty). A family takes the stage effort; agy refuses an effort a family lacks, so the
service picks the nearest effort below, else the lowest (`resolveAgyModel`): e.g.
`agy:gemini-4-argon` + `xhigh` → `gemini-4-argon-high`. An exact variant
(`agy:gemini-4-neon-low`) runs as it is.

## How a run is limited

agy has no `--mcp-config`, `--allowedTools` or system-prompt flag. Each run therefore
gets, in its own folder (`packages/model-service/agy.ts`):

- `.llcad/agy-settings.json` (`--settings`): agy permission grants for exactly what the
  stage may use (`mcp(llcad/<tool>)`, `command(*)` for writing stages, `read_url(*)` where
  the Claude stage has web access). Print mode refuses everything not granted.
- `.agents/hooks.json` + `.llcad/agy-policy.json`: `agy_hook.py --scope run --policy …`
  denies other MCP servers or tools, writes outside the run folder, commands, web,
  browser and subagents as the stage requires; writing stages with the skill also get
  the LLcad lifecycle hooks (`--lifecycle`). The plugin's global hooks step aside in a
  folder with such run hooks.
- The stage prompt (written for Claude Code) gets a preamble that maps tool names
  (Read → view_file, Bash → run_command, …), the `call_mcp_tool` shape and, for writing
  stages, the skill briefing. Prompts over 24 kB go through a file.

| Stage | llcad tools | Files | Commands | Web |
| --- | --- | --- | --- | --- |
| purpose (plan) | none | none | read-only guard (`intake_guard.py`) | yes |
| proposal | read/measure tools | none | none | yes |
| execution, direct, build | all | run folder | all (`acceptEdits`: none; `plan`: read-only) | yes |
| drawing | drawing + reading tools | run folder | none | no |
| feature worker | the worker grant's tools | none | none | no |

Feature workers reach llcad with their own short-lived token: the service sets
`LLCAD_MCP_TOKEN` for the agy process, and the stdio bridge sends it instead of the
local key (and bypasses its tool-list cache).

## agy hook contract, as bridged

- MCP calls are `call_mcp_tool {ServerName, ToolName, Arguments}`; commands `run_command`.
- PostToolUse gets no tool result and may not answer, and agy writes the result into the
  transcript only after it. The adapter records the step; the next PreInvocation reads
  the result from the transcript, waits for jobs, runs the gates and injects the text as
  a system message.
- Lint rewrites use `overwrite: {Arguments}`; a blocking stop gate answers
  `{"decision": "continue"}`. Hook output is pure ASCII JSON.
- Trace: `%TEMP%/llcad-agy-hooks/trace.log` (one line per handled event).

## Results

`stream-json` events (`init`, `step_update`, `result`) feed the run progress (tokens, tool
calls, last answer). The `result` carries `conversation_id` (resumed with
`--conversation` when an accepted proposal is carried out on the same runtime) and, for
the plan stage, `structured_output` from `--json-schema`. agy reports no prices; cost
stays 0. On Windows a cancelled or timed-out agy run is stopped with its process tree.

## Print-mode pitfalls (observed with agy 1.5.13)

- A permission agy would have to ask for is not a refusal the agent can work around:
  print mode "soft-denies" it and **ends the whole run** with an empty answer
  (`denied_actions` in the result, "headless mode cannot prompt" on stderr). Hence the
  grants cover everything the run's hook allows, and the hook refuses the rest with a
  reason the agent reads and continues from. `command(ls)` does not cover `ls -la`.
- agy's tool-safety monitor may ask before a file write even inside the workspace
  (seen once for a new `.py` in a new subfolder). Stages that write their folder run with
  `--mode accept-edits` and a `write_file(<run folder>)` grant.
- The monitor decisions are in `~/.gemini/antigravity-cli/log/cli-*.log`
  (`agy_tool_safety`, `Print mode: soft-denying tool confirmation`).
