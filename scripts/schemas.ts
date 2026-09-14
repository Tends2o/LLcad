import { writeFileSync, mkdirSync } from "node:fs";
import { z } from "zod";
import { ModelIR, Patch, ToolSchemas } from "../packages/semantic-ir/schema.js";
import { OPERATORS, REGISTRY_HASH } from "../packages/compiler/index.js";
import { OUTPUT_SCHEMA } from "../packages/mcp-gateway/tools.js";
mkdirSync("schemas", { recursive: true });
const schemas: Record<string, unknown> = {
  "model-ir-v1.schema.json": z.toJSONSchema(ModelIR),
  "parameter-patch-v1.schema.json": z.toJSONSchema(Patch),
  "tool-results-v1.schema.json": OUTPUT_SCHEMA,
};
for (const [name, schema] of Object.entries(ToolSchemas))
  schemas[name + ".schema.json"] = z.toJSONSchema(schema);
schemas["operator-contract.schema.json"] = {
  type: "object",
  additionalProperties: false,
  required: [
    "version",
    "params",
    "refs",
    "output",
    "required_validators",
    "engine",
  ],
  properties: {
    version: { type: "integer" },
    params: {
      type: "object",
      additionalProperties: {
        type: "object",
        required: ["dimension"],
        properties: {
          dimension: { enum: ["length", "angle", "scalar"] },
          min: { type: "number" },
          max: { type: "number" },
          optional: { type: "boolean" },
          integer: { type: "boolean" },
        },
        additionalProperties: false,
      },
    },
    refs: {
      type: "array",
      items: { type: "integer" },
      minItems: 2,
      maxItems: 2,
    },
    output: { type: "string" },
    required_validators: { type: "array", items: { type: "string" } },
    engine: { type: "string" },
  },
};
schemas["validation-result.schema.json"] = z.toJSONSchema(
  z.object({
    candidate_revision: z.string(),
    ir_hash: z.string().regex(/^[a-f0-9]{64}$/),
    geometry_digest: z.string(),
    registry_hash: z.string(),
    policy_hash: z.string(),
    engine_build: z.string(),
    profile: z.enum(["precision_cad", "render_surface"]),
    status: z.enum(["failed", "checks_passed_within_profile"]),
    digest: z.string().regex(/^[a-f0-9]{64}$/),
    checks: z.array(
      z.object({
        check_id: z.string(),
        target: z.string(),
        method: z.string(),
        guarantee: z.enum(["sampled", "bounded", "exact_for_declared_domain"]),
        coverage: z.string(),
        status: z.enum(["passed", "failed"]),
        measured: z.unknown(),
        error_bound: z.number().nullable(),
      }),
    ),
  }),
);
for (const [filename, schema] of Object.entries(schemas))
  writeFileSync(
    "schemas/" + filename,
    JSON.stringify(
      {
        $schema: "https://json-schema.org/draft/2020-12/schema",
        ...(schema as object),
      },
      null,
      2,
    ) + "\n",
  );
writeFileSync(
  "schemas/operator-registry.json",
  JSON.stringify({ hash: REGISTRY_HASH, operators: OPERATORS }, null, 2) + "\n",
);
console.log(
  `Generated ${Object.keys(schemas).length} schemas and operator registry.`,
);
