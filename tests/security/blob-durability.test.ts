import test from "node:test";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import {
  mkdtempSync,
  rmSync,
  readdirSync,
  readFileSync,
  writeFileSync,
  existsSync,
  statSync,
  symlinkSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { Store } from "../../packages/model-service/store.js";
import { bytesHash } from "../../packages/semantic-ir/hash.js";
import { SCOPES } from "../../packages/policy/index.js";
import { DatabaseSync } from "node:sqlite";
const principal = { tenant: "test", user: "owner", scopes: SCOPES };
const payload = "durable geometry bytes\n".repeat(4096);
const digest = bytesHash(payload);

test("migration syncs and verifies legacy blobs before atomically recording storage version 4", () => {
  const root = mkdtempSync(join(tmpdir(), "llcad-storage-migration-"));
  try {
    const old = new Store(root);
    old.blob(payload);
    old.run("PRAGMA user_version=3");
    old.close();
    const path = join(root, "blobs", digest);
    writeFileSync(path, "old incomplete blob");
    assert.throws(
      () => new Store(root),
      (e: any) => e.code === "INTEGRITY_FAILURE",
    );
    const before = new DatabaseSync(join(root, "models.sqlite"), {
      readOnly: true,
    });
    assert.equal(
      (before.prepare("PRAGMA user_version").get() as any).user_version,
      3,
    );
    before.close();
    writeFileSync(path, payload);
    const current = new Store(root);
    try {
      assert.equal(current.get("PRAGMA user_version").user_version, 6);
      const migration = JSON.parse(
        current.get(
          "SELECT data FROM audit WHERE event='storage_durability_migrated' ORDER BY seq DESC LIMIT 1",
        ).data,
      );
      assert.deepEqual(migration, {
        from_store_version: 3,
        to_store_version: 4,
        synchronized_blobs: 1,
      });
      assert.equal(current.readBlob(digest).toString(), payload);
    } finally {
      current.close();
    }
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

function child(root: string, phase: string) {
  return spawnSync(
    process.execPath,
    ["--import", "tsx", "--input-type=module"],
    {
      cwd: process.cwd(),
      encoding: "utf8",
      timeout: 15000,
      env: {
        ...process.env,
        LLCAD_TEST_DIR: root,
        LLCAD_TEST_PHASE: phase,
        LLCAD_TEST_STORE_MODULE: resolve("packages/model-service/store.ts"),
      },
      input: `
import fs from 'node:fs';
import assert from 'node:assert/strict';
import { syncBuiltinESMExports } from 'node:module';
import { pathToFileURL } from 'node:url';
const { Store } = await import(pathToFileURL(process.env.LLCAD_TEST_STORE_MODULE).href);
const root=process.env.LLCAD_TEST_DIR, phase=process.env.LLCAD_TEST_PHASE;
const store=new Store(root), p={tenant:'test',user:'owner',scopes:['model:read']};
const originalSync=fs.fsyncSync, originalLink=fs.linkSync;
let syncedFile=false,syncedDirectory=false;
const order=[];
fs.linkSync=(...args)=>{assert.ok(syncedFile,'file must be synced before publication');order.push('publish');return originalLink(...args)};
fs.fsyncSync=(fd)=>{
 const path=fs.readlinkSync('/proc/self/fd/'+fd);
 const file=path.startsWith(root+'/.blob-staging/blob-'), directory=path===root+'/blobs';
 if(file && phase==='before_file_sync')process.kill(process.pid,'SIGKILL');
 if(directory && phase==='before_directory_sync')process.kill(process.pid,'SIGKILL');
 if(directory && phase==='sync_error')throw Object.assign(new Error('Injected directory sync failure'),{code:'EIO'});
 originalSync(fd);
 if(file){syncedFile=true;order.push('file_sync');if(phase==='after_file_sync')process.kill(process.pid,'SIGKILL');}
 if(directory){syncedDirectory=true;order.push('directory_sync');if(phase==='after_directory_sync')process.kill(process.pid,'SIGKILL');}
};
syncBuiltinESMExports();
const originalRun=store.run.bind(store);
store.run=(sql,...args)=>{if(sql.startsWith('INSERT INTO artifacts')){assert.ok(syncedDirectory,'metadata cannot precede durable file publication');order.push('metadata');}return originalRun(sql,...args)};
let failed=false;
try{store.dedupe(p,'durable-write-001',{action:'write'},()=>store.artifact(p,'durable geometry bytes\\n'.repeat(4096),'application/octet-stream',null,null,{}));}
catch(error){if(phase!=='sync_error')throw error;assert.equal(error.code,'EIO');failed=true;}
if(phase==='sync_error'){
 assert.ok(failed);
 assert.equal(store.get('SELECT COUNT(*) AS n FROM artifacts').n,0);
 assert.equal(store.get('SELECT COUNT(*) AS n FROM idempotency').n,0);
}
store.close();
console.log(JSON.stringify({order,failed}));
`,
    },
  );
}

test("blob bytes and publication are synced before transactional metadata; identical content stays immutable", () => {
  const base = mkdtempSync(join(tmpdir(), "llcad-durable-")),
    root = join(base, "nested", "store");
  try {
    const result = child(root, "normal");
    assert.equal(result.status, 0, result.stderr);
    assert.deepEqual(JSON.parse(result.stdout).order, [
      "file_sync",
      "publish",
      "directory_sync",
      "metadata",
    ]);
    const store = new Store(root);
    try {
      assert.equal(store.get("PRAGMA synchronous").synchronous, 2);
      const before = statSync(join(root, "blobs", digest));
      assert.equal(store.blob(payload), digest);
      assert.equal(statSync(join(root, "blobs", digest)).ino, before.ino);
      assert.equal(store.readBlob(digest).toString(), payload);
      assert.deepEqual(readdirSync(join(root, ".blob-staging")), []);
    } finally {
      store.close();
    }
  } finally {
    rmSync(base, { recursive: true, force: true });
  }
});

test("actual process kills across blob publication leave no committed references or partial named blobs", () => {
  const base = mkdtempSync(join(tmpdir(), "llcad-crash-"));
  try {
    for (const phase of [
      "before_file_sync",
      "after_file_sync",
      "before_directory_sync",
      "after_directory_sync",
    ]) {
      const root = join(base, phase),
        result = child(root, phase);
      assert.equal(result.signal, "SIGKILL", result.stderr);
      const final = join(root, "blobs", digest);
      if (["before_file_sync", "after_file_sync"].includes(phase))
        assert.equal(existsSync(final), false);
      if (existsSync(final))
        assert.equal(bytesHash(readFileSync(final)), digest);
      const store = new Store(root);
      try {
        assert.equal(store.get("SELECT COUNT(*) AS n FROM artifacts").n, 0);
        assert.equal(store.get("SELECT COUNT(*) AS n FROM idempotency").n, 0);
        assert.deepEqual(readdirSync(join(root, ".blob-staging")), []);
        const artifact = store.dedupe(
          principal,
          "durable-write-001",
          { action: "write" },
          () =>
            store.artifact(
              principal,
              payload,
              "application/octet-stream",
              null,
              null,
              {},
            ),
        );
        assert.equal(artifact.hash, digest);
        assert.equal(store.readBlob(digest).toString(), payload);
        assert.deepEqual(readdirSync(join(root, "blobs")), [digest]);
      } finally {
        store.close();
      }
    }
  } finally {
    rmSync(base, { recursive: true, force: true });
  }
});

test("failed storage sync rolls metadata back, and corrupt existing hashes are never reused or overwritten", () => {
  const root = mkdtempSync(join(tmpdir(), "llcad-sync-error-"));
  try {
    const result = child(root, "sync_error");
    assert.equal(result.status, 0, result.stderr);
    assert.equal(JSON.parse(result.stdout).failed, true);
    const store = new Store(root);
    try {
      const path = join(root, "blobs", digest);
      writeFileSync(path, "corrupt previous write");
      assert.throws(
        () =>
          store.dedupe(
            principal,
            "durable-write-001",
            { action: "write" },
            () =>
              store.artifact(
                principal,
                payload,
                "application/octet-stream",
                null,
                null,
                {},
              ),
          ),
        (e: any) => e.code === "INTEGRITY_FAILURE",
      );
      assert.equal(readFileSync(path, "utf8"), "corrupt previous write");
      assert.equal(store.get("SELECT COUNT(*) AS n FROM artifacts").n, 0);
      assert.equal(store.get("SELECT COUNT(*) AS n FROM idempotency").n, 0);
    } finally {
      store.close();
    }
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("unknown database versions and foreign staging entries are retained for explicit recovery", () => {
  const root = mkdtempSync(join(tmpdir(), "llcad-staging-"));
  try {
    const store = new Store(root);
    store.run("PRAGMA user_version=999");
    store.close();
    const pending = join(
      root,
      ".blob-staging",
      "blob-00000000-0000-0000-0000-000000000000",
    );
    writeFileSync(pending, "future pending write");
    assert.throws(
      () => new Store(root),
      (e: any) => e.code === "BUILD_MISMATCH",
    );
    assert.equal(readFileSync(pending, "utf8"), "future pending write");
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
  const base = mkdtempSync(join(tmpdir(), "llcad-staging-link-"));
  try {
    const store = new Store(base);
    store.close();
    const outside = join(base, "operator-note");
    writeFileSync(outside, "retain me");
    symlinkSync(
      outside,
      join(base, ".blob-staging", "blob-00000000-0000-0000-0000-000000000000"),
    );
    assert.throws(
      () => new Store(base),
      (e: any) => e.code === "INTEGRITY_FAILURE",
    );
    assert.equal(readFileSync(outside, "utf8"), "retain me");
  } finally {
    rmSync(base, { recursive: true, force: true });
  }
});
