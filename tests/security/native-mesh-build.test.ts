import test from "node:test";
import assert from "node:assert/strict";
import {
  mkdtempSync,
  mkdirSync,
  copyFileSync,
  readFileSync,
  writeFileSync,
  rmSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { resolve, join, dirname } from "node:path";
import { pathToFileURL } from "node:url";
import { spawnSync } from "node:child_process";
test("native checker rejects missing, changed and source-stale binaries before worker input", () => {
  const root = mkdtempSync(join(tmpdir(), "llcad-native-check-"));
  try {
    for (const file of [
      "workers/mesh-cgal/check.cpp",
      "scripts/build-native.py",
      "deployment/native-packages.json",
      "workers/cad-occt/.meshcheck-build.json",
    ]) {
      mkdirSync(dirname(join(root, file)), { recursive: true });
      copyFileSync(file, join(root, file));
    }
    const check = () =>
      spawnSync(
        process.execPath,
        [
          "--import",
          "tsx",
          "--input-type=module",
          "-e",
          `import {checkNativeMeshBuild} from ${JSON.stringify(pathToFileURL(resolve("packages/compiler/native-build.ts")).href)}; try {checkNativeMeshBuild(); console.log("current");} catch(e) { console.log(e.code); process.exitCode=2; }`,
        ],
        {
          env: { ...process.env, MATHFORGE_ROOT: root },
          encoding: "utf8",
          timeout: 10000,
        },
      );
    assert.match(check().stdout, /BUILD_MISMATCH/);
    const target = join(root, "workers/cad-occt/meshcheck");
    copyFileSync("workers/cad-occt/meshcheck", target);
    assert.equal(check().status, 0);
    const binary = readFileSync(target);
    binary[binary.length - 1] ^= 1;
    writeFileSync(target, binary);
    assert.match(check().stdout, /BUILD_MISMATCH/);
    copyFileSync("workers/cad-occt/meshcheck", target);
    writeFileSync(
      join(root, "workers/mesh-cgal/check.cpp"),
      "// stale source\n",
    );
    assert.match(check().stdout, /BUILD_MISMATCH/);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});
