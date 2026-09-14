import { chromium } from "@playwright/test";
import assert from "node:assert/strict";
import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { ModelService } from "../packages/model-service/index.js";
import { createApp } from "../packages/mcp-gateway/app.js";
const dir = mkdtempSync(join(tmpdir(), "mathforge-browser-")),
  service = new ModelService(dir);
const url = "http://127.0.0.1:4311";
const token = "test-browser-token-" + "x".repeat(32);
const { app } = createApp(service, {
  mode: "local",
  publicURL: url,
  dataRoot: dir,
  localToken: token,
});
const server = app.listen(4311, "127.0.0.1");
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
const errors: string[] = [];
page.on("pageerror", (error) => errors.push(error.message));
try {
  await page.goto(url);
  await page.locator("#token").fill(token);
  await page.locator("#login-form button").click();
  await page.locator("#login-dialog").waitFor({ state: "hidden" });
  await page.locator("#start-demo").click();
  await page
    .locator("#detail-name")
    .filter({ hasText: "innere Dichtungsnut" })
    .waitFor({ timeout: 60000 });
  await page.locator("#busy").waitFor({ state: "hidden", timeout: 60000 });
  // The displayed body belongs to the final hole operation. Clicking its
  // central native face must resolve the original base instead of that output.
  const canvas = page.locator("#viewport canvas");
  const canvasBox = await canvas.boundingBox();
  assert.ok(canvasBox);
  await canvas.click({
    position: { x: canvasBox.width / 2, y: canvasBox.height / 2 },
  });
  await page
    .locator("#detail-name")
    .filter({ hasText: "Gehäuseboden" })
    .waitFor();
  assert.match(
    await page.locator("#detail-purpose").innerText(),
    /Herkunft geprüft/,
  );
  await page.locator('[data-feature="feat-groove-07"]').click();
  await page
    .locator("#detail-name")
    .filter({ hasText: "innere Dichtungsnut" })
    .waitFor();
  await page.getByRole("textbox", { name: "Tiefe", exact: true }).fill("0.82");
  await page.locator("#stage-edit").click();
  await page.locator("#step-candidate.done").waitFor({ timeout: 60000 });
  await page.locator("#busy").waitFor({ state: "hidden", timeout: 60000 });
  await page.locator("#validate").click();
  await page.locator("#step-validation.done").waitFor({ timeout: 60000 });
  mkdirSync("reports", { recursive: true });
  await page.screenshot({
    path: "reports/viewer-candidate.png",
    fullPage: true,
  });
  await page.locator("#commit").click();
  await page.locator("#step-commit.done").waitFor({ timeout: 60000 });
  assert.equal(
    await page
      .getByRole("textbox", { name: "Tiefe", exact: true })
      .inputValue(),
    "0.82",
  );
  assert.equal(
    await page
      .getByRole("textbox", { name: "Breite", exact: true })
      .isDisabled(),
    true,
  );
  await page.locator("#section").click();
  await page.locator("#wireframe").click();
  await page.locator("#wireframe").click();
  await page.locator("#section").click();
  await page.locator("#export").click();
  await page.locator("#downloads a").first().waitFor({ timeout: 60000 });
  await page.locator("#busy").waitFor({ state: "hidden", timeout: 60000 });
  await page.locator(".right").evaluate((node) => (node.scrollTop = 0));
  await page.screenshot({ path: "reports/viewer.png", fullPage: true });
  assert.deepEqual(errors, []);
  writeFileSync(
    "reports/browser.json",
    JSON.stringify(
      {
        status: "passed",
        browser: "Chromium",
        viewport: [1512, 982],
        checks: [
          "login",
          "fixture construction",
          "native face picking resolves the source feature",
          "groove edit",
          "validation",
          "commit",
          "protected width",
          "section",
          "wireframe",
          "STEP export",
        ],
        errors,
      },
      null,
      2,
    ) + "\n",
  );
  console.log("Browser workflow passed; screenshots saved in reports/.");
} catch (error) {
  mkdirSync("reports", { recursive: true });
  await page.screenshot({
    path: "reports/browser-failure.png",
    fullPage: true,
  });
  console.error({ errors, toast: await page.locator("#toast").textContent() });
  throw error;
} finally {
  await browser.close();
  server.close();
  await service.close();
  rmSync(dir, { recursive: true, force: true });
}
