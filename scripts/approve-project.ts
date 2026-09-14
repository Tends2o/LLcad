/** Trusted local policy entry point, deliberately absent from the CAD tool set.
 * Invoke only for a concrete user-authorized grant/revocation after reviewing
 * its complete proposal. The expected digest must come from that review.
 */
import {
  constants,
  openSync,
  closeSync,
  fstatSync,
  readFileSync,
} from "node:fs";
import { resolve, join } from "node:path";
import {
  AccessPayload,
  AccessDecision,
} from "../packages/semantic-ir/access.js";
import { Id } from "../packages/semantic-ir/identifiers.js";
import { signApproval } from "../packages/policy/approvals.js";
import { requireThat } from "../packages/semantic-ir/errors.js";

async function main() {
  const [requestID, digest, ...extra] = process.argv.slice(2);
  requireThat(
    Id.safeParse(requestID).success &&
      /^[a-f0-9]{64}$/.test(digest ?? "") &&
      !extra.length,
    "NEEDS_APPROVAL",
    "Antragskennung und ausdrücklich geprüfter Aktionsdigest erforderlich.",
  );
  const root = resolve(process.env.MATHFORGE_DATA ?? "data"),
    url = new URL(process.env.MATHFORGE_PUBLIC_URL ?? "http://127.0.0.1:4310");
  requireThat(
    url.protocol === "http:" &&
      ["127.0.0.1", "[::1]"].includes(url.hostname) &&
      !url.username &&
      !url.password &&
      url.pathname === "/" &&
      !url.search &&
      !url.hash,
    "ACCESS_DENIED",
    "Dieser lokale Policy-Helper benötigt den konfigurierten Loopback-Dienst.",
  );
  const fd = openSync(
    join(root, "local-token"),
    constants.O_RDONLY | constants.O_NOFOLLOW,
  );
  let token: string;
  try {
    const info = fstatSync(fd);
    requireThat(
      info.isFile() &&
        info.uid === process.getuid?.() &&
        (info.mode & 0o077) === 0 &&
        info.size <= 513,
      "AUTH_REQUIRED",
      "Privater lokaler Zugangsschlüssel erforderlich.",
    );
    token = readFileSync(fd, "utf8").trim();
    requireThat(
      /^[A-Za-z0-9_-]{32,512}$/.test(token),
      "AUTH_REQUIRED",
      "Ungültiger Zugangsschlüssel.",
    );
  } finally {
    closeSync(fd);
  }
  const headers = {
    Authorization: "Bearer " + token,
    "Content-Type": "application/json",
  };
  const request = await fetch(
    new URL("/api/policy/requests/" + requestID, url),
    {
      headers,
      redirect: "error",
      signal: AbortSignal.timeout(5000),
    },
  );
  requireThat(
    request.ok,
    "NEEDS_APPROVAL",
    "Freigabeantrag ist nicht verfügbar.",
  );
  const view = AccessPayload.parse(await request.json());
  requireThat(
    "request_state" in view && view.request_state === "pending",
    "NEEDS_APPROVAL",
    "Freigabeantrag ist nicht offen.",
  );
  const approval = await signApproval(
    root,
    new URL("/api/policy/approvals", url).toString(),
    view,
    digest,
  );
  const response = await fetch(
    new URL("/api/policy/approvals/" + requestID, url),
    {
      method: "POST",
      headers,
      body: JSON.stringify({ approval_jwt: approval }),
      redirect: "error",
      signal: AbortSignal.timeout(5000),
    },
  );
  requireThat(
    response.ok,
    "NEEDS_APPROVAL",
    "Dienst hat die gebundene Bestätigung abgewiesen; aktuellen Antrag prüfen.",
  );
  console.log(JSON.stringify(AccessDecision.parse(await response.json())));
}
main().catch(() => {
  // Never echo credentials, signed assertions, request bodies or native errors.
  console.error(
    "Projektfreigabe nicht ausgeführt. Privaten Dienst, offenen Antrag und ausdrücklich geprüften Aktionsdigest prüfen.",
  );
  process.exitCode = 1;
});
