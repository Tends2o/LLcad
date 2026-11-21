import { writeFileSync } from "node:fs";
import { join } from "node:path";

const escape = (value: unknown) =>
  String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
const names: Record<string, string> = {
  "wrong-label": "Wrong label",
  control: "Unchanged control",
  "wrong-width": "Wrong housing width",
  "missing-wheel": "Missing wheel",
  combined: "Three combined errors",
  "unknown-extra": "Unexpected extra part",
};
export function writeHtmlReport(folder: string, result: any) {
  const cases: any[] = result.cases ?? [];
  const image = (path: string, title: string) =>
    `<figure><a href="${escape(path)}"><img loading="lazy" src="${escape(path)}" alt="${escape(title)}"></a><figcaption>${escape(title)}</figcaption></figure>`;
  const sections = cases
    .map((c) => {
      const base = `sample-${Number(c.sample)}`;
      const checks = c.report.inspections.length;
      const accepted = c.report.status === "verified_within_contract";
      return `<article><h2>${escape(names[c.scenario] ?? c.scenario)} <span class="${c.pass ? "pass" : "fail"}">${c.pass ? "Test passed" : "Test failed"}</span></h2>
      <p>${accepted ? (c.report.rounds ? "Corrected automatically and checked again." : "Checked; accepted without change.") : "Not verified. The original state is kept."}
      ${Number(c.report.rounds)} correction attempt(s) · ${(c.total_elapsed_ms / 1000).toFixed(2)} s including data import.</p>
      <div class="images">${image("reference.png", "Fixed reference")}${image(base + "/check-0.png", "Before correction")}${image(base + `/check-${Math.max(0, checks - 1)}.png`, accepted ? "Verified result" : "Rejected state")}${image(base + "/check-0.diff.png", "Deviations before correction (red)")}</div>
      <details><summary>Findings and evidence</summary><pre>${escape(
        JSON.stringify(
          {
            status: c.report.status,
            reason: c.report.reason,
            first_observation: c.report.inspections[0]?.issues,
            independent_oracle: c.independent_oracle,
          },
          null,
          2,
        ),
      )}</pre><a href="${base}/result.json">Full result</a> · <a href="${base}/final.ir.json">Parametric model</a></details></article>`;
    })
    .join("\n");
  writeFileSync(
    join(folder, "report.html"),
    `<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>LLcad · Automatic reference check</title>
    <style>body{font:16px/1.55 system-ui,sans-serif;background:#f4f6f8;color:#172331;margin:0}main{max-width:1200px;margin:auto;padding:32px 24px}h1{font-size:32px;line-height:1.15}h2{font-size:21px}article{background:white;border:1px solid #dce3e9;border-radius:12px;padding:20px;margin:24px 0}a{color:#135b9c}figure{margin:0}img{width:100%;max-height:340px;object-fit:contain;background:#fff;border:1px solid #eee}figcaption{font-size:13px;color:#52616e} .images{display:grid;grid-template-columns:repeat(4,1fr);gap:16px}.pass,.fail{font-size:13px;white-space:nowrap;padding:5px 10px;border-radius:16px}.pass{background:#d7f3e2;color:#12623a}.fail{background:#ffe0e0;color:#a01919}pre{white-space:pre-wrap;font-size:12px}details{margin-top:18px}.scope{border-left:4px solid #5286b2;padding-left:16px}@media(max-width:650px){.images{grid-template-columns:repeat(2,1fr)}main{padding:18px}}</style>
    <main><p>LLcad · controlled development experiment</p><h1>Automatic reference check and CAD correction</h1>
    <p><strong>${cases.filter((c) => c.pass).length} / ${cases.length} scenarios passed</strong> · ${(Number(result.elapsed_ms) / 1000).toFixed(2)} s for the experiment · ${escape(result.status)}</p>
    <p class="scope">Known synthetic target construction, fixed front view and fixed repair rules. No evidence for a real product reconstruction, full 3D accuracy or PowerPoint output. The time applies to this experiment.</p>
    <p>Error corrections are made through native CAD candidates. Geometry and image are checked again before adoption. A separate oracle compares the resulting native geometry. The image checker receives no list of the manipulations.</p>
    <p><a href="report.json">Complete machine-readable report</a> · <a href="view-contract.json">Fixed check contract</a></p>
    ${result.error ? `<pre>${escape(result.error)}</pre>` : ""}${sections}</main></html>`,
  );
}
