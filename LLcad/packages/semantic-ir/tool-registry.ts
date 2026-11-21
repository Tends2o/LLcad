/**
 * Tool register. Deliberately outside the `buildFiles` (packages/compiler/build.ts):
 * schema.ts stays byte-identical so that the build hash, and with it all existing model
 * revisions, remain valid. Tools added later are registered here.
 * All consumers except packages/policy/index.ts import ToolSchemas,
 * ToolName, READ_TOOLS and inputJSONSchema from here; policy/index.ts is a build file and
 * uses the base type (access to it only through toolScope).
 */
import { DrawingPlanInput } from "../drawing/schema.js";
import {
  ToolSchemas as BaseToolSchemas,
  READ_TOOLS as BASE_READ_TOOLS,
  inputJSONSchema as baseInputJSONSchema,
  type ToolName as BaseToolName,
} from "./schema.js";
import { scopeFor } from "../policy/index.js";
import { toolInputJSONSchema } from "./json-schema.js";

// Order as before: cad_drawing_plan directly after cad_drawing_analyze.
export const ToolSchemas = Object.fromEntries(
  Object.entries(BaseToolSchemas).flatMap(([name, schema]) =>
    name === "cad_drawing_analyze"
      ? [[name, schema], ["cad_drawing_plan", DrawingPlanInput]]
      : [[name, schema]],
  ),
) as typeof BaseToolSchemas & { cad_drawing_plan: typeof DrawingPlanInput };
export type ToolName = keyof typeof ToolSchemas;
export function inputJSONSchema(tool: ToolName) {
  if (tool === "cad_drawing_plan")
    return { ...toolInputJSONSchema(DrawingPlanInput), type: "object" as const };
  return baseInputJSONSchema(tool as BaseToolName);
}
export const READ_TOOLS = new Set<ToolName>([...BASE_READ_TOOLS, "cad_drawing_plan"]);
/** Scopes of the register tools (not in schema.ts/policy). Add a new register tool here. */
const REGISTER_SCOPES: Record<Exclude<ToolName, BaseToolName>, string> = {
  cad_drawing_plan: "model:read",
};
/** Base tools go through scopeFor (policy, build file), register tools through the table;
 *  anything else is an error instead of a silent model:read. */
export function toolScope(tool: ToolName): string {
  if (Object.hasOwn(REGISTER_SCOPES, tool))
    return REGISTER_SCOPES[tool as keyof typeof REGISTER_SCOPES];
  if (Object.hasOwn(BaseToolSchemas, tool)) return scopeFor(tool as BaseToolName);
  throw new Error(`Unknown tool without scope: ${tool}`);
}
