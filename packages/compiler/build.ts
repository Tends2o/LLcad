import { readFileSync, readdirSync } from "node:fs";
import { resolve } from "node:path";
import { hash, bytesHash } from "../semantic-ir/hash.js";
const root = resolve(process.env.MATHFORGE_ROOT ?? process.cwd());
/** Cache/proof identity includes actual worker source and locked native dependencies. */
const buildFiles = [
  "workers/cad-occt/geometry.py",
  "workers/cad-occt/frames.py",
  "workers/cad-occt/advanced.py",
  "workers/cad-occt/analysis.py",
  "workers/cad-occt/solver.py",
  "workers/cad-occt/main.py",
  "workers/cad-occt/mesh_quality.py",
  "workers/mesh-cgal/check.cpp",
  "scripts/build-native.py",
  "packages/compiler/native-build.ts",
  "workers/cad-occt/fields.py",
  "workers/cad-occt/intervals.py",
  "workers/cad-occt/extract.py",
  "workers/cad-occt/field_analysis.py",
  "workers/cad-occt/mesh_ops.py",
  "workers/cad-occt/views.py",
  "workers/cad-occt/step_structure.py",
  "workers/cad-occt/distances.py",
  "workers/cad-occt/bezier.py",
  "workers/cad-occt/field_cache.py",
  "workers/cad-occt/volume_io.py",
  "deployment/native-packages.json",
  "workers/cad-occt/topology.py",
  "packages/compiler/index.ts",
  "packages/compiler/expressions.ts",
  "packages/compiler/constraints.ts",
  "packages/compiler/patches.ts",
  "packages/compiler/bernstein.ts",
  "packages/compiler/math.ts",
  "packages/compiler/nurbs.ts",
  "packages/compiler/affine.ts",
  "packages/compiler/field-regions.ts",
  "packages/compiler/structure.ts",
  "packages/compiler/build.ts",
  "packages/semantic-ir/units.ts",
  "packages/semantic-ir/hash.ts",
  "packages/semantic-ir/schema.ts",
  "packages/semantic-ir/mesh.ts",
  "packages/semantic-ir/identifiers.ts",
  "packages/semantic-ir/access.ts",
  "packages/validation/index.ts",
  "packages/policy/index.ts",
  "requirements.lock",
];
export const currentBuildHash = () =>
  hash(
    buildFiles.map((path) => ({
      path,
      sha256: bytesHash(readFileSync(resolve(root, path))),
    })),
  );
export const BUILD_HASH = currentBuildHash();

/** Release evidence covers the service, UI, gates, checks and deployment too. */
const implementationFiles = [
  "packages",
  "hooks",
  "workers",
  "scripts",
  "tests",
  "benchmarks",
  "deployment",
].flatMap((directory) =>
  readdirSync(resolve(root, directory), { recursive: true })
    .map(String)
    .filter(
      (path) =>
        !path.includes(".meshcheck") &&
        (/\.(ts|py|cpp|hpp|json|sh|service|example)$/.test(path) ||
          path === "Caddyfile"),
    )
    .map((path) => directory + "/" + path),
);
export const IMPLEMENTATION_HASH = hash(
  [
    ...implementationFiles,
    "package.json",
    "package-lock.json",
    "requirements.lock",
    "versions.lock",
    "tsconfig.json",
    "public/index.html",
    "public/style.css",
    ".github/workflows/ci.yml",
  ]
    .sort()
    .map((path) => ({
      path,
      sha256: bytesHash(readFileSync(resolve(root, path))),
    })),
);
