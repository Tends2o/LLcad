import { Request, Response } from "express";
import { z } from "zod";
import { ModelService } from "../../model-service/index.js";
import { Principal } from "../../policy/index.js";
import { ToolName } from "../../semantic-ir/schema.js";
import {
  SERVER_INFO,
  INSTRUCTIONS,
  toolDefinitions,
  toolResult,
  resourceTemplates,
} from "../tools.js";
import { VERSIONS } from "../versions.js";
const Meta = z
  .object({
    "io.modelcontextprotocol/protocolVersion": z.string(),
    "io.modelcontextprotocol/clientCapabilities": z.record(
      z.string(),
      z.unknown(),
    ),
    "io.modelcontextprotocol/clientInfo": z
      .object({ name: z.string(), version: z.string() })
      .optional(),
  })
  .passthrough();
const RequestSchema = z
  .object({
    jsonrpc: z.literal("2.0"),
    id: z.union([z.string(), z.int()]),
    method: z.string(),
    params: z.object({ _meta: Meta }).passthrough(),
  })
  .strict();
function decoded(value: string | undefined) {
  if (!value) return value;
  if (value.startsWith("=?base64?") && value.endsWith("?=")) {
    const b64 = value.slice(9, -2);
    if (
      !/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(
        b64,
      )
    )
      return undefined;
    return new TextDecoder("utf-8", { fatal: true }).decode(
      Buffer.from(b64, "base64"),
    );
  }
  return /^[\x20-\x7e]+$/.test(value) && value.trim() === value
    ? value
    : undefined;
}
export function modern(
  service: ModelService,
  p: Principal,
  req: Request,
  res: Response,
) {
  const input = req.body,
    requestID =
      typeof input?.id === "string" || Number.isSafeInteger(input?.id)
        ? input.id
        : undefined;
  const error = (
    status: number,
    code: number,
    message: string,
    data?: unknown,
  ) =>
    res.status(status).json({
      jsonrpc: "2.0",
      ...(requestID !== undefined ? { id: requestID } : {}),
      error: { code, message, ...(data ? { data } : {}) },
    });
  const version = req.header("mcp-protocol-version");
  if (!version)
    return error(400, -32020, "MCP-Protocol-Version header required");
  if (!VERSIONS.includes(version))
    return error(400, -32022, "Unsupported protocol version", {
      requested: version,
      supported: VERSIONS,
    });
  if (
    version !==
      input?.params?._meta?.["io.modelcontextprotocol/protocolVersion"] ||
    req.header("mcp-method") !== input?.method
  )
    return error(400, -32020, "Header mismatch");
  const parsed = RequestSchema.safeParse(input);
  if (!parsed.success)
    return error(
      400,
      -32602,
      "Invalid per-request metadata or JSON-RPC request",
    );
  if (!req.accepts("application/json"))
    return error(406, -32600, "Accept application/json required");
  const params = input.params;
  if (["tools/call", "resources/read", "prompts/get"].includes(input.method)) {
    let name;
    try {
      name = decoded(req.header("mcp-name"));
    } catch {
      return error(400, -32020, "Malformed name encoding");
    }
    if (name !== (params.name ?? params.uri) || name === undefined)
      return error(400, -32020, "Mcp-Name header mismatch");
  }
  let result: any;
  try {
    switch (input.method) {
      case "server/discover":
        result = {
          supportedVersions: VERSIONS,
          capabilities: { tools: {}, resources: {} },
          instructions: INSTRUCTIONS,
          ttlMs: 300000,
          cacheScope: "private",
        };
        break;
      case "ping":
        result = {};
        break;
      case "tools/list":
        if (params.cursor) return error(400, -32602, "Unknown cursor");
        result = {
          tools: toolDefinitions(),
          ttlMs: 300000,
          cacheScope: "private",
        };
        break;
      case "tools/call":
        if (typeof params.name !== "string")
          return error(400, -32602, "Tool name required");
        result = toolResult(
          service.call(p, params.name as ToolName, params.arguments ?? {}),
        );
        break;
      case "resources/list":
        result = { resources: [], ttlMs: 0, cacheScope: "private" };
        break;
      case "resources/templates/list":
        result = { resourceTemplates, ttlMs: 300000, cacheScope: "private" };
        break;
      case "resources/read":
        if (typeof params.uri !== "string")
          return error(400, -32602, "Resource URI required");
        result = {
          contents: [
            {
              uri: params.uri,
              mimeType: "application/json",
              text: JSON.stringify(service.resource(p, params.uri)),
            },
          ],
        };
        break;
      default:
        return error(404, -32601, "Method not found");
    }
    return res.json({
      jsonrpc: "2.0",
      id: input.id,
      result: {
        resultType: "complete",
        ...result,
        _meta: { "io.modelcontextprotocol/serverInfo": SERVER_INFO },
      },
    });
  } catch {
    return error(400, -32602, "Resource or request not accessible");
  }
}
