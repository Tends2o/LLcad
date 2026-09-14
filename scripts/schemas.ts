import { writeFileSync, mkdirSync } from "node:fs";
import { z } from "zod";
import {
  ModelIR,
  Patch,
  ToolSchemas,
  inputJSONSchema,
} from "../packages/semantic-ir/schema.js";
import { OPERATORS, REGISTRY_HASH } from "../packages/compiler/index.js";
import {
  FailureResponse,
  ValidationReport,
  outputJSONSchema,
} from "../packages/semantic-ir/results.js";
mkdirSync("schemas", { recursive: true });
const schemas: Record<string, unknown> = {
  "model-ir-v1.schema.json": z.toJSONSchema(ModelIR),
  "parameter-patch-v1.schema.json": z.toJSONSchema(Patch),
  "tool-results-v1.schema.json": {
    type: "object",
    anyOf: Object.keys(ToolSchemas).map((name) => ({
      $ref: name + ".result.schema.json",
    })),
  },
  "tool-error-v1.schema.json": z.toJSONSchema(FailureResponse, {
    reused: "ref",
  }),
};
for (const [name, schema] of Object.entries(ToolSchemas)) {
  schemas[name + ".schema.json"] = inputJSONSchema(
    name as keyof typeof ToolSchemas,
  );
  schemas[name + ".result.schema.json"] = outputJSONSchema(
    name as keyof typeof ToolSchemas,
  );
}
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
schemas["validation-result.schema.json"] = z.toJSONSchema(ValidationReport, {
  reused: "ref",
});
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
