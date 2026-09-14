/** Local host transport: identity comes from the private OS process boundary. */
import { resolve } from "node:path";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { ModelService } from "../model-service/index.js";
import { SCOPES } from "../policy/index.js";
import { createToolServer } from "./adapters/legacy_tested.js";

const service = new ModelService(resolve(process.env.MATHFORGE_DATA ?? "data"));
const server = createToolServer(service, {
  tenant: "local",
  user: "local-user",
  scopes: SCOPES,
});
let stopping = false;
async function stop() {
  if (stopping) return;
  stopping = true;
  await server.close();
  await service.close();
}
server.onclose = () => {
  void stop();
};
process.once("SIGTERM", () => {
  void stop();
});
process.once("SIGINT", () => {
  void stop();
});
process.stdin.once("end", () => {
  void stop();
});
await server.connect(new StdioServerTransport());
