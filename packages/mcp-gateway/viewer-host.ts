/** Viewer lifecycle behind cad_viewer_open and cad_viewer_close.
 *
 *  SharedViewer: the HTTP service already serves the viewer; opening only mints a
 *  single-use login code. EmbeddedViewer: a stdio server has no HTTP listener, so the
 *  viewer is started on demand on the loopback interface and stopped again on request.
 *  Both keep the long-lived local key out of tool results. */
import { spawn } from "node:child_process";
import { once } from "node:events";
import { existsSync } from "node:fs";
import type { Server } from "node:http";
import { createServer } from "node:net";
import { delimiter, join } from "node:path";
import {
  ModelService,
  ViewerArguments,
  ViewerHost,
  ViewerState,
} from "../model-service/index.js";
import { createApp } from "./app.js";

const CODE_NOTE =
  "The URL carries a single-use login code that expires after five minutes; request a new one with cad_viewer_open.";

export function viewerURL(base: string, code: string, modelID?: string) {
  const fragment = new URLSearchParams({ code });
  if (modelID) fragment.set("model", modelID);
  return new URL("/#" + fragment.toString(), base).toString();
}

function commandExists(command: string) {
  return (process.env.PATH ?? "")
    .split(delimiter)
    .some((dir) => dir && existsSync(join(dir, command)));
}

/** Start the platform browser detached from the server process; never blocks a tool call. */
export function launchBrowser(url: string): {
  launched: boolean;
  note: string;
} {
  const platform = process.platform;
  if (
    platform === "linux" &&
    !process.env.DISPLAY &&
    !process.env.WAYLAND_DISPLAY
  )
    return {
      launched: false,
      note: "No graphical session was detected, so no browser was started; open the URL on this machine.",
    };
  const [command, args]: [string, string[]] =
    platform === "darwin"
      ? ["open", [url]]
      : platform === "win32"
        ? ["cmd", ["/c", "start", "", url]]
        : ["xdg-open", [url]];
  if (platform !== "win32" && !commandExists(command))
    return {
      launched: false,
      note: `${command} is not installed, so no browser was started; open the URL manually.`,
    };
  try {
    const child = spawn(command, args, { detached: true, stdio: "ignore" });
    child.on("error", () => {});
    child.unref();
    return {
      launched: true,
      note: `The default browser was asked to open the viewer.`,
    };
  } catch (error) {
    return {
      launched: false,
      note: `Starting a browser failed (${(error as Error).message}); open the URL manually.`,
    };
  }
}

function maybeLaunch(url: string, wanted: boolean) {
  return wanted
    ? launchBrowser(url)
    : {
        launched: false,
        note: "No browser was started because launch_browser is false.",
      };
}

export class SharedViewer implements ViewerHost {
  constructor(
    private readonly publicURL: string,
    private readonly issueCode: () => string,
  ) {}
  async open(args: ViewerArguments): Promise<ViewerState> {
    const url = viewerURL(this.publicURL, this.issueCode(), args.model_id);
    const browser = maybeLaunch(url, args.launch_browser);
    return {
      running: true,
      transport: "http",
      url,
      browser_launched: browser.launched,
      message: `The viewer is served by the running HTTP service. ${browser.note} ${CODE_NOTE}`,
    };
  }
  async close(): Promise<ViewerState> {
    return {
      running: true,
      transport: "http",
      url: null,
      browser_launched: false,
      message:
        "The viewer is part of the running HTTP service and stays available while the service runs; stop the service to close it.",
    };
  }
}

async function portIsFree(port: number) {
  return new Promise<boolean>((resolve) => {
    const probe = createServer();
    probe.once("error", () => resolve(false));
    probe.listen(port, "127.0.0.1", () => probe.close(() => resolve(true)));
  });
}

async function choosePort(preferred: number) {
  if (preferred > 0 && (await portIsFree(preferred))) return preferred;
  const probe = createServer();
  probe.listen(0, "127.0.0.1");
  await once(probe, "listening");
  const port = (probe.address() as { port: number }).port;
  await new Promise<void>((resolve) => probe.close(() => resolve()));
  return port;
}

export class EmbeddedViewer implements ViewerHost {
  private server: Server | null = null;
  private baseURL = "";
  private issueCode: (() => string) | null = null;
  constructor(
    private readonly service: ModelService,
    private readonly dataRoot: string,
    private readonly preferredPort = Number(
      process.env.MATHFORGE_VIEWER_PORT ?? 4310,
    ),
  ) {}
  private async start() {
    const port = await choosePort(this.preferredPort);
    const publicURL = `http://127.0.0.1:${port}`;
    const { app, issueBootstrapCode } = createApp(this.service, {
      mode: "local",
      publicURL,
      dataRoot: this.dataRoot,
    });
    const server = app.listen(port, "127.0.0.1");
    await once(server, "listening");
    this.server = server;
    this.baseURL = publicURL;
    this.issueCode = issueBootstrapCode;
  }
  async open(args: ViewerArguments): Promise<ViewerState> {
    const started = !this.server;
    if (!this.server) await this.start();
    const url = viewerURL(this.baseURL, this.issueCode!(), args.model_id);
    const browser = maybeLaunch(url, args.launch_browser);
    return {
      running: true,
      transport: "stdio",
      url,
      browser_launched: browser.launched,
      message: `${started ? "Started" : "Reusing"} the local viewer on ${this.baseURL} (loopback only). ${browser.note} ${CODE_NOTE}`,
    };
  }
  async close(): Promise<ViewerState> {
    const server = this.server;
    if (!server)
      return {
        running: false,
        transport: "stdio",
        url: null,
        browser_launched: false,
        message: "The viewer was not running.",
      };
    this.server = null;
    server.closeAllConnections();
    await new Promise<void>((resolve) => server.close(() => resolve()));
    return {
      running: false,
      transport: "stdio",
      url: null,
      browser_launched: false,
      message:
        "The local viewer has been stopped and its browser sessions were closed.",
    };
  }
  async stop() {
    if (this.server) await this.close();
  }
}
