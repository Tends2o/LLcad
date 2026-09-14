import { resolve } from "node:path";
import { ModelService } from "../model-service/index.js";
import { createApp } from "./app.js";
import { requireThat } from "../semantic-ir/errors.js";
const port = Number(process.env.PORT ?? 4310),
  mode = process.env.MATHFORGE_AUTH === "oauth" ? "oauth" : "local";
const host = process.env.HOST ?? "127.0.0.1";
requireThat(
  mode !== "local" || ["127.0.0.1", "::1", "localhost"].includes(host),
  "AUTH_REQUIRED",
  "Lokaler Modus darf nur an Loopback lauschen.",
);
const dataRoot = resolve(process.env.MATHFORGE_DATA ?? "data");
const service = new ModelService(dataRoot);
const config = {
  mode,
  publicURL: process.env.MATHFORGE_PUBLIC_URL ?? `http://127.0.0.1:${port}`,
  dataRoot,
  issuer: process.env.MATHFORGE_ISSUER,
  jwksURL: process.env.MATHFORGE_JWKS_URL,
  audience: process.env.MATHFORGE_AUDIENCE,
} as const;
const { app } = createApp(service, config);
const server = app.listen(port, host, () => {
  console.log(`MathForge 3D: ${config.publicURL}`);
  if (mode === "local")
    console.log(`Lokaler Zugangsschlüssel: ${dataRoot}/local-token`);
});
let stopping = false;
async function stop() {
  if (stopping) return;
  stopping = true;
  server.close();
  await service.close();
  process.exit(0);
}
process.on("SIGTERM", stop);
process.on("SIGINT", stop);
