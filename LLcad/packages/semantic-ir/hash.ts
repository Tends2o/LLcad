import { createHash, randomUUID } from "node:crypto";
export function canonical(value: unknown): string {
  if (Array.isArray(value)) return "[" + value.map(canonical).join(",") + "]";
  if (value !== null && typeof value === "object")
    return (
      "{" +
      Object.entries(value)
        .filter(([, v]) => v !== undefined)
        .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))
        .map(([k, v]) => JSON.stringify(k) + ":" + canonical(v))
        .join(",") +
      "}"
    );
  return JSON.stringify(value);
}
export const hash = (value: unknown) =>
  createHash("sha256").update(canonical(value)).digest("hex");
export const bytesHash = (value: Uint8Array | string) =>
  createHash("sha256").update(value).digest("hex");
export const id = (prefix: string) => `${prefix}-${randomUUID()}`;
