/** Host differences between the Linux deployment and Windows in one place. */
import { delimiter, join } from "node:path";

export const IS_WINDOWS = process.platform === "win32";

/** The project's virtual-environment interpreter (bin/python, Scripts\python.exe on Windows). */
export function venvPython(root: string) {
  return IS_WINDOWS
    ? join(root, ".venv", "Scripts", "python.exe")
    : join(root, ".venv/bin/python");
}

/** Interpreter for the image and review helpers (LLCAD_IMAGE_PYTHON overrides). */
export function imagePython(root: string) {
  return process.env.LLCAD_IMAGE_PYTHON ?? venvPython(root);
}

/** A folder list from the environment: separated by the platform delimiter (":" on
 *  Linux, ";" on Windows, where ":" belongs to drive letters), with optional extra
 *  separators such as ",". */
export function envPathList(value: string | undefined, extra = "") {
  if (!value) return [];
  const separators = new Set([delimiter, ...extra]);
  const pattern = new RegExp(
    "[" + [...separators].map((c) => "\\" + c).join("") + "]",
  );
  return value
    .split(pattern)
    .map((part) => part.trim())
    .filter(Boolean);
}

/** Whether `candidate` is `root` or inside it; case-insensitive on Windows. */
export function pathInside(candidate: string, root: string, sep: string) {
  const [c, r] = IS_WINDOWS
    ? [candidate.toLowerCase(), root.toLowerCase()]
    : [candidate, root];
  return c === r || c.startsWith(r.endsWith(sep) ? r : r + sep);
}
