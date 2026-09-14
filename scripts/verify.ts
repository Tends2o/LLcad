import { spawn } from "node:child_process";
import { mkdirSync, writeFileSync } from "node:fs";
import { REGISTRY_HASH } from "../packages/compiler/index.js";
import { IMPLEMENTATION_HASH } from "../packages/compiler/build.js";
mkdirSync("reports", { recursive: true });
const checks = [];
let failed = false;
for (const [name, args] of [
  ["versions", ["run", "versions:check"]],
  ["typecheck", ["run", "typecheck"]],
  ["tests", ["test"]],
  ["geometry", ["run", "test:geometry"]],
  ["mcp", ["run", "test:mcp"]],
  ["build", ["run", "build"]],
  ["browser", ["run", "test:browser"]],
] as const) {
  const start = Date.now();
  const result = await new Promise<{
    exit_code: number | null;
    output: string;
  }>((resolve, reject) => {
    const child = spawn("npm", [...args], {
      env: process.env,
      stdio: ["ignore", "pipe", "pipe"],
    });
    let output = "";
    child.stdout.on("data", (c) => {
      output += c;
      process.stdout.write(c);
    });
    child.stderr.on("data", (c) => {
      output += c;
      process.stderr.write(c);
    });
    child.on("error", reject);
    child.on("close", (exit_code) => resolve({ exit_code, output }));
  });
  writeFileSync(`reports/${name}.log`, result.output);
  checks.push({
    name,
    status: result.exit_code === 0 ? "passed" : "failed",
    duration_ms: Date.now() - start,
    exit_code: result.exit_code,
  });
  if (result.exit_code !== 0) {
    failed = true;
    break;
  }
}
writeFileSync(
  "reports/verification.json",
  JSON.stringify(
    {
      status: failed ? "failed" : "passed",
      created: new Date().toISOString(),
      registry_hash: REGISTRY_HASH,
      implementation_hash: IMPLEMENTATION_HASH,
      checks,
    },
    null,
    2,
  ) + "\n",
);
if (failed) process.exitCode = 1;
