import { spawnSync } from "node:child_process";
import { readdirSync, readFileSync, realpathSync, statSync } from "node:fs";
import { homedir } from "node:os";
import { basename, join, resolve, sep } from "node:path";
import { createHash } from "node:crypto";
import { requireThat } from "../semantic-ir/errors.js";
import { IS_WINDOWS, envPathList, pathInside } from "../semantic-ir/host.js";

/**
 * Fonts the `text` operator may use, resolved to files on this host once.
 *
 * Only the whitelisted open families (DejaVu: Bitstream Vera licence, Liberation: OFL,
 * Inter: OFL) are ever used; Nimbus/URW files (AGPL) are refused even when fontconfig
 * offers them as a substitute. A resolved font carries its sha256, which goes into the
 * feature's cache key, and a fixed sandbox path the worker launcher binds read-only.
 */
export const FONT_STYLES = [
  "regular",
  "bold",
  "italic",
  "bold_italic",
  "medium",
  "semibold",
] as const;
type Style = (typeof FONT_STYLES)[number];
const CANDIDATES: Record<string, Partial<Record<Style, string[]>>> = {
  "DejaVu Sans": {
    regular: ["DejaVuSans.ttf"],
    bold: ["DejaVuSans-Bold.ttf"],
    italic: ["DejaVuSans-Oblique.ttf"],
    bold_italic: ["DejaVuSans-BoldOblique.ttf"],
  },
  "DejaVu Sans Condensed": {
    regular: ["DejaVuSansCondensed.ttf"],
    bold: ["DejaVuSansCondensed-Bold.ttf"],
    italic: ["DejaVuSansCondensed-Oblique.ttf"],
    bold_italic: ["DejaVuSansCondensed-BoldOblique.ttf"],
  },
  "Liberation Sans": {
    regular: ["LiberationSans-Regular.ttf"],
    bold: ["LiberationSans-Bold.ttf"],
    italic: ["LiberationSans-Italic.ttf"],
    bold_italic: ["LiberationSans-BoldItalic.ttf"],
  },
  "Liberation Sans Narrow": {
    regular: ["LiberationSansNarrow-Regular.ttf"],
    bold: ["LiberationSansNarrow-Bold.ttf"],
    italic: ["LiberationSansNarrow-Italic.ttf"],
    bold_italic: ["LiberationSansNarrow-BoldItalic.ttf"],
  },
  Inter: {
    regular: ["Inter-Regular.ttf", "Inter.ttf"],
    medium: ["Inter-Medium.ttf", "InterMedium.ttf"],
    semibold: ["Inter-SemiBold.ttf", "InterSemiBold.ttf"],
    bold: ["Inter-Bold.ttf", "InterBold.ttf"],
    italic: ["Inter-Italic.ttf"],
    bold_italic: ["Inter-BoldItalic.ttf"],
  },
};
export type ResolvedFont = {
  family: string;
  style: string;
  host: string;
  sandbox: string;
  sha256: string;
};
const slug = (text: string) => text.toLowerCase().replace(/[^a-z0-9]+/g, "-");
const forbidden = (path: string) => /nimbus|urw/i.test(path);
let table: Map<string, ResolvedFont> | null = null;

function candidateFiles(): string[] {
  const files = new Set<string>();
  // fontconfig knows user and system fonts; a missing fc-list only narrows the search.
  const listed = IS_WINDOWS
    ? null
    : spawnSync("fc-list", ["--format", "%{file}\\n"], {
        encoding: "utf8",
        timeout: 5000,
      });
  if (listed?.status === 0)
    for (const line of listed.stdout.split("\n")) if (line.trim()) files.add(line.trim());
  const dirs = [
    ...envPathList(process.env.LLCAD_FONT_DIRS),
    ...(IS_WINDOWS
      ? [
          join(process.env.SystemRoot ?? "C:\\Windows", "Fonts"),
          join(process.env.LOCALAPPDATA ?? homedir(), "Microsoft", "Windows", "Fonts"),
        ]
      : ["/usr/share/fonts", "/usr/local/share/fonts"]),
    join(homedir(), ".local/share/fonts"),
  ];
  for (const dir of dirs) {
    try {
      for (const entry of readdirSync(dir, { recursive: true }))
        if (/\.(ttf|otf)$/i.test(String(entry))) files.add(join(dir, String(entry)));
    } catch {
      /* directory absent */
    }
  }
  return [...files].filter((f) => !forbidden(f)).sort();
}

/** Every whitelisted family/style found on this host (resolved once per process). */
export function fontTable(): Map<string, ResolvedFont> {
  if (table) return table;
  table = new Map();
  const files = candidateFiles();
  const byName = new Map<string, string>();
  for (const file of files) {
    const name = basename(file);
    if (!byName.has(name)) byName.set(name, file);
  }
  for (const [family, styles] of Object.entries(CANDIDATES))
    for (const [style, names] of Object.entries(styles)) {
      const file = names!.map((n) => byName.get(n)).find(Boolean);
      if (!file) continue;
      let host: string;
      try {
        host = realpathSync(file);
        if (!statSync(host).isFile() || forbidden(host)) continue;
      } catch {
        continue;
      }
      table.set(family + "|" + style, {
        family,
        style,
        host,
        sandbox: "/fonts/" + slug(family) + "-" + style + ".ttf",
        sha256: createHash("sha256").update(readFileSync(host)).digest("hex"),
      });
    }
  return table;
}

export function resolveFont(family: string, style: string): ResolvedFont {
  const found = fontTable().get(family + "|" + style);
  requireThat(
    found,
    "OUT_OF_SCOPE",
    `Font ${family} (${style}) is not installed on this server.`,
    {
      available: [...fontTable().values()].map((f) => `${f.family} ${f.style}`),
    },
  );
  return found!;
}

/** bwrap arguments binding every resolved font file read-only at its sandbox path. */
export function fontBinds(): string[] {
  return [...fontTable().values()].flatMap((f) => [
    "--ro-bind-try",
    f.host,
    f.sandbox,
  ]);
}

/** Roots from which svg_emblem may read a file `path` (realpath, read-only). */
export function assetRoots(): string[] {
  const configured = envPathList(
    process.env.LLCAD_ASSET_PATHS ?? process.env.LLCAD_PATCH_PATHS,
  );
  const defaults = [
    "/root/projects",
    join(resolve(process.env.MATHFORGE_DATA ?? "data"), "uploads"),
  ];
  return (configured.length ? configured : defaults).flatMap((root) => {
    try {
      return [realpathSync(root)];
    } catch {
      return [];
    }
  });
}

const svgCache = new Map<
  string,
  { mtime: number; size: number; text: string; sha256: string; text_sha256: string }
>();
/** An SVG file named by path: inside the asset roots, *.svg, at most 1 MiB, UTF-8. */
export function readSvgAsset(path: string) {
  const inside = (candidate: string) =>
    assetRoots().some((root) => pathInside(candidate, root, sep));
  // Outside the roots nothing is looked at, not even whether the file exists.
  requireThat(
    inside(resolve(path)),
    "ACCESS_DENIED",
    "The SVG path is outside the permitted directories (LLCAD_ASSET_PATHS).",
    { allowed_roots: assetRoots() },
  );
  let real: string;
  try {
    real = realpathSync(path);
  } catch {
    requireThat(false, "INVALID_SCHEMA", `SVG file ${path} does not exist.`);
    throw new Error("unreachable");
  }
  requireThat(
    inside(real),
    "ACCESS_DENIED",
    "The SVG path is outside the permitted directories (LLCAD_ASSET_PATHS).",
    { allowed_roots: assetRoots() },
  );
  requireThat(/\.svg$/i.test(real), "INVALID_SCHEMA", "svg_emblem reads only *.svg files.");
  const info = statSync(real);
  requireThat(
    info.isFile() && info.size <= 1024 * 1024,
    "BUDGET_EXCEEDED",
    "The SVG file is too large (at most 1 MiB).",
  );
  const cached = svgCache.get(real);
  if (cached && cached.mtime === info.mtimeMs && cached.size === info.size)
    return cached;
  const bytes = readFileSync(real);
  const text = bytes.toString("utf8");
  const entry = {
    mtime: info.mtimeMs,
    size: info.size,
    text,
    /** Of the file bytes: what a `sha256` pin in the IR names. */
    sha256: createHash("sha256").update(bytes).digest("hex"),
    /** Of the UTF-8 text handed to the worker, which checks it before parsing. */
    text_sha256: createHash("sha256").update(text, "utf8").digest("hex"),
  };
  svgCache.set(real, entry);
  return entry;
}
