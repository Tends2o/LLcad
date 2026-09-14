/** Compare the effective runtime and installed packages to a reviewable lock. */
import { readFileSync, writeFileSync } from "node:fs";
import { execFileSync } from "node:child_process";
import { bytesHash, hash } from "../packages/semantic-ir/hash.js";
const pkg = JSON.parse(readFileSync("package.json", "utf8"));
const requirements = readFileSync("requirements.lock", "utf8")
  .split(/\r?\n/)
  .filter(Boolean);
const python = JSON.parse(
  execFileSync(
    ".venv/bin/python",
    [
      "-c",
      `
import json,sys,platform,importlib.metadata
requirements=json.load(sys.stdin)
print(json.dumps({'version':'Python '+platform.python_version(),'packages':{line.split('==')[0]:importlib.metadata.version(line.split('==')[0]) for line in requirements}}))
`,
    ],
    { input: JSON.stringify(requirements), encoding: "utf8" },
  ),
);
const npm = (deps: Record<string, string>) =>
  Object.fromEntries(
    Object.entries(deps).map(([name, expected]) => {
      const installed = JSON.parse(
        readFileSync(`node_modules/${name}/package.json`, "utf8"),
      ).version;
      if (expected !== installed)
        throw new Error(`Installierte npm-Version weicht ab: ${name}`);
      return [name, installed];
    }),
  );
for (const line of requirements) {
  const [name, version] = line.split("==");
  if (python.packages[name] !== version)
    throw new Error(`Installierte Python-Version weicht ab: ${name}`);
}
const native = JSON.parse(
  readFileSync("deployment/native-packages.json", "utf8"),
);
const nativePackages = Object.fromEntries(
  ["python3-openvdb", "libopenvdb10.0t64"].map((name) => {
    const version = execFileSync("dpkg-query", ["-W", "-f=${Version}", name], {
      encoding: "utf8",
    }).trim();
    if (version !== native[name])
      throw new Error(`Installierte native Version weicht ab: ${name}`);
    return [name, version];
  }),
);
const files = [
  "package-lock.json",
  "requirements.lock",
  "deployment/native-packages.json",
];
const ciContainer = readFileSync(".github/workflows/ci.yml", "utf8").match(
  /^\s+image:\s+(\S+)\s*$/m,
)?.[1];
if (!ciContainer || !/^[a-z0-9:/.-]+@sha256:[a-f0-9]{64}$/.test(ciContainer))
  throw new Error(
    "CI-Container benötigt einen unveränderlichen SHA-256-Digest.",
  );
const result = {
  application: pkg.version,
  ir_schema: "1",
  node: process.version,
  python: python.version,
  npm_dependencies: npm(pkg.dependencies),
  npm_dev_dependencies: npm(pkg.devDependencies),
  native_requirements: requirements,
  distribution_packages: nativePackages,
  ci_container: ciContainer,
  locks: Object.fromEntries(
    files.map((file) => [file, bytesHash(readFileSync(file))]),
  ),
};
if (process.argv.includes("--write"))
  writeFileSync("versions.lock", JSON.stringify(result, null, 2) + "\n");
else if (
  hash(JSON.parse(readFileSync("versions.lock", "utf8"))) !== hash(result)
)
  throw new Error(
    "versions.lock stimmt nicht mit den installierten, gebundenen Abhängigkeiten überein.",
  );
console.log(
  "Runtime, npm-, Python- und native Paketversionen stimmen mit ihren Locks überein.",
);
