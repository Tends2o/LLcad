import { execFile } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import { venvPython } from "../semantic-ir/host.js";

/** Antigravity (agy) as a second agent runtime next to Claude Code. A model id
 *  "agy:<model>" runs through the agy CLI; <model> is an agy model family
 *  ("gemini-4-neon") or one exact variant ("gemini-4-neon-high"). A family takes
 *  the stage's effort, clamped to what the family offers (agy refuses an effort a
 *  family does not have). The run reaches LLcad through the llcad MCP server that
 *  the agy setup registers (agy mcp add llcad …), like an interactive agy session. */
export const AGY_PREFIX = "agy:";
const EFFORT_ORDER = ["low", "medium", "high", "xhigh", "max"];
const EFFORT_SUFFIX = /-(low|medium|high|xhigh|max)$/;

export const isAgyModel = (model: string | null | undefined) =>
  typeof model === "string" && model.startsWith(AGY_PREFIX);
export const agyModelId = (model: string) =>
  isAgyModel(model) ? model.slice(AGY_PREFIX.length) : model;

/** The CLI, resolved once per call: a service PATH rarely contains it. */
export function agyExecutable() {
  const candidates = [
    process.env.LLCAD_AGY_CLI,
    process.env.LOCALAPPDATA && join(process.env.LOCALAPPDATA, "agy/bin/agy.exe"),
    join(homedir(), "AppData/Local/agy/bin/agy.exe"),
    join(homedir(), ".local/bin/agy"),
    "/usr/local/bin/agy",
  ].filter(Boolean) as string[];
  return candidates.find((path) => existsSync(path)) ?? "agy";
}

export type AgyModel = { id: string; label: string };
export type AgyFamily = { id: string; label: string; efforts: string[] };

/** What `agy models` listed when this integration was set up; used only when the
 *  CLI cannot be asked (not installed, not signed in, offline). */
export const AGY_FALLBACK_MODELS: AgyModel[] = [
  ["gemini-4-neon", "Gemini 4 Neon", ["low", "medium", "high", "xhigh"]],
  ["gemini-4-argon", "Gemini 4 Argon", ["low", "medium", "high"]],
  ["gemini-4-hydrogen", "Gemini 4 Hydrogen", ["low", "medium", "high", "xhigh"]],
  ["gemini-3.8-flash", "Gemini 3.8 Flash", ["low", "medium", "high"]],
  ["claude-opus-5-5", "Claude Opus 5.5", ["low", "medium", "high"]],
  ["claude-sonnet-5-5", "Claude Sonnet 5.5", ["low", "medium", "high"]],
  ["gpt-oss-120b", "GPT-OSS 120B", ["medium"]],
].flatMap(([id, label, efforts]) =>
  (efforts as string[]).map((e) => ({
    id: `${id}-${e}`,
    label: `${label} (${e === "xhigh" ? "XHigh" : e[0].toUpperCase() + e.slice(1)})`,
  })),
);
AGY_FALLBACK_MODELS.push(
  { id: "gemini-pro-orchestrator-neon", label: "Orchestrator (Neon)" },
  { id: "gemini-pro-orchestrator-g1-pro", label: "Orchestrator (Argon)" },
);

/** `agy models` prints "<id>\t<label>" per line (after a progress line on stderr). */
export function parseAgyModels(text: string): AgyModel[] {
  const out: AgyModel[] = [];
  for (const line of String(text ?? "").split(/\r?\n/)) {
    const m = /^([a-z0-9][a-z0-9._-]{1,63})\t(.+)$/.exec(line.trim());
    if (m) out.push({ id: m[1], label: m[2].trim() });
  }
  return out;
}

/** Variants grouped by family: "gemini-4-neon-high" → family gemini-4-neon. */
export function agyFamilies(models: AgyModel[]): AgyFamily[] {
  const families = new Map<string, AgyFamily>();
  for (const m of models) {
    const effort = EFFORT_SUFFIX.exec(m.id)?.[1];
    const id = effort ? m.id.slice(0, -effort.length - 1) : m.id;
    const label = effort ? m.label.replace(/\s*\([^)]*\)\s*$/, "") : m.label;
    const family = families.get(id) ?? { id, label, efforts: [] };
    if (effort && !family.efforts.includes(effort)) family.efforts.push(effort);
    families.set(id, family);
  }
  for (const f of families.values())
    f.efforts.sort((a, b) => EFFORT_ORDER.indexOf(a) - EFFORT_ORDER.indexOf(b));
  return [...families.values()];
}

type Listed = { at: number; models: AgyModel[]; source: "cli" | "fallback"; error?: string };
let cached: Listed | null = null;
let pending: Promise<Listed> | null = null;
/** The models this machine's agy offers, asked at most every ten minutes and
 *  never twice at once; the service keeps answering while the CLI thinks. */
export function listAgyModels(maxAgeMs = 10 * 60_000): Promise<Listed> {
  if (cached && Date.now() - cached.at < maxAgeMs) return Promise.resolve(cached);
  pending ??= new Promise<Listed>((done) => {
    execFile(
      agyExecutable(),
      ["models"],
      { encoding: "utf8", timeout: 30_000, windowsHide: true, maxBuffer: 1 << 20 },
      (error, stdout) => {
        const models = parseAgyModels(stdout ?? "");
        cached = models.length
          ? { at: Date.now(), models, source: "cli" }
          : {
              at: Date.now(),
              models: AGY_FALLBACK_MODELS,
              source: "fallback",
              error: error?.message ?? "agy models listed nothing",
            };
        pending = null;
        done(cached);
      },
    );
  });
  return pending;
}

/** Families without asking the CLI: the last answer of any age, else the
 *  fallback list. Starting a run never waits for `agy models`. */
export function knownAgyFamilies() {
  return agyFamilies(cached?.models ?? AGY_FALLBACK_MODELS);
}

/** The agy skill with its briefing and hook adapter (the llcad plugin). */
export function agySkillInstalled(dir = agySkillDirectory()) {
  return existsSync(join(dir, "briefing.md")) && !!agyHookScript(dir);
}

/** The exact agy model for a stage: a variant stays as it is; a family takes the
 *  stage effort, or the nearest effort below it that the family has (the lowest
 *  one when it has nothing below). Families without efforts run as they are. */
export function resolveAgyModel(
  model: string,
  effort: string,
  families: AgyFamily[] = knownAgyFamilies(),
): { model: string; effort: string | null } {
  const id = agyModelId(model);
  const family = families.find((f) => f.id === id);
  if (!family) {
    const suffix = EFFORT_SUFFIX.exec(id)?.[1] ?? null;
    return { model: id, effort: suffix };
  }
  if (!family.efforts.length) return { model: id, effort: null };
  const want = Math.max(EFFORT_ORDER.indexOf(effort), 0);
  const below = family.efforts.filter((e) => EFFORT_ORDER.indexOf(e) <= want);
  const chosen = below.length ? below[below.length - 1] : family.efforts[0];
  return { model: `${id}-${chosen}`, effort: chosen };
}

/** The choices the viewer offers: one entry per family ("agy:<family>"). */
export async function agyChoices() {
  const listed = await listAgyModels();
  return {
    available: listed.source === "cli",
    source: listed.source,
    ...(listed.error ? { error: listed.error.slice(0, 200) } : {}),
    models: agyFamilies(listed.models).map((f) => ({
      id: AGY_PREFIX + f.id,
      label: f.label,
      efforts: f.efforts,
    })),
  };
}

/* --- Per-run workspace ------------------------------------------------------
 * agy has no --mcp-config or --allowedTools. A run gets the same limits another
 * way: a settings file with agy permission grants (print mode refuses whatever is
 * not granted), and a hook in the run folder (.agents/hooks.json) that applies the
 * run's tool policy and, for runs that change the model, the LLcad lifecycle
 * scripts (lint, job wait, gates, time signal, stop gate). */

/** llcad tools that read or measure, never change a model. */
export const AGY_READ_TOOLS = [
  "cad_capabilities",
  "cad_context",
  "cad_compare",
  "cad_find",
  "cad_gates",
  "cad_get_model",
  "cad_inspect",
  "cad_job_get",
  "cad_list_models",
  "cad_measure",
  "cad_plan_edit",
  "cad_reference_report",
  "cad_render",
  "cad_structure",
  "cad_view",
];
export type AgyPolicy = {
  mcp: Record<string, "*" | string[]>;
  write: "none" | "cwd";
  command: "none" | "guard" | "any";
  command_guard?: string;
  web: boolean;
  deny_tools?: string[];
};
export type AgyStage =
  | "proposal"
  | "execution"
  | "direct"
  | "purpose"
  | "build"
  | "drawing";

/** What a viewer stage may do under agy — the same split as the Claude tool lists. */
export function agyStagePolicy(stage: AgyStage, drawingTools: string[]): AgyPolicy {
  if (stage === "purpose")
    return {
      mcp: {},
      write: "none",
      command: "guard",
      command_guard: ".llcad/intake_guard.py",
      web: true,
    };
  if (stage === "proposal")
    return { mcp: { llcad: AGY_READ_TOOLS }, write: "none", command: "none", web: true };
  if (stage === "drawing")
    return { mcp: { llcad: drawingTools }, write: "cwd", command: "none", web: false };
  return { mcp: { llcad: "*" }, write: "cwd", command: "any", web: true };
}

/** agy permission grants for a policy. They must cover everything the run's hook
 *  allows: in print mode a missing grant is not a refusal the agent can work
 *  around but the end of the whole run ("headless mode cannot prompt"). The hook
 *  runs first and refuses with a reason the agent reads; the grants are the
 *  backstop behind it (MCP tools one by one, so a hook that is missing cannot
 *  open them). Commands for the intake's read-only guard cannot be expressed as
 *  prefixes (`command(ls)` does not cover `ls -la`), so the guard alone decides. */
export function agyGrants(policy: AgyPolicy, directory?: string) {
  const grants: string[] = [];
  for (const [server, tools] of Object.entries(policy.mcp))
    if (tools === "*") grants.push(`mcp(${server}/*)`);
    else grants.push(...tools.map((t) => `mcp(${server}/${t})`));
  if (policy.command === "any" || policy.command === "guard") grants.push("command(*)");
  if (policy.web) grants.push("read_url(*)");
  // agy's tool-safety monitor may ask before a file write; unanswered in print
  // mode, that ends the run. The run folder is granted (the hook keeps writes there).
  if (policy.write === "cwd" && directory) {
    const forward = directory.replace(/\\/g, "/");
    grants.push(`write_file(${directory})`);
    if (forward !== directory) grants.push(`write_file(${forward})`);
  }
  return grants;
}

export function agySkillDirectory() {
  return (
    process.env.LLCAD_AGY_SKILL_DIR ??
    join(homedir(), ".gemini/config/plugins/llcad/skills/llcad-modelling")
  );
}
export function agyHookScript(skillDir = agySkillDirectory()) {
  const path = join(skillDir, "scripts/agy_hook.py");
  return existsSync(path) ? path : null;
}

/** Writes .agents/hooks.json, .llcad/agy-policy.json and .llcad/agy-settings.json
 *  into the run folder; returns the settings file for --settings. */
export function writeAgyWorkspace(o: {
  directory: string;
  policy: AgyPolicy;
  lifecycle: boolean;
  root: string;
  hookScript?: string | null;
  python?: string;
}) {
  const work = join(o.directory, ".llcad"),
    agents = join(o.directory, ".agents");
  mkdirSync(work, { recursive: true, mode: 0o700 });
  mkdirSync(agents, { recursive: true, mode: 0o700 });
  const policyFile = join(work, "agy-policy.json");
  writeFileSync(policyFile, JSON.stringify(o.policy, null, 1), { mode: 0o600 });
  const hook = o.hookScript === undefined ? agyHookScript() : o.hookScript;
  const python = o.python ?? venvPython(o.root);
  // agy runs a hook through `cmd /c` on Windows, which strips the first and last
  // quote of a command that starts with one: quote only a path that needs it.
  const q = (s: string) => {
    const path = s.replace(/\\/g, "/");
    return /\s/.test(path) ? `"${path}"` : path;
  };
  const command = (extra: string) =>
    hook
      ? `${q(python)} -X utf8 ${q(hook)} --scope run --policy .llcad/agy-policy.json${extra}`
      : "";
  // Hooks run with the folder of hooks.json as working directory (.agents), so the
  // policy path is resolved against the agent's cwd that agy passes in the payload.
  const tool = (extra: string, timeout: number) => [
    { matcher: "*", hooks: [{ type: "command", command: command(extra), timeout }] },
  ];
  const flat = (extra: string, timeout: number) => [
    { type: "command", command: command(extra), timeout },
  ];
  const hooks: Record<string, unknown> = hook
    ? {
        "llcad-run": {
          PreToolUse: tool(o.lifecycle ? " --lifecycle" : "", 30),
          ...(o.lifecycle
            ? {
                SessionStart: flat(" --lifecycle", 15),
                // agy writes a tool's result into the transcript only after its
                // PostToolUse hooks: that hook just records the step, and the next
                // PreInvocation waits for jobs, runs the gates and injects the result.
                PreInvocation: flat(" --lifecycle", 420),
                PostToolUse: [
                  {
                    matcher: "call_mcp_tool|run_command",
                    hooks: [{ type: "command", command: command(" --lifecycle"), timeout: 20 }],
                  },
                ],
                Stop: flat(" --lifecycle", 200),
              }
            : {}),
        },
      }
    : {};
  writeFileSync(join(agents, "hooks.json"), JSON.stringify(hooks, null, 1), {
    mode: 0o600,
  });
  const settingsFile = join(work, "agy-settings.json");
  writeFileSync(
    settingsFile,
    JSON.stringify({ permissions: { allow: agyGrants(o.policy, o.directory) } }, null, 1),
    { mode: 0o600 },
  );
  return { settingsFile, policyFile, hooked: !!hook };
}

/** The llcad server the run reaches must be registered with agy. */
export function agyHasLlcad() {
  try {
    const cfg = JSON.parse(
      readFileSync(join(homedir(), ".gemini/config/mcp_config.json"), "utf8"),
    );
    const entry = cfg?.mcpServers?.llcad;
    return !!entry && entry.disabled !== true;
  } catch {
    return false;
  }
}

/** The command line of one agy run. Long prompts go through a file: Windows caps
 *  a command line at 32 767 characters. */
export function agyArgs(o: {
  prompt: string;
  model: string;
  settingsFile: string;
  directory: string;
  resume?: string | null;
  jsonSchema?: unknown;
  addDirs?: string[];
  /** accept-edits: agy confirms file edits itself (print mode cannot ask). */
  mode?: "accept-edits";
}) {
  let prompt = o.prompt;
  if (Buffer.byteLength(prompt) > 24_000) {
    const file = join(o.directory, ".llcad", "agy-task.md");
    writeFileSync(file, prompt, { mode: 0o600 });
    prompt = `Your complete task is in the file ${file.replace(/\\/g, "/")}. Read it in full first (view_file) and carry it out exactly as written; it is part of this instruction.`;
  }
  const args = [
    "--print",
    prompt,
    "--model",
    o.model,
    "--output-format",
    "stream-json",
    "--settings",
    o.settingsFile,
  ];
  if (o.mode) args.push("--mode", o.mode);
  if (o.resume) args.push("--conversation", o.resume);
  if (o.jsonSchema) {
    const file = join(o.directory, ".llcad", "agy-schema.json");
    writeFileSync(file, JSON.stringify(o.jsonSchema), { mode: 0o600 });
    args.push("--json-schema", file);
  }
  for (const dir of o.addDirs ?? []) args.push("--add-dir", dir);
  return args;
}

/** How a run under agy differs from what the stage prompts (written for Claude
 *  Code) say: tool names, the MCP call shape and where hook results appear. */
export function agyPreamble(o: { skillDir: string | null; briefing: string | null }) {
  return [
    "RUNTIME ENVIRONMENT: Antigravity (agy) in print mode — nobody answers questions.",
    "Tool names in this task (written for Claude Code) mean here: Read = view_file (also for looking at images), Write/Edit = write_to_file or replace_file_content, Bash = run_command (Windows: PowerShell; `python3` is the package Python with PIL and numpy), WebSearch = search_web, WebFetch = read_url_content, Glob/Grep = find_by_name/grep_search. List folders with list_dir, not with the shell.",
    'Call LLcad tools with call_mcp_tool {ServerName: "llcad", ToolName: "cad_…", Arguments: {…}}; their schemas are files under ~/.gemini/antigravity-cli/mcp/llcad/<tool>.json. What is not allowed in this run is rejected by a run rule with a reason — do not look for a workaround then.',
    "Hook results (awaited jobs, gates tables, time signals, stop gate) appear as a system message before your next step; use them instead of an earlier queued/running status.",
    ...(o.skillDir
      ? [
          `SKILL llcad-modelling (agy version): ${o.skillDir.replace(/\\/g, "/")} — SKILL.md, reference/*.md, scripts/ (DSL client llcad.py, ledger.py). In paths of the task, replace the skill directory with this one.`,
        ]
      : []),
    ...(o.briefing ? ["", o.briefing.trim()] : []),
    "",
    "— TASK —",
    "",
    "",
  ].join("\n");
}

/** One agy stream-json result in the shape the run bookkeeping reads. */
export function agyResult(r: any) {
  const ok = r?.status === "SUCCESS";
  return {
    type: "result",
    runtime: "agy",
    subtype: ok ? "success" : String(r?.finished_reason ?? r?.status ?? "error").toLowerCase(),
    is_error: !ok,
    result: ok
      ? String(r?.response ?? "")
      : String(r?.error ?? r?.response ?? "agy reported an error."),
    session_id: r?.conversation_id || null,
    structured_output: r?.structured_output,
    duration_ms:
      typeof r?.duration_seconds === "number" ? Math.round(r.duration_seconds * 1000) : null,
    total_cost_usd: 0,
    usage: r?.usage,
    denied_actions: r?.denied_actions,
  };
}
