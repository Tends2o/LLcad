import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js";
import {
  CallToolRequestSchema,
  ListToolsRequestSchema,
  ReadResourceRequestSchema,
  ListResourceTemplatesRequestSchema,
  ListResourcesRequestSchema,
} from "@modelcontextprotocol/sdk/types.js";
import { Request, Response } from "express";
import { ModelService } from "../../model-service/index.js";
import { Principal } from "../../policy/index.js";
import { ToolName } from "../../semantic-ir/schema.js";
import { LEGACY_VERSIONS } from "../versions.js";
import {
  SERVER_INFO,
  INSTRUCTIONS,
  toolDefinitions,
  toolResult,
  resourceTemplates,
} from "../tools.js";
export function createToolServer(service: ModelService, p: Principal) {
  const server = new Server(SERVER_INFO, {
    capabilities: { tools: {}, resources: {} },
    instructions: INSTRUCTIONS,
  });
  registerTools(server, service, p);
  return server;
}

function registerTools(server: Server, service: ModelService, p: Principal) {
  server.setRequestHandler(ListToolsRequestSchema, async () => ({
    tools: toolDefinitions(),
  }));
  server.setRequestHandler(CallToolRequestSchema, async (r) =>
    toolResult(
      await service.call(
        p,
        r.params.name as ToolName,
        r.params.arguments ?? {},
      ),
    ),
  );
  server.setRequestHandler(ListResourcesRequestSchema, async () => ({
    resources: [],
  }));
  server.setRequestHandler(ListResourceTemplatesRequestSchema, async () => ({
    resourceTemplates,
  }));
  server.setRequestHandler(ReadResourceRequestSchema, async (r) => ({
    contents: [
      {
        uri: r.params.uri,
        mimeType: "application/json",
        text: JSON.stringify(service.resource(p, r.params.uri)),
      },
    ],
  }));
}

export async function legacy(
  service: ModelService,
  p: Principal,
  req: Request,
  res: Response,
) {
  const server = createToolServer(service, p);
  const transport = new StreamableHTTPServerTransport({
    sessionIdGenerator: undefined,
    enableJsonResponse: true,
  });
  await server.connect(transport);
  res.on("close", () => void server.close());
  // Offer our latest tested version when the client proposes another one.
  // Leave malformed requests untouched for the SDK's schema validation.
  const requested = req.body?.params?.protocolVersion;
  const body =
    req.body?.method === "initialize" &&
    typeof requested === "string" &&
    !LEGACY_VERSIONS.includes(requested)
      ? {
          ...req.body,
          params: { ...req.body.params, protocolVersion: LEGACY_VERSIONS[0] },
        }
      : req.body;
  await transport.handleRequest(req, res, body);
}
