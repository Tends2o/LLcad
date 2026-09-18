import { spawn, type ChildProcess } from "node:child_process";
import {
  createWriteStream,
  existsSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { join } from "node:path";
import { homedir } from "node:os";
import type { Store } from "./store.js";
import { Principal, authorize } from "../policy/index.js";
import { requireThat } from "../semantic-ir/errors.js";
import {
  annotationDirectory,
  attached,
  dropFiles,
  setState,
  writeRecord,
} from "./annotations.js";
import { OPERATORS } from "../compiler/index.js";
/** Handing a marked-up change order to Claude Code. The agent runs in the
 *  annotation's own folder, so the order, the measurements and the two images
 *  are simply files next to it, and it reaches the model through the llcad MCP
 *  like any other client — this service never edits geometry on its behalf. */
export const EFFORTS = ["low", "medium", "high", "xhigh", "max"] as const;
export const PERMISSION_MODES = [
  "dontAsk",
  "acceptEdits",
  "auto",
  "plan",
  "bypassPermissions",
] as const;
/** Nobody is sitting in front of this run, so every tool it may use has to be
 *  named up front: "dontAsk" then grants exactly these and denies the rest
 *  instead of waiting for an answer. The blanket mode is no use here — Claude
 *  Code refuses it for a process running as root, which a service does. */
const ALLOWED_TOOLS = [
  "Bash",
  "Read",
  "Edit",
  "Write",
  "Glob",
  "Grep",
  "WebFetch",
  "WebSearch",
  "Task",
  "TodoWrite",
  "NotebookEdit",
  "mcp__llcad",
];
/** Reading an order and writing it down needs no more than the files in front
 *  of it. Everything else only costs context — and turns spent looking for it. */
const READING_TOOLS = ["Read", "Glob"];
export const AGENT_DEFAULTS = {
  model: "opus",
  effort: "high",
  proposal_effort: "medium",
  permission_mode: "dontAsk",
  max_budget_usd: null as number | null,
  timeout_minutes: 45,
};
export type AgentSettings = typeof AGENT_DEFAULTS;
const MAX_RUNNING = 2;
export function readSettings(store: Store, p: Principal): AgentSettings {
  authorize(p, "model:read");
  const row = store.get(
    "SELECT config FROM agent_settings WHERE tenant=?",
    p.tenant,
  );
  return { ...AGENT_DEFAULTS, ...(row ? JSON.parse(row.config) : {}) };
}
export function writeSettings(store: Store, p: Principal, input: any) {
  authorize(p, "model:edit");
  const settings: AgentSettings = {
    model: String(input.model ?? AGENT_DEFAULTS.model),
    effort: String(input.effort ?? AGENT_DEFAULTS.effort),
    proposal_effort: String(
      input.proposal_effort ?? AGENT_DEFAULTS.proposal_effort,
    ),
    permission_mode: String(
      input.permission_mode ?? AGENT_DEFAULTS.permission_mode,
    ),
    max_budget_usd:
      input.max_budget_usd == null ? null : Number(input.max_budget_usd),
    timeout_minutes: Number(
      input.timeout_minutes ?? AGENT_DEFAULTS.timeout_minutes,
    ),
  };
  requireThat(
    /^[a-z0-9][a-z0-9.\-]{0,63}$/.test(settings.model),
    "INVALID_SCHEMA",
    "Modellname ist kein zulässiger Claude-Code-Alias.",
  );
  requireThat(
    (EFFORTS as readonly string[]).includes(settings.effort) &&
      (EFFORTS as readonly string[]).includes(settings.proposal_effort),
    "INVALID_SCHEMA",
    "Aufwandstufe unbekannt.",
  );
  requireThat(
    (PERMISSION_MODES as readonly string[]).includes(settings.permission_mode),
    "INVALID_SCHEMA",
    "Rechtemodus unbekannt.",
  );
  requireThat(
    settings.max_budget_usd === null ||
      (Number.isFinite(settings.max_budget_usd) &&
        settings.max_budget_usd > 0 &&
        settings.max_budget_usd <= 100),
    "INVALID_SCHEMA",
    "Kostengrenze muss zwischen 0 und 100 US-Dollar liegen.",
  );
  requireThat(
    Number.isInteger(settings.timeout_minutes) &&
      settings.timeout_minutes >= 5 &&
      settings.timeout_minutes <= 180,
    "INVALID_SCHEMA",
    "Zeitgrenze muss zwischen 5 und 180 Minuten liegen.",
  );
  store.run(
    "INSERT INTO agent_settings(tenant,config,updated) VALUES(?,?,?) ON CONFLICT(tenant) DO UPDATE SET config=excluded.config,updated=excluded.updated",
    p.tenant,
    JSON.stringify(settings),
    new Date().toISOString(),
  );
  return settings;
}
/** The run reaches the model through llcad and nothing else. Whatever other MCP
 *  servers this machine has configured stay out: their tool schemas would fill
 *  the agent's context — a single run cost half a dollar that way — and the rule
 *  here is that geometry is changed through llcad only. */
function mcpConfig(store: Store, directory: string, llcad = true) {
  const key = join(store.root, "local-token");
  if (llcad && !existsSync(key)) return null;
  const path = join(directory, "mcp.json");
  writeFileSync(
    path,
    JSON.stringify({
      // No servers at all for a run that only reads the order in its folder.
      mcpServers: !llcad
        ? {}
        : {
            llcad: {
              type: "http",
              url: new URL(
                "/mcp",
                process.env.MATHFORGE_PUBLIC_URL ?? "http://127.0.0.1:4310",
              ).toString(),
              headers: {
                Authorization: `Bearer ${readFileSync(key, "utf8").trim()}`,
              },
            },
          },
    }),
    { mode: 0o600 },
  );
  return path;
}
/** The shape of a patch, written from the registry the service actually runs.
 *  A run that has to discover the schema by trial and error wastes its turns on
 *  it — and a small model gives up and reports the change as impossible. */
function toolGuide(directory: string) {
  const operator = (name: string, spec: any) => {
    const params = Object.entries(spec.params ?? {})
      .map(
        ([key, p]: [string, any]) =>
          `${key}${p.optional ? "?" : ""}:${p.dimension}`,
      )
      .join(", ");
    return `| \`${name}\` | ${spec.refs[0]}–${spec.refs[1]} | ${params || "—"} |`;
  };
  writeFileSync(
    join(directory, "werkzeuge.md"),
    [
      "# So ändert man dieses Modell",
      "",
      'Alle Geometrie läuft über das llcad-MCP. Ein Feature wird nie "beschrieben",',
      "sondern als Eintrag im Konstruktionsbaum angelegt: ein Operator, seine",
      "Parameter, und die Features, auf denen er aufbaut (`depends_on`).",
      "",
      "## Ablauf",
      "",
      "1. `cad_get_model` — aktuellen Stand und `base_revision` holen.",
      "2. `cad_apply_patch` — Operationen anwenden (siehe unten). Liefert einen Job.",
      "3. `cad_job_get` — pollen, bis `succeeded`.",
      "4. `cad_validate` — prüfen; das Ergebnis enthält `result.digest`.",
      "5. `cad_commit` — mit `validation_digest` übernehmen.",
      "",
      "## Ein Loch in einen Körper: genau so",
      "",
      "```json",
      JSON.stringify(
        {
          model_id: "<model_id>",
          base_revision: "<rev>",
          idempotency_key: "<eindeutig>",
          operations: [
            {
              op: "add_feature",
              feature: {
                id: "bohrwerkzeug",
                semantic_name: "Bohrwerkzeug Ø6",
                kind: "solid",
                owner_part: "part-main",
                local_frame: "world",
                authoritative_representation: "brep",
                parameters: {
                  radius: { value: "3", unit: "mm" },
                  height: { value: "20", unit: "mm" },
                  x: { value: "0", unit: "mm" },
                  y: { value: "0", unit: "mm" },
                  z: { value: "0", unit: "mm" },
                },
                expressions: {},
                parameter_sources: {},
                construction: { operator: "cylinder" },
                depends_on: [],
                protected_relations: [],
              },
            },
            {
              op: "add_feature",
              feature: {
                id: "block-gebohrt",
                semantic_name: "Block mit Bohrung",
                kind: "solid",
                owner_part: "part-main",
                local_frame: "world",
                authoritative_representation: "brep",
                parameters: {},
                expressions: {},
                parameter_sources: {},
                construction: { operator: "difference" },
                depends_on: ["grundkoerper", "bohrwerkzeug"],
                protected_relations: [],
              },
            },
            { op: "set_outputs", outputs: ["block-gebohrt"] },
          ],
        },
        null,
        1,
      ),
      "```",
      "",
      "Merksätze:",
      "",
      "- `construction` enthält **nur** `{ operator: ... }` (und bei wenigen Operatoren",
      "  zusätzliche Felder wie `points`); alle Maße stehen in `parameters` als",
      '  `{ value: "12.5", unit: "mm" }` — Zahlen immer als Zeichenkette.',
      "- Boolesche Operationen (`difference`, `union`, `intersection`) haben **keine**",
      "  Parameter; sie verweisen über `depends_on` auf ihre Eingänge, erster Eintrag",
      "  ist der Grundkörper.",
      "- Ein neues Endergebnis muss mit `set_outputs` zur Ausgabe erklärt werden,",
      "  sonst bleibt das alte Feature das sichtbare Teil.",
      "- Bestehende Features ändert man mit `set_parameter` (mit `expected`) oder",
      "  `set_construction` (mit `expected_hash` aus `cad_inspect`).",
      "",
      "## Wo ein Grundkörper sitzt",
      "",
      "- `box`: (x, y, z) ist die **Ecke mit den kleinsten Koordinaten**; der Körper",
      "  wächst von dort um width/depth/height in +X/+Y/+Z. Seine Mitte liegt also",
      "  bei (x + width/2, y + depth/2, z + height/2).",
      "- `cylinder`, `cone`, `torus`, `circle`: (x, y, z) ist der **Mittelpunkt der",
      "  Grundfläche**, die Achse zeigt in +Z.",
      "- `sphere`: (x, y, z) ist der **Mittelpunkt**.",
      "- Beispiel: ein Loch mittig durch eine Box, die bei (0, 0, 0) beginnt und",
      "  30 × 20 × 10 groß ist, braucht den Zylinder bei x = 15, y = 10 — nicht bei",
      "  (0, 0). Rechne die Mitte aus, statt die Koordinaten des Grundkörpers zu",
      "  übernehmen.",
      "",
      "## Operatoren dieses Dienstes",
      "",
      "| Operator | Eingänge | Parameter |",
      "| --- | --- | --- |",
      ...Object.entries(OPERATORS).map(([name, spec]) => operator(name, spec)),
      "",
    ].join("\n"),
    { mode: 0o600 },
  );
}
/** The command line, resolved once: a service PATH rarely contains ~/.local/bin. */
function executable() {
  const candidates = [
    process.env.MATHFORGE_CLAUDE_CLI,
    join(homedir(), ".local/bin/claude"),
    "/usr/local/bin/claude",
    "/usr/bin/claude",
  ].filter(Boolean) as string[];
  return candidates.find((path) => existsSync(path)) ?? "claude";
}
export class Agents {
  private running = new Map<string, ChildProcess>();
  /** Runs a person called off: their end is not a failure. */
  private called_off = new Set<string>();
  constructor(private store: Store) {}
  /** A child does not survive its parent: runs left over from an earlier start
   *  are reported as failed rather than waited for forever. */
  recover() {
    for (const row of this.store.all(
      "SELECT id,run FROM annotations WHERE state IN ('queued','running')",
    ))
      setState(this.store, row.id, "failed", {
        ...(row.run ? JSON.parse(row.run) : {}),
        finished: new Date().toISOString(),
        error: "Der Dienst wurde neu gestartet, während der Auftrag lief.",
      });
  }
  dispatch(
    p: Principal,
    annotation: string,
    stage: "proposal" | "execution" | "purpose" | "build" = "proposal",
  ) {
    authorize(p, "model:edit");
    const row = this.store.get(
      "SELECT * FROM annotations WHERE id=? AND tenant=?",
      annotation,
      p.tenant,
    );
    requireThat(row, "ACCESS_DENIED", "Markierung nicht zugänglich.");
    this.store.model(p, row.model, "model:edit");
    requireThat(
      !this.running.has(annotation),
      "CONSTRAINT_CONFLICT",
      "Dieser Auftrag läuft bereits.",
    );
    requireThat(
      this.running.size < MAX_RUNNING,
      "BUDGET_EXCEEDED",
      "Es laufen bereits zwei Aufträge; bitte abwarten.",
    );
    const settings = readSettings(this.store, p),
      directory = annotationDirectory(this.store, annotation),
      earlier = row.run ? JSON.parse(row.run) : {},
      proposing = stage === "proposal",
      executing = stage === "execution",
      // Carrying out a change and building a model both write geometry.
      writing = executing || stage === "build",
      mode = writing
        ? settings.permission_mode
        : stage === "purpose"
          ? "dontAsk"
          : "plan",
      effort = writing ? settings.effort : settings.proposal_effort;
    requireThat(
      !executing || earlier.proposal,
      "CONSTRAINT_CONFLICT",
      "Zuerst muss ein Vorschlag vorliegen.",
    );
    this.called_off.delete(annotation);
    writeRecord(this.store, annotation);
    // A run that only reads and writes a text needs no patch grammar.
    if (stage !== "purpose") toolGuide(directory);
    const payload = JSON.parse(row.payload),
      plan =
        this.store.get("SELECT purpose FROM models WHERE id=?", row.model)
          ?.purpose ?? "",
      command = executable(),
      args = [
        "--print",
        prompt(row, payload, stage, earlier.proposal, plan),
        "--model",
        settings.model,
        "--effort",
        effort,
        "--permission-mode",
        // Only a run that builds may touch the model. A proposal plans; a
        // creation order may not even do that — it reads and answers.
        mode,
        "--output-format",
        "stream-json",
        "--verbose",
      ];
    // The execution carries on in the same conversation, so it keeps what the
    // proposal already found out instead of reading the model a second time.
    if (executing && earlier.session_id)
      args.push("--resume", earlier.session_id);
    if (stage === "purpose")
      args.push(
        "--allowedTools",
        ...READING_TOOLS,
        "--permission-prompts",
        "none",
      );
    else if (!writing || settings.permission_mode !== "bypassPermissions")
      args.push(
        "--allowedTools",
        ...ALLOWED_TOOLS,
        "--permission-prompts",
        "none",
      );
    const servers = mcpConfig(this.store, directory, stage !== "purpose");
    if (servers) args.push("--mcp-config", servers, "--strict-mcp-config");
    if (settings.max_budget_usd)
      args.push("--max-budget-usd", String(settings.max_budget_usd));
    const started = new Date().toISOString(),
      run = {
        ...(executing ? earlier : {}),
        stage,
        started,
        model: settings.model,
        effort,
        permission_mode: mode,
        command: [command, ...args.filter((a) => a !== args[1])].join(" "),
        activity: "Claude Code startet …",
      };
    setState(this.store, annotation, "running", run);
    const log = createWriteStream(join(directory, "run.jsonl"), {
        mode: 0o600,
      }),
      errors = createWriteStream(join(directory, "run.err.log"), {
        mode: 0o600,
      });
    const child = spawn(command, args, {
      cwd: directory,
      env: {
        ...process.env,
        HOME: process.env.HOME ?? homedir(),
        PATH: `${join(homedir(), ".local/bin")}:${process.env.PATH ?? "/usr/local/bin:/usr/bin:/bin"}`,
      },
      stdio: ["ignore", "pipe", "pipe"],
    });
    this.running.set(annotation, child);
    const timer = setTimeout(() => {
      run.activity = "Zeitgrenze erreicht.";
      child.kill("SIGTERM");
    }, settings.timeout_minutes * 60_000);
    timer.unref();
    let rest = "",
      result: any = null,
      spoken: string | null = null,
      turns = 0;
    child.stdout.on("data", (chunk: Buffer) => {
      log.write(chunk);
      rest += chunk.toString();
      const lines = rest.split("\n");
      rest = lines.pop() ?? "";
      for (const line of lines) {
        if (!line.trim()) continue;
        try {
          const event = JSON.parse(line);
          if (event.type === "result") result = event;
          spoken = said(event) ?? spoken;
          const activity = describe(event);
          if (activity) {
            turns++;
            run.activity = activity;
            this.note(annotation, { ...run, turns });
          }
        } catch {
          /* a line that is not an event is only interesting in the log */
        }
      }
    });
    child.stderr.on("data", (chunk: Buffer) => errors.write(chunk));
    child.on("error", (error) =>
      this.finish(annotation, run, "failed", {
        error: `Claude Code konnte nicht gestartet werden: ${error.message}`,
      }),
    );
    child.on("close", (code) => {
      clearTimeout(timer);
      this.running.delete(annotation);
      log.end();
      errors.end();
      const called_off = this.called_off.delete(annotation);
      const failed = !called_off && (code !== 0 || !result || result.is_error);
      const answer =
        typeof result?.result === "string" ? result.result : spoken;
      this.finish(
        annotation,
        run,
        called_off
          ? "cancelled"
          : failed
            ? "failed"
            : proposing
              ? "proposed"
              : "succeeded",
        {
          exit_code: code,
          turns,
          // Whatever the agent said last is worth keeping even when the run
          // was cut short: that is where its reasoning stands.
          summary: answer,
          proposal: proposing && !failed ? answer : (earlier.proposal ?? null),
          cost_usd:
            (proposing ? 0 : (earlier.cost_usd ?? 0)) +
            (result?.total_cost_usd ?? 0),
          duration_ms: result?.duration_ms ?? null,
          session_id: result?.session_id ?? earlier.session_id ?? null,
          error: failed ? reason(result, code, settings) : null,
        },
      );
    });
    return { status: "running", annotation_id: annotation, run };
  }
  cancel(p: Principal, annotation: string) {
    authorize(p, "model:edit");
    const child = this.running.get(annotation);
    if (child) {
      this.called_off.add(annotation);
      child.kill("SIGTERM");
      return { status: "cancelling", annotation_id: annotation };
    }
    // An order that has not started yet is simply called off, and it gives its
    // working material back at once.
    const row = this.store.get(
      "SELECT state FROM annotations WHERE id=? AND tenant=?",
      annotation,
      p.tenant,
    );
    requireThat(row, "ACCESS_DENIED", "Auftrag nicht zugänglich.");
    requireThat(
      row.state !== "cancelled",
      "CONSTRAINT_CONFLICT",
      "Dieser Auftrag ist bereits abgebrochen.",
    );
    setState(this.store, annotation, "cancelled");
    dropFiles(this.store, annotation);
    return { status: "cancelled", annotation_id: annotation };
  }
  private note(annotation: string, run: Record<string, unknown>) {
    try {
      this.store.run(
        "UPDATE annotations SET run=? WHERE id=? AND state='running'",
        JSON.stringify(run),
        annotation,
      );
    } catch {
      /* progress is telemetry; the run itself carries on */
    }
  }
  /** The answer of a creation order is the model's purpose: no tool sets it, so
   *  it is written here. The pictures that led to it were working material and
   *  leave with it. */
  private adoptPurpose(annotation: string, answer: unknown) {
    const row = this.store.get(
        "SELECT model FROM annotations WHERE id=?",
        annotation,
      ),
      purpose = String(answer ?? "")
        .replace(/^\s*```[a-z]*\n?|```\s*$/g, "")
        .trim()
        .slice(0, 4000);
    if (row && purpose)
      this.store.run(
        "UPDATE models SET purpose=? WHERE id=?",
        purpose,
        row.model,
      );
    dropFiles(this.store, annotation);
  }
  private finish(
    annotation: string,
    run: Record<string, unknown>,
    state: "succeeded" | "failed" | "proposed" | "cancelled",
    extra: Record<string, unknown>,
  ) {
    this.running.delete(annotation);
    // The key the agent borrowed does not outlive its run.
    rmSync(join(annotationDirectory(this.store, annotation), "mcp.json"), {
      force: true,
    });
    setState(this.store, annotation, state, {
      ...run,
      ...extra,
      finished: new Date().toISOString(),
    });
    if (run.stage === "purpose" && state === "succeeded")
      this.adoptPurpose(annotation, extra.summary);
    if (run.stage === "purpose" && state === "cancelled")
      dropFiles(this.store, annotation);
    writeRecord(this.store, annotation);
  }
}
/** The agent's own words, kept as the summary when a run ends early. */
function said(event: any): string | null {
  if (event.type !== "assistant") return null;
  const text = (event.message?.content ?? [])
    .filter((block: any) => block.type === "text" && block.text.trim())
    .map((block: any) => block.text.trim())
    .join("\n");
  return text || null;
}
/** Why a run ended, in the words of the person who will read it. */
function reason(result: any, code: number | null, settings: AgentSettings) {
  if (result?.subtype === "error_max_budget_usd")
    return `Kostengrenze von ${settings.max_budget_usd} US-Dollar erreicht; der Auftrag wurde abgebrochen.`;
  if (result?.subtype === "error_max_turns")
    return "Claude Code hat die Zugrenze erreicht.";
  if (typeof result?.result === "string" && result.is_error)
    return result.result;
  return `Claude Code endete mit Code ${code}.`;
}
/** One short line per event, so the phone can show what the agent is doing. */
function describe(event: any): string | null {
  if (event.type === "assistant") {
    for (const block of event.message?.content ?? []) {
      if (block.type === "tool_use")
        return `${block.name}${block.input?.model_id ? ` · ${block.input.model_id}` : ""}`;
      if (block.type === "text" && block.text.trim())
        return block.text.trim().split("\n")[0].slice(0, 120);
    }
    return null;
  }
  if (event.type === "result") return "Fertig.";
  if (event.type === "system" && event.subtype === "init")
    return `Werkzeuge bereit (${(event.mcp_servers ?? []).map((s: any) => s.name).join(", ") || "keine MCP-Server"})`;
  return null;
}
/** Attachments by name and kind, so a prompt can say what is actually there. */
function describeFiles(files: any[]) {
  return files
    .map(
      (f) =>
        `${f.file} (${f.file.endsWith(".stl") ? "STL-Netz" : "Bild"}${f.bytes ? `, ${Math.round(f.bytes / 1024)} KiB` : ""})`,
    )
    .join(", ");
}
/** A new model, described in words, in pictures or in both: this run reads them
 *  and writes the order down properly. That text becomes the model's purpose. */
function creationPrompt(row: any, payload: any) {
  const files = attached(payload);
  return [
    "Du bist der Aufnahme-Agent dieses CAD-Projekts. Eine Person hat ein neues Modell angelegt und beschrieben, was darin entstehen soll — in Worten, in Bildern oder in beidem. Formuliere daraus den Bauplan, nach dem das Modell anschließend gebaut wird. Er wird einem Konstruktionsagenten vorgelegt, der nur diesen Text und die llcad-Werkzeuge hat; schreibe ihn so, dass er danach bauen kann.",
    "",
    `MODELLNAME: ${payload.model_name ?? "—"}`,
    `AUFTRAG IM WORTLAUT: ${row.note || "(kein Text — die Bilder sind der Auftrag)"}`,
    `Modell: model_id ${row.model}, Revision ${row.revision}, noch ohne Geometrie.`,
    "",
    files.length
      ? `Im Arbeitsverzeichnis liegen auftrag.md und ${files.length} Anhang/Anhänge: ${describeFiles(files)}. Sieh dir jedes Bild mit dem Read-Werkzeug wirklich an.`
      : "Im Arbeitsverzeichnis liegt auftrag.md. Anhänge gibt es zu diesem Auftrag nicht.",
    "",
    "Vorgehen:",
    files.length
      ? "1. Lies auftrag.md. Öffne jedes Bild mit Read und halte fest, was darauf zu sehen ist: Bauteil, Ansicht, Maßzahlen, Beschriftungen, Toleranzen, Werkstoffe, Schnittstellen, Normteile. Liegt ein STL dabei, nenne seine Hüllmaße, soweit sie im Text stehen, und schreibe unter **Offen**, dass die genaue Kontur aus der Datei kommt — vermessen darf diese Runde nicht."
      : "1. Lies auftrag.md.",
    "2. Ändere nichts. Diese Runde schreibt nur Text; das Modell bleibt leer.",
    "3. Stelle keine Rückfragen — was offen bleibt, gehört unter **Offen** in die Antwort.",
    "",
    "Antworte auf Deutsch mit genau dem Text, der als Bauplan gespeichert wird: kein Vorspann, keine Anrede, kein Codeblock, höchstens 1800 Zeichen, in genau dieser Gliederung:",
    "**Bauteil:** was gebaut wird und wofür es da ist, in ein bis zwei Sätzen.",
    "**Gestalt:** die Bauform, wie sie aus Text und Bildern hervorgeht.",
    '**Maße:** alle erkennbaren Maße in Millimetern mit ihrem Bezug (Durchmesser, Länge, Wandstärke, Lochbild …). Geschätztes kennzeichne mit „~".',
    "**Schnittstellen:** Anschlüsse, Gewinde, Passungen, Gegenstücke.",
    "**Anforderungen:** Werkstoff, Toleranzen, Fertigungsverfahren, Normen, soweit genannt oder erkennbar.",
    "**Offen:** was vor dem Bauen noch geklärt werden muss.",
  ].join("\n");
}
/** A plan exists, the model is empty: build it. This is the only run that may
 *  create geometry from nothing, and it does it the way every other client
 *  does — through the llcad MCP, patch by committed patch. */
function buildPrompt(row: any, payload: any, plan: string) {
  return [
    "Du bist der Konstruktionsagent dieses CAD-Projekts. Für ein Modell liegt ein Bauplan vor. Baue ihn.",
    "",
    `MODELL: ${payload.model_name ?? row.model} (model_id ${row.model})`,
    `BASIS-REVISION: ${row.revision}`,
    "",
    "BAUPLAN:",
    plan || "(kein Plan hinterlegt — dann baue nichts und sage das)",
    "",
    "Vorgehen:",
    "1. Lies werkzeuge.md im Arbeitsverzeichnis: dort steht die genaue Form eines Patches und jeder Operator dieses Dienstes. Rate das Schema nicht.",
    "2. Arbeite ausschließlich über das llcad-MCP: cad_get_model für den Stand, cad_plan_edit als Trockenlauf, cad_apply_patch zum Bauen, cad_job_get zum Pollen, cad_validate, dann cad_commit mit dem zurückgegebenen Prüf-Digest.",
    "   Hat das Modell schon Features, wurde hier bereits gebaut: setze fort, statt neu anzufangen, und ändere Bestehendes nur, wenn der Plan es verlangt.",
    "3. Baue in mehreren Schritten statt in einem Riesenpatch: Grundkörper, dann Aussparungen, dann Details. Übernimm jeden geprüften Zwischenstand sofort mit cad_commit — so bleibt erhalten, was fertig ist, auch wenn eine Runde an ihre Zeitgrenze kommt.",
    "4. Millimeter als Einheit, Zahlen als Zeichenkette. Jedes sichtbare Endergebnis gehört über set_outputs zur Ausgabe und über set_structure in ein benanntes Teil, damit der Betrachter es benennen und einfärben kann.",
    "5. Halte dich an den Plan. Was dort unter **Offen** steht, entscheidest du selbst plausibel und schreibst am Ende auf, wie du entschieden hast.",
    "",
    "Antworte zum Schluss auf Deutsch in höchstens zehn Zeilen: was gebaut wurde, welche Revision entstanden ist, welche Maße du geprüft hast, welche Annahmen du getroffen hast und was offen bleibt.",
  ].join("\n");
}
function prompt(
  row: any,
  payload: any,
  stage: "proposal" | "execution" | "purpose" | "build",
  proposal?: string | null,
  plan = "",
) {
  if (stage === "purpose") return creationPrompt(row, payload);
  if (stage === "build") return buildPrompt(row, payload, plan);
  const number = (value: unknown) =>
    typeof value === "number" ? value.toFixed(3) : "—";
  const point = (p: number[] | undefined) =>
    p ? `(${number(p[0])} | ${number(p[1])} | ${number(p[2])}) mm` : "—";
  const parts = (payload.parts ?? []) as any[],
    features = (payload.features ?? []) as any[];
  return [
    "Du bist der CAD-Änderungsagent dieses Projekts. Eine Person hat im 3D-Viewer eine Stelle am Modell rot markiert und dazu einen Auftrag geschrieben.",
    "",
    `AUFTRAG: ${row.note}`,
    "",
    `Modell: ${payload.model_name ?? row.model} (model_id ${row.model})`,
    `Revision: ${row.revision}`,
    `Markierung: Mittelpunkt ${point(payload.world?.centre)}, Bereich ${point(payload.world?.min)} bis ${point(payload.world?.max)}, Blickabstand ${number(payload.view?.distance_mm)} mm bei ${number(payload.view?.mm_per_pixel)} mm/px.`,
    parts.length
      ? `Berührte Teile: ${parts.map((part) => `${part.name ?? part.part_id} (${part.part_id}, ${number((part.coverage ?? 0) * 100)} % der Fläche)`).join("; ")}`
      : "Berührte Teile: keine erkannt.",
    features.length
      ? `Berührte Features: ${features.map((f) => `${f.semantic_name ?? f.feature_id} (${f.feature_id}${f.face_id ? `, Fläche ${f.face_id}` : ""})`).join("; ")}`
      : "Berührte Features: keine erkannt.",
    "",
    "Im aktuellen Arbeitsverzeichnis liegen: auftrag.md (dieser Auftrag als Text), annotation.json (alle Messwerte der Markierung, inklusive Kamera, Bildschirm- und Weltkoordinaten), werkzeuge.md (die genaue Form eines Patches und alle Operatoren dieses Dienstes), region.png (Bildausschnitt mit der roten Markierung) und view.jpg (die ganze Ansicht).",
    ...(attached(payload).length
      ? [
          `Dazu hat die Person angehängt: ${describeFiles(attached(payload))}. Sieh dir jedes Bild mit Read an. Ein STL ist eine Vorlage, kein Modellteil: vermiss es mit python3 (Hüllmaße, Querschnitte, Umriss) und baue die Geometrie daraus über das llcad-MCP nach — importiere es nicht.`,
        ]
      : []),
    "",
    ...(stage === "proposal"
      ? [
          "Diese Runde ist die Planungsrunde: Du änderst noch nichts, du schlägst vor.",
          "",
          "Vorgehen:",
          "1. Lies auftrag.md, annotation.json und werkzeuge.md, sieh dir region.png an.",
          "2. Sieh dir den betroffenen Stand ausschließlich über das llcad-MCP an (cad_get_model, cad_find, cad_inspect, cad_measure, cad_plan_edit als Trockenlauf).",
          "3. Erarbeite daraus einen konkreten Umsetzungsvorschlag.",
          "",
          "Antworte auf Deutsch, höchstens 20 Zeilen, in genau dieser Gliederung:",
          "**Vorschlag:** was du bauen würdest, in einem Satz.",
          "**Schritte:** die geplanten cad_*-Aufrufe mit den neuen Features, Operatoren und Maßen (Millimeter, konkrete Zahlen, keine Platzhalter).",
          "**Auswirkung:** welche Teile und Features berührt werden und was sich messbar ändert.",
          "**Risiken:** was dabei schiefgehen kann, welche Prüfungen greifen.",
          "**Annahmen:** was du aus dem Auftrag ableiten musstest, weil es nicht dasteht.",
          "",
          "Wenn der Auftrag so nicht umsetzbar ist, sage das in derselben Gliederung und nenne genau, was fehlt.",
        ]
      : [
          "Der folgende Vorschlag wurde von einem Menschen angenommen. Setze genau ihn um — nicht mehr und nicht weniger.",
          "",
          "ANGENOMMENER VORSCHLAG:",
          proposal ?? "(kein Vorschlag hinterlegt)",
          "",
          "Vorgehen:",
          "1. Arbeite am Modell ausschließlich über das llcad-MCP (die cad_* Werkzeuge). Kein anderes CAD-Werkzeug, keine Änderungen am Quellcode dieses Projekts. werkzeuge.md beschreibt die genaue Form eines Patches — halte dich daran, statt das Schema zu erraten.",
          "2. Ändere mit cad_apply_patch, prüfe den Kandidaten mit cad_validate und übernimm ihn mit cad_commit unter Angabe des zurückgegebenen Prüf-Digests.",
          "3. Behalte Millimeter als Einheit, verletze keine geschützten Beziehungen und lasse alles unangetastet, was der Vorschlag nicht verlangt.",
          "4. Weicht die Wirklichkeit vom Vorschlag ab, halte an und beschreibe die Abweichung, statt etwas anderes zu bauen.",
          "",
          "Antworte zum Schluss auf Deutsch in höchstens acht Zeilen: was geändert wurde, welche Revision entstanden ist, welche Prüfungen gelaufen sind und was offen bleibt.",
        ]),
  ].join("\n");
}
