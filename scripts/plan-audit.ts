/** Coverage and evidence-integrity gate; this does not replace human review of proof scope. */
import { readFileSync, existsSync, writeFileSync, mkdirSync } from "node:fs";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { bytesHash } from "../packages/semantic-ir/hash.js";
export function auditPlan(root = process.cwd()) {
  const path = resolve(root, "docs/plan-audit.json");
  if (!existsSync(path))
    return {
      status: "incomplete",
      issues: ["Anforderungsabgleich zum Originalplan fehlt."],
      sections: 0,
      verified: 0,
      remaining: 0,
    };
  const audit = JSON.parse(readFileSync(path, "utf8"));
  const source = readFileSync(
    resolve(root, "Mathematik_First_3D_MCP_Bauplan.md"),
  );
  const issues: string[] = [];
  if (audit.source_sha256 !== bytesHash(source))
    issues.push("Abgleich gehört nicht zum aktuellen Originalplan.");
  const lines = source.toString().split(/\r?\n/);
  const expected = lines.flatMap((line, i) =>
    /^#{2,3} /.test(line) &&
    !/^### Lesepfad|^## Inhaltsverzeichnis|^## Technischer Bauplan/.test(line)
      ? [i + 1]
      : [],
  );
  const covered = new Set<number>();
  let verified = 0;
  for (const section of audit.sections ?? []) {
    const key = section.id + ": " + section.heading;
    if (covered.has(section.source_line))
      issues.push(key + " – doppelter Abschnitt.");
    covered.add(section.source_line);
    if (
      !expected.includes(section.source_line) ||
      section.source_end_line < section.source_line ||
      section.source_end_line > lines.length
    )
      issues.push(key + " – ungültiger Originalverweis.");
    const fragment = lines
      .slice(section.source_line - 1, section.source_end_line)
      .join("\n");
    if (bytesHash(fragment) !== section.source_sha256)
      issues.push(key + " – Originalabschnitt stimmt nicht überein.");
    if (
      !["required", "conditional", "informational"].includes(
        section.classification,
      )
    ) {
      issues.push(key + " – Anforderungen noch nicht vollständig abgeleitet.");
      continue;
    }
    if (!section.requirements?.length) {
      issues.push(key + " – konkrete Abnahmekriterien fehlen.");
      continue;
    }
    if (section.status !== "verified") {
      issues.push(key + " – unvollständig oder nicht ausreichend geprüft.");
      continue;
    }
    if (
      !section.evidence?.length ||
      section.evidence.some(
        (file: string) =>
          !file ||
          file.startsWith("/") ||
          file.split("/").includes("..") ||
          !existsSync(resolve(root, file)),
      )
    ) {
      issues.push(key + " – belastbare lokale Nachweise fehlen.");
      continue;
    }
    if (!section.review?.scope_match || !section.review?.findings) {
      issues.push(key + " – Prüfung der Nachweisabdeckung fehlt.");
      continue;
    }
    verified++;
  }
  for (const line of expected)
    if (!covered.has(line))
      issues.push(
        "Originalabschnitt bei Zeile " + line + " fehlt im Abgleich.",
      );
  return {
    status: issues.length ? "incomplete" : "verified",
    source_sha256: bytesHash(source),
    audit_sha256: bytesHash(readFileSync(path)),
    sections: expected.length,
    verified,
    remaining: expected.length - verified,
    issues,
  };
}
if (
  process.argv[1] &&
  import.meta.url === pathToFileURL(resolve(process.argv[1])).href
) {
  const report = auditPlan();
  mkdirSync("reports", { recursive: true });
  writeFileSync(
    "reports/plan-audit.json",
    JSON.stringify(report, null, 2) + "\n",
  );
  console.log(
    JSON.stringify({
      status: report.status,
      sections: report.sections,
      verified: report.verified,
      remaining: report.remaining,
    }),
  );
  if (report.status !== "verified") process.exitCode = 2;
}
