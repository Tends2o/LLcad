import { resolve } from "node:path";
import { ModelService } from "../model-service/index.js";
import { createApp } from "./app.js";
import { requireThat } from "../semantic-ir/errors.js";
import { SharedViewer } from "./viewer-host.js";
const port = Number(process.env.PORT ?? 4310),
  mode = process.env.MATHFORGE_AUTH === "oauth" ? "oauth" : "local";
const host = process.env.HOST ?? "127.0.0.1";
requireThat(
  mode !== "local" || ["127.0.0.1", "::1", "localhost"].includes(host),
  "AUTH_REQUIRED",
  "Local mode may listen only on loopback.",
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
const { app, issueBootstrapCode } = createApp(service, config);
service.viewer = new SharedViewer(config.publicURL, issueBootstrapCode);
const server = app.listen(port, host, () => {
  console.log(`MathForge 3D: ${config.publicURL}`);
  if (mode === "local")
    console.log(`Local access key: ${dataRoot}/local-token`);
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

/** On-demand operation (the Windows stdio bridge starts the service when a client
 *  needs it): after LLCAD_IDLE_EXIT_MINUTES without any request and without work in
 *  progress the service ends itself, so an idle machine runs nothing. A viewer that
 *  is being used keeps it alive with its requests; an untouched viewer tab does not.
 *  0 or unset: run until stopped. */
const idleMinutes = Number(process.env.LLCAD_IDLE_EXIT_MINUTES ?? "0");
if (Number.isFinite(idleMinutes) && idleMinutes > 0) {
  let inFlight = 0,
    lastActivity = Date.now();
  server.on("request", (_req, res) => {
    inFlight++;
    lastActivity = Date.now();
    res.once("close", () => {
      inFlight--;
      lastActivity = Date.now();
    });
  });
  const idleCheck = setInterval(
    () => {
      if (
        inFlight === 0 &&
        Date.now() - lastActivity >= idleMinutes * 60_000 &&
        !service.busy()
      ) {
        clearInterval(idleCheck);
        console.log(`Idle for ${idleMinutes} min; stopping.`);
        void stop();
      }
    },
    Math.min(30_000, idleMinutes * 60_000),
  );
  idleCheck.unref();
}
