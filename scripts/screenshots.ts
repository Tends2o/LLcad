/** Capture documentation screenshots from a running local LLcad server.
 *
 *  npm run screenshots -- --url http://127.0.0.1:4310 --out docs/images \
 *    --shot hero:"Tipper-Steuerplatine":page --shot pcb:"Tipper-Steuerplatine":viewport \
 *    --shot assembly:"Tipper-System":viewport --shot section:"Tipper-Aktor":section
 *
 *  Each --shot is name:model name:mode. Modes: page (whole application), viewport (3D view
 *  only) and section (3D view with the section plane enabled). The local key is read from
 *  $MATHFORGE_DATA/local-token or --token-file. Nothing in the model is changed. */
import { chromium } from "@playwright/test";
import { mkdirSync, readFileSync } from "node:fs";
import { join, resolve } from "node:path";

const args = process.argv.slice(2);
const option = (name: string, fallback: string) => {
  const index = args.indexOf(name);
  return index >= 0 && args[index + 1] ? args[index + 1] : fallback;
};
const url = option("--url", "http://127.0.0.1:4310");
const out = resolve(option("--out", "docs/images"));
const tokenFile = option(
  "--token-file",
  join(process.env.MATHFORGE_DATA ?? "data", "local-token"),
);
const shots = args
  .flatMap((value, index) => (args[index - 1] === "--shot" ? [value] : []))
  .map((spec) => {
    const match = /^([^:]+):"?([^"]+?)"?:(page|viewport|section)$/.exec(spec);
    if (!match) throw new Error(`Unreadable --shot ${spec}`);
    return { name: match[1], model: match[2], mode: match[3] };
  });
if (!shots.length)
  throw new Error("Pass at least one --shot name:model:page|viewport|section");
const token = readFileSync(tokenFile, "utf8").trim();
mkdirSync(out, { recursive: true });
const browser = await chromium.launch({
  headless: true,
  args: [
    "--no-sandbox",
    "--use-gl=angle",
    "--use-angle=swiftshader",
    "--enable-unsafe-swiftshader",
  ],
});
const page = await browser.newPage({
  viewport: { width: 1512, height: 982 },
  deviceScaleFactor: 1,
});
try {
  await page.goto(url, { waitUntil: "domcontentloaded" });
  await page.locator("#model-select").waitFor({ state: "attached" });
  await page.waitForTimeout(800);
  if (await page.locator("#login-dialog").isVisible()) {
    await page.locator("#token").fill(token);
    await page.locator("#login-form button").click();
    await page.locator("#login-dialog").waitFor({ state: "hidden" });
  }
  await page.locator("#busy").waitFor({ state: "hidden", timeout: 90000 });
  for (const shot of shots) {
    await page
      .locator("#model-select")
      .selectOption({ label: shot.model }, { timeout: 30000 });
    await page.locator("#busy").waitFor({ state: "hidden", timeout: 120000 });
    await page.locator("#overlay").selectOption("parts");
    await page.locator("#fit").click();
    if (shot.mode === "section") await page.locator("#section").click();
    await page.waitForTimeout(1500);
    const path = join(out, shot.name + ".png");
    if (shot.mode === "page") await page.screenshot({ path });
    else await page.locator("#viewport").screenshot({ path });
    if (shot.mode === "section") await page.locator("#section").click();
    console.log("written", path);
  }
} finally {
  await browser.close();
}
