import { readFileSync, lstatSync } from "node:fs";
import { resolve } from "node:path";
import { createHash } from "node:crypto";
import { requireThat, CadError } from "../semantic-ir/errors.js";

const root = resolve(process.env.MATHFORGE_ROOT ?? process.cwd());
const inputs = [
  "workers/mesh-cgal/check.cpp",
  "scripts/build-native.py",
  "deployment/native-packages.json",
];
const fingerprint = createHash("sha256");
for (const name of inputs)
  fingerprint
    .update(name + "\0")
    .update(readFileSync(resolve(root, name)))
    .update("\0");
export const NATIVE_MESH_SOURCE_HASH = fingerprint.digest("hex");
export function checkNativeMeshBuild() {
  const binary = resolve(root, "workers/cad-occt/meshcheck"),
    manifest = resolve(root, "workers/cad-occt/.meshcheck-build.json");
  try {
    const info = lstatSync(binary),
      data = JSON.parse(readFileSync(manifest, "utf8"));
    requireThat(
      info.isFile() &&
        !info.isSymbolicLink() &&
        info.size <= 32 * 1024 * 1024 &&
        data.source_hash === NATIVE_MESH_SOURCE_HASH &&
        data.cgal === "6.0.1" &&
        data.kernel === "EPECK" &&
        data.binary_sha256 ===
          createHash("sha256").update(readFileSync(binary)).digest("hex"),
      "BUILD_MISMATCH",
      "Nativer Meshprüfer fehlt oder passt nicht zum Quellstand.",
    );
    return data;
  } catch {
    throw new CadError(
      "BUILD_MISMATCH",
      "Nativer Meshprüfer fehlt oder passt nicht zum Quellstand; geprüften Build bereitstellen.",
    );
  }
}
