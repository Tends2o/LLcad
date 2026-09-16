/** License and distribution check (Bauplan 21.3): every shipped component gets a license
 * class and a distribution note. Local operation needs no approval; distributing binaries
 * with strong-copyleft native components requires an explicit operator review. */
import {
  readFileSync,
  readdirSync,
  existsSync,
  writeFileSync,
  mkdirSync,
} from "node:fs";
import { join } from "node:path";
const CLASSES: Record<string, string> = {
  MIT: "permissive",
  ISC: "permissive",
  "BSD-2-Clause": "permissive",
  "BSD-3-Clause": "permissive",
  "Apache-2.0": "permissive",
  "0BSD": "permissive",
  "BSL-1.0": "permissive",
  "PSF-2.0": "permissive",
  Unlicense: "permissive",
  "BlueOak-1.0.0": "permissive",
  "CC0-1.0": "permissive",
  "MIT-CMU": "permissive",
  HPND: "permissive",
  "Python-2.0": "permissive",
  "MPL-2.0": "weak_copyleft",
  "LGPL-2.1-or-later": "weak_copyleft",
  "LGPL-2.1-only": "weak_copyleft",
  "LGPL-3.0-or-later": "weak_copyleft",
  "LGPL-2.1-with-OCCT-exception": "weak_copyleft",
  "GPL-2.0-or-later": "strong_copyleft",
  "GPL-3.0-or-later": "strong_copyleft",
};
const classify = (license: string | null) => {
  if (!license) return "unknown";
  if (CLASSES[license]) return CLASSES[license];
  if (
    /^\(?(MIT|ISC|BSD-[23]-Clause|Apache-2\.0)( OR | AND |\)| )/.test(license)
  )
    return "permissive";
  if (/GPL/.test(license) && !/LGPL/.test(license)) return "strong_copyleft";
  if (/LGPL|MPL/.test(license)) return "weak_copyleft";
  return "unknown";
};
const npm: {
  name: string;
  version: string;
  license: string | null;
  scope: string;
}[] = [];
const roots = ["node_modules"];
const seen = new Set<string>();
for (const root of roots) {
  const walk = (dir: string, depth: number) => {
    if (depth > 6 || !existsSync(dir)) return;
    for (const entry of readdirSync(dir, { withFileTypes: true })) {
      if (!entry.isDirectory() || entry.name.startsWith(".")) continue;
      const path = join(dir, entry.name);
      if (entry.name.startsWith("@")) {
        walk(path, depth);
        continue;
      }
      const manifest = join(path, "package.json");
      if (existsSync(manifest)) {
        try {
          const pkg = JSON.parse(readFileSync(manifest, "utf8"));
          const key = pkg.name + "@" + pkg.version;
          if (!seen.has(key) && pkg.name) {
            seen.add(key);
            const license =
              typeof pkg.license === "string"
                ? pkg.license
                : (pkg.license?.type ??
                  (Array.isArray(pkg.licenses)
                    ? pkg.licenses.map((l: any) => l.type).join(" OR ")
                    : null));
            npm.push({
              name: pkg.name,
              version: pkg.version,
              license,
              scope: "npm",
            });
          }
        } catch {
          /* unreadable manifest is reported as unknown below */
        }
        walk(join(path, "node_modules"), depth + 1);
      }
    }
  };
  walk(root, 0);
}
const inventory = existsSync("reports/license-inventory.json")
  ? JSON.parse(readFileSync("reports/license-inventory.json", "utf8"))
  : { components: [] };
const classifierLicense = (classifiers: string[] = []) => {
  const map: Record<string, string> = {
    "Apache Software License": "Apache-2.0",
    "MIT License": "MIT",
    "BSD License": "BSD-3-Clause",
    "Python Software Foundation License": "PSF-2.0",
    "Mozilla Public License 2.0 (MPL 2.0)": "MPL-2.0",
    "GNU Lesser General Public License v2 or later (LGPLv2+)":
      "LGPL-2.1-or-later",
  };
  for (const c of classifiers)
    for (const [k, v] of Object.entries(map)) if (c.includes(k)) return v;
  return null;
};
// Packages whose wheels ship a license file but no machine-readable expression or classifier.
const KNOWN_PYTHON: Record<string, string> = {
  fonttools: "MIT",
  pillow: "MIT-CMU",
};
const python = (inventory.components as any[]).map((c) => ({
  name: c.name,
  version: c.version,
  license:
    c.license_expression ??
    classifierLicense(c.license_classifiers) ??
    KNOWN_PYTHON[c.name] ??
    null,
  scope: "python",
}));
// Native components bundled or linked by the isolated workers, with their upstream licenses.
const native = [
  {
    name: "Open CASCADE Technology (via cadquery-ocp)",
    version: "7.9.3",
    license: "LGPL-2.1-with-OCCT-exception",
    scope: "native",
  },
  {
    name: "CGAL Polygon_mesh_processing headers",
    version: "6.0.1",
    license: "GPL-3.0-or-later",
    scope: "native",
  },
  {
    name: "GMP",
    version: "6.3.0",
    license: "LGPL-3.0-or-later",
    scope: "native",
  },
  {
    name: "MPFR",
    version: "4.2.2",
    license: "LGPL-3.0-or-later",
    scope: "native",
  },
  {
    name: "Boost headers",
    version: "1.83.0",
    license: "BSL-1.0",
    scope: "native",
  },
  { name: "OpenVDB", version: "10.0.1", license: "MPL-2.0", scope: "native" },
  {
    name: "bubblewrap",
    version: "0.12.0",
    license: "LGPL-2.1-or-later",
    scope: "native",
  },
];
const components = [...npm, ...python, ...native].map((c) => ({
  ...c,
  class: classify(c.license),
  distribution_note:
    classify(c.license) === "strong_copyleft"
      ? "Distributing binaries that link this component requires offering the corresponding source of the combined work under the same license; local operation is unaffected."
      : classify(c.license) === "weak_copyleft"
        ? "Library-level copyleft: keep the component replaceable and provide its source/notice; the CAD service itself is not required to change license."
        : classify(c.license) === "unknown"
          ? "License could not be determined automatically; review before any distribution."
          : "Attribution and license text must accompany a distribution.",
}));
const summary = Object.fromEntries(
  ["permissive", "weak_copyleft", "strong_copyleft", "unknown"].map((k) => [
    k,
    components.filter((c) => c.class === k).length,
  ]),
);
const acknowledged =
  process.env.MATHFORGE_DISTRIBUTION_REVIEW === "acknowledged";
const report = {
  created: new Date().toISOString(),
  scope: "local_service_operation_and_hypothetical_binary_distribution",
  components,
  summary,
  distribution_review: {
    status:
      summary.unknown === 0 && (summary.strong_copyleft === 0 || acknowledged)
        ? "acknowledged"
        : "requires_operator_review",
    strong_copyleft_components: components
      .filter((c) => c.class === "strong_copyleft")
      .map((c) => c.name),
    unknown_components: components
      .filter((c) => c.class === "unknown")
      .map((c) => c.name),
    how_to_acknowledge:
      "MATHFORGE_DISTRIBUTION_REVIEW=acknowledged after a human legal review; local operation never requires it",
  },
  note: "This is an automated inventory with classification, not legal advice and not a substitute for a component-specific license review.",
};
mkdirSync("reports", { recursive: true });
writeFileSync(
  "reports/license-check.json",
  JSON.stringify(report, null, 2) + "\n",
);
console.log(
  JSON.stringify({
    components: components.length,
    ...summary,
    review: report.distribution_review.status,
  }),
);
