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
    `Erwartete Dimension: ${dimension ?? "bekannt"}.`,
  );
  const value = new Decimal(q.value).mul(entry[0]).toNumber();
  requireThat(
    Number.isFinite(value) && Math.abs(value) <= 1e9,
    "BUDGET_EXCEEDED",
    "Wert außerhalb des numerischen Bereichs.",
  );
  return value;
}
export function equalQuantity(a: Quantity, b: Quantity): boolean {
  requireThat(
    units[a.unit][1] === units[b.unit][1],
    "UNIT_MISMATCH",
    "Die Größen haben unterschiedliche Dimensionen.",
  );
  return new Decimal(a.value)
    .mul(units[a.unit][0])
    .eq(new Decimal(b.value).mul(units[b.unit][0]));
}
export function parse<T>(
  schema: { safeParse: (x: unknown) => any },
  input: unknown,
): T {
  const result = schema.safeParse(input);
  requireThat(
    result.success,
    "INVALID_SCHEMA",
    "Eingabe entspricht nicht dem Schema.",
    result.success
      ? {}
      : {
          issues: result.error.issues
            .slice(0, 8)
            .map((x: any) => ({ path: x.path, code: x.code })),
        },
  );
  return result.data;
}
