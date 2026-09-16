#!/usr/bin/env node
// Start the stdio MCP server from any working directory (npm link / global install).
import { existsSync } from "node:fs";
import { fileURLToPath } from "node:url";
const root = fileURLToPath(new URL("..", import.meta.url));
process.chdir(root);
if (!existsSync("dist/packages/mcp-gateway/stdio.js")) {
  console.error("LLcad is not built yet. Run `npm run setup` (or `npm run build`) in " + root);
  process.exit(1);
}
await import("../dist/packages/mcp-gateway/stdio.js");
