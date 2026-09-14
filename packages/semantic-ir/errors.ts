export class CadError extends Error {
  constructor(
    public code: string,
    message: string,
    public details: Record<string, unknown> = {},
  ) {
    super(message);
  }
}
export function requireThat(
  condition: unknown,
  code: string,
  message: string,
  details = {},
): asserts condition {
  if (!condition) throw new CadError(code, message, details);
}
export function safeError(error: unknown) {
  return error instanceof CadError
    ? { code: error.code, message: error.message, details: error.details }
    : {
        code: "INTERNAL_ERROR",
        message: "Die Operation konnte nicht abgeschlossen werden.",
        details: {},
      };
}
