import { Decimal } from "decimal.js";
import { Quantity } from "./schema.js";
import { requireThat } from "./errors.js";
Decimal.set({ precision: 60 });
const units = {
  mm: [1, "length"],
  m: [1000, "length"],
  um: [0.001, "length"],
  rad: [1, "angle"],
  deg: [Math.PI / 180, "angle"],
  "1": [1, "scalar"],
} as const;
export function quantity(q: Quantity, dimension?: string): number {
  const entry = units[q.unit];
  requireThat(
    entry && (!dimension || dimension === entry[1]),
    "UNIT_MISMATCH",
    `Expected dimension: ${dimension ?? "known"}.`,
  );
  const value = new Decimal(q.value).mul(entry[0]).toNumber();
  requireThat(
    Number.isFinite(value) && Math.abs(value) <= 1e9,
    "BUDGET_EXCEEDED",
    "Value outside the numeric range.",
  );
  return value;
}
export function equalQuantity(a: Quantity, b: Quantity): boolean {
  requireThat(
    units[a.unit][1] === units[b.unit][1],
    "UNIT_MISMATCH",
    "The quantities have different dimensions.",
  );
  return new Decimal(a.value)
    .mul(units[a.unit][0])
    .eq(new Decimal(b.value).mul(units[b.unit][0]));
}

/** At most this many schema issues are reported per call: enough to fix a whole
 *  patch in one retry, bounded so an unrelated document cannot flood the answer. */
export const MAX_REPORTED_ISSUES = 32;

/**
 * Strict parse with an actionable INVALID_SCHEMA payload. Every issue keeps zod's
 * message and the facts needed to fix it without another round trip: expected and
 * received type, the allowed keys next to unrecognized ones, the valid options of a
 * discriminator or enum, numeric/length limits and formats. Unions are resolved to
 * their closest alternative instead of an opaque "invalid_union". Rejected values
 * themselves are never echoed (only their JSON type).
 */
export function parse<T>(
  schema: { safeParse: (x: unknown) => any },
  input: unknown,
): T {
  const result = schema.safeParse(input);
  if (!result.success) {
    const issues = schemaIssues(schema, input, result.error.issues);
    const first = issues[0];
    requireThat(
      false,
      "INVALID_SCHEMA",
      "Input does not match the schema." +
        (first
          ? ` ${first.path.length ? first.path.join(".") : "(root)"}: ${first.message}` +
            (issues.length > 1 ? ` (+${issues.length - 1}more, details.issues)` : "")
          : ""),
      {
        issue_count: issues.length,
        issues: issues.slice(0, MAX_REPORTED_ISSUES),
      },
    );
  }
  return result.data;
}

type Z = any;
const DECIMAL_SOURCE = "^-?(0|[1-9][0-9]*)(\\.[0-9]+)?$";
const WRAPPERS = new Set([
  "optional",
  "nullable",
  "default",
  "prefault",
  "catch",
  "readonly",
  "nonoptional",
  "success",
]);
/** The schema a value is actually checked against: wrappers, lazies and pipes removed
 *  (a preprocess pipe is checked by its output schema, a transform by its input). */
function unwrap(schema: Z): Z {
  let s = schema;
  for (let i = 0; i < 32 && s?._zod?.def; i++) {
    const def = s._zod.def;
    if (WRAPPERS.has(def.type)) s = def.innerType;
    else if (def.type === "lazy") s = def.getter();
    else if (def.type === "pipe")
      s = def.in?._zod?.def?.type === "transform" ? def.out : def.in;
    else return s;
  }
  return s;
}
function isDecimalSchema(schema: Z) {
  const def = unwrap(schema)?._zod?.def;
  return (
    def?.type === "string" &&
    (def.checks ?? []).some(
      (c: any) => c?._zod?.def?.pattern?.source === DECIMAL_SOURCE,
    )
  );
}
function canonical(value: number) {
  if (!Number.isFinite(value)) return value;
  const text = new Decimal(value).toFixed();
  return text === "-0" ? "0" : text;
}
function objectShape(s: Z): Record<string, Z> | null {
  const def = s?._zod?.def;
  if (def?.type !== "object") return null;
  return s.shape ?? def.shape ?? null;
}
/** Options of a discriminated union that accept this discriminator value. */
function discriminated(def: any, value: any): Z[] | null {
  if (!def.discriminator || !value || typeof value !== "object") return null;
  const key = value[def.discriminator];
  return def.options.filter((o: Z) => {
    const values = o?._zod?.propValues?.[def.discriminator];
    return values instanceof Set ? values.has(key) : false;
  });
}

/**
 * Plain JSON numbers are accepted wherever the schema expects a decimal string: they
 * become the canonical decimal (no exponent), exactly what a caller would have typed.
 * Guided by the schema, so genuine integer/number fields keep their numbers.
 */
export function normaliseDecimals(schema: Z, value: unknown, depth = 0): unknown {
  if (depth > 96 || value === null || value === undefined) return value;
  const s = unwrap(schema);
  const def = s?._zod?.def;
  if (!def) return value;
  if (def.type === "union") {
    if (typeof value === "number")
      return def.options.some(isDecimalSchema) &&
        !def.options.some((o: Z) => unwrap(o)?._zod?.def?.type === "number")
        ? canonical(value)
        : value;
    if (typeof value !== "object") return value;
    const matching = discriminated(def, value);
    if (matching)
      return matching.length === 1
        ? normaliseDecimals(matching[0], value, depth + 1)
        : value;
    for (const option of def.options) {
      const candidate = normaliseDecimals(option, value, depth + 1);
      if (option.safeParse(candidate).success) return candidate;
    }
    return value;
  }
  if (typeof value === "number")
    return isDecimalSchema(s) ? canonical(value) : value;
  if (Array.isArray(value)) {
    if (def.type === "array")
      return value.map((v) => normaliseDecimals(def.element, v, depth + 1));
    if (def.type === "tuple")
      return value.map((v, i) =>
        i < def.items.length
          ? normaliseDecimals(def.items[i], v, depth + 1)
          : def.rest
            ? normaliseDecimals(def.rest, v, depth + 1)
            : v,
      );
    return value;
  }
  if (typeof value !== "object") return value;
  const shape = objectShape(s);
  if (shape)
    return Object.fromEntries(
      Object.entries(value as Record<string, unknown>).map(([k, v]) => [
        k,
        Object.hasOwn(shape, k) ? normaliseDecimals(shape[k], v, depth + 1) : v,
      ]),
    );
  if (def.type === "record")
    return Object.fromEntries(
      Object.entries(value as Record<string, unknown>).map(([k, v]) => [
        k,
        normaliseDecimals(def.valueType, v, depth + 1),
      ]),
    );
  if (def.type === "intersection")
    return normaliseDecimals(
      def.right,
      normaliseDecimals(def.left, value, depth + 1),
      depth + 1,
    );
  return value;
}

/** The schema node that checks `path` of `input` (discriminators resolved by the input). */
export function schemaAt(root: Z, path: PropertyKey[], input: unknown): Z | null {
  let s: Z = root,
    value: any = input;
  for (const key of path) {
    s = unwrap(s);
    let def = s?._zod?.def;
    if (def?.type === "union") {
      const matching = discriminated(def, value);
      const options: Z[] = matching?.length
        ? matching
        : def.options.filter((o: Z) => {
            const shape = objectShape(unwrap(o));
            return shape ? Object.hasOwn(shape, String(key)) : false;
          });
      if (!options.length) return null;
      s = unwrap(options[0]);
      def = s?._zod?.def;
    }
    if (!def) return null;
    const shape = objectShape(s);
    if (shape) s = shape[String(key)];
    else if (def.type === "array") s = def.element;
    else if (def.type === "tuple")
      s = def.items[Number(key)] ?? def.rest ?? null;
    else if (def.type === "record") s = def.valueType;
    else return null;
    if (!s) return null;
    value = value?.[key as any];
  }
  return s;
}
function allowedKeys(root: Z, path: PropertyKey[], input: unknown) {
  const node = unwrap(schemaAt(root, path, input));
  const shape = objectShape(node);
  if (shape) return Object.keys(shape);
  const def = node?._zod?.def;
  if (def?.type === "union") {
    const value = valueAt(input, path);
    const matching = discriminated(def, value);
    const shapes = (matching?.length ? matching : def.options)
      .map((o: Z) => objectShape(unwrap(o)))
      .filter(Boolean);
    if (shapes.length === 1) return Object.keys(shapes[0]);
  }
  return undefined;
}
function valueAt(input: unknown, path: PropertyKey[]) {
  let value: any = input;
  for (const key of path) {
    if (value === null || typeof value !== "object") return undefined;
    value = value[key as any];
  }
  return value;
}
/** Closest option by edit distance (typos like "cylindre"), when it is close enough. */
function nearest(given: string, options: string[]) {
  const distance = (a: string, b: string) => {
    const row = Array.from({ length: b.length + 1 }, (_, j) => j);
    for (let i = 1; i <= a.length; i++) {
      let previous = row[0];
      row[0] = i;
      for (let j = 1; j <= b.length; j++) {
        const current = row[j];
        row[j] = Math.min(row[j] + 1, row[j - 1] + 1, previous + (a[i - 1] === b[j - 1] ? 0 : 1));
        previous = current;
      }
    }
    return row[b.length];
  };
  let best: string | null = null,
    score = Infinity;
  for (const option of options) {
    const d = distance(given.toLowerCase(), option.toLowerCase());
    if (d < score) {
      score = d;
      best = option;
    }
  }
  return best && score <= Math.max(2, Math.floor(given.length / 3)) ? best : null;
}
function jsonType(value: unknown) {
  if (value === undefined) return "missing";
  if (value === null) return "null";
  if (Array.isArray(value)) return "array";
  return typeof value;
}
export type SchemaIssue = {
  path: (string | number)[];
  code: string;
  message: string;
  [key: string]: unknown;
};
/** Flattened, fix-oriented issue list (see parse). Exported for tools that validate
 *  a sub-document themselves (e.g. a patch read from patch_ref). */
export function schemaIssues(
  root: Z,
  input: unknown,
  issues: any[],
  prefix: (string | number)[] = [],
): SchemaIssue[] {
  const out: SchemaIssue[] = [];
  const visit = (list: any[], base: (string | number)[], depth: number) => {
    for (const issue of list) {
      const path = [...base, ...(issue.path ?? [])].map((k) =>
        typeof k === "number" ? k : String(k),
      );
      if (
        issue.code === "invalid_union" &&
        Array.isArray(issue.errors) &&
        issue.errors.length &&
        depth < 8
      ) {
        // Each alternative's issues, flattened; the closest one is reported: an
        // alternative whose type already matches beats one that fails at the root.
        const branches = issue.errors.map((branch: any[]) => {
          const collected: SchemaIssue[] = [];
          const inner = (l: any[], b: (string | number)[]) => {
            for (const i of l) {
              const p = [...b, ...(i.path ?? [])];
              if (i.code === "invalid_union" && i.errors?.length) {
                const best = i.errors
                  .map((e: any[]) => e.map((x) => ({ ...x, path: [...p, ...(x.path ?? [])] })))
                  .sort((a: any[], c: any[]) => a.length - c.length)[0];
                collected.push(...best);
              } else collected.push({ ...i, path: p });
            }
          };
          inner(branch, path);
          return collected;
        });
        const rootMismatch = (b: SchemaIssue[]) =>
          b.some(
            (x) =>
              x.code === "invalid_type" &&
              x.path.length === path.length,
          );
        const plausible = branches.filter((b: SchemaIssue[]) => !rootMismatch(b));
        const pick = (plausible.length ? plausible : branches).sort(
          (a: SchemaIssue[], b: SchemaIssue[]) => a.length - b.length,
        )[0];
        for (const x of pick)
          out.push(
            describe(root, input, {
              ...x,
              note:
                x.note ??
                `closest of ${issue.errors.length} alternatives at ${path.join(".") || "(root)"}`,
            }),
          );
        continue;
      }
      out.push(describe(root, input, { ...issue, path }));
    }
  };
  visit(issues, prefix, 0);
  // Duplicates arise when several alternatives report the same fact.
  const seen = new Set<string>();
  return out.filter((i) => {
    const key = i.code + ":" + i.path.join(".") + ":" + i.message;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}
function describe(root: Z, input: unknown, issue: any): SchemaIssue {
  const path: (string | number)[] = (issue.path ?? []).map((k: any) =>
    typeof k === "number" ? k : String(k),
  );
  const d: SchemaIssue = {
    path,
    code: String(issue.code),
    message: String(issue.message ?? ""),
  };
  if (issue.code === "invalid_type") {
    d.expected = issue.expected;
    d.received = jsonType(valueAt(input, path));
  }
  if (issue.code === "unrecognized_keys") {
    d.keys = issue.keys;
    const allowed = allowedKeys(root, path, input);
    if (allowed) d.allowed_keys = allowed;
  }
  if (issue.code === "invalid_union" && issue.options) {
    d.options = issue.options.slice(0, 96).map(String);
    const given = valueAt(input, path);
    // zod's message repeats every option; the options are listed once, below.
    d.message = `unknown ${issue.discriminator ?? "variant"} ${JSON.stringify(given ?? null)}`;
    const near = typeof given === "string" ? nearest(given, d.options as string[]) : null;
    if (near) d.did_you_mean = near;
  }
  if (issue.discriminator) d.discriminator = issue.discriminator;
  if (issue.code === "invalid_value" && issue.values)
    d.options = issue.values.slice(0, 96).map(String);
  if (issue.code === "too_big" || issue.code === "too_small") {
    if (issue.maximum !== undefined) d.maximum = Number(issue.maximum);
    if (issue.minimum !== undefined) d.minimum = Number(issue.minimum);
    if (issue.inclusive !== undefined) d.inclusive = issue.inclusive;
    if (issue.origin) d.origin = issue.origin;
    const value = valueAt(input, path);
    if (Array.isArray(value)) d.received_length = value.length;
    else if (typeof value === "string") d.received_length = value.length;
  }
  if (issue.code === "invalid_format") {
    d.format = issue.format;
    if (issue.pattern) d.pattern = String(issue.pattern);
    d.received = jsonType(valueAt(input, path));
    if (
      issue.pattern &&
      String(issue.pattern).includes("(0|[1-9][0-9]*)") &&
      typeof valueAt(input, path) === "string"
    )
      d.hint =
        "decimal string without exponent, leading '+', leading zeros or a bare '.5' (e.g. \"0.5\", \"-12\", \"3.25\"); plain JSON numbers are accepted too";
  }
  if (issue.note) d.note = issue.note;
  return d;
}
