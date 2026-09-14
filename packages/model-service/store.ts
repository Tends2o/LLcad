import { DatabaseSync } from "node:sqlite";
import { execFileSync } from "node:child_process";
import {
  mkdirSync,
  readFileSync,
  chmodSync,
  openSync,
  closeSync,
} from "node:fs";
import { join, resolve } from "node:path";
import { bytesHash, hash, id } from "../semantic-ir/hash.js";
import { requireThat } from "../semantic-ir/errors.js";
import { Principal, authorize } from "../policy/index.js";
import { durableBlob, prepareBlobStorage } from "./durable-files.js";

export class Store {
  db!: DatabaseSync;
  root: string;
  private lock: number;
  constructor(root: string) {
    this.root = resolve(root);
    const firstCreated = mkdirSync(this.root, { recursive: true, mode: 0o700 });
    mkdirSync(join(this.root, "blobs"), { recursive: true, mode: 0o700 });
    this.lock = openSync(join(this.root, ".store.lock"), "a", 0o600);
    try {
      // flock belongs to the inherited open-file description. Keeping this fd
      // open holds the lock even after the small helper process has exited.
      execFileSync("/usr/bin/flock", ["--exclusive", "--nonblock", "3"], {
        stdio: ["ignore", "ignore", "ignore", this.lock],
      });
    } catch {
      closeSync(this.lock);
      requireThat(
        false,
        "STORE_BUSY",
        "Datenverzeichnis wird bereits verwendet oder flock ist nicht verfügbar. Dienst vor Offline-Wartung stoppen.",
      );
    }
    try {
      this.db = new DatabaseSync(join(this.root, "models.sqlite"));
      const version = (this.db.prepare("PRAGMA user_version").get() as any)
        .user_version;
      requireThat(
        [0, 1, 2, 3, 4].includes(version),
        "BUILD_MISMATCH",
        "Unbekannte Datenbankschemaversion; explizite Migration erforderlich.",
      );
      const synchronized = prepareBlobStorage(
        this.root,
        firstCreated,
        version < 4,
      );
      this.db
        .exec(`PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL; PRAGMA foreign_keys=ON; PRAGMA busy_timeout=5000;
      CREATE TABLE IF NOT EXISTS models(id TEXT PRIMARY KEY,tenant TEXT NOT NULL,owner TEXT NOT NULL,name TEXT NOT NULL,purpose TEXT NOT NULL,head TEXT NOT NULL,created TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS revisions(id TEXT PRIMARY KEY,model TEXT NOT NULL REFERENCES models(id),parent TEXT,ir TEXT NOT NULL,ir_hash TEXT NOT NULL,geometry TEXT,quality TEXT NOT NULL,created TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS transactions(id TEXT PRIMARY KEY,model TEXT NOT NULL REFERENCES models(id),tenant TEXT NOT NULL,owner TEXT NOT NULL,base TEXT NOT NULL,candidate TEXT NOT NULL,state TEXT NOT NULL,plan TEXT NOT NULL,result TEXT,validation TEXT,committed_revision TEXT);
      CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,tenant TEXT NOT NULL,owner TEXT NOT NULL,model TEXT NOT NULL,tx TEXT,kind TEXT NOT NULL,state TEXT NOT NULL,request TEXT NOT NULL,result TEXT,error TEXT,lease TEXT,lease_until INTEGER,attempts INTEGER NOT NULL DEFAULT 0,created TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS idempotency(tenant TEXT NOT NULL,owner TEXT NOT NULL,key TEXT NOT NULL,request_hash TEXT NOT NULL,result TEXT NOT NULL,PRIMARY KEY(tenant,owner,key));
      CREATE TABLE IF NOT EXISTS artifacts(id TEXT PRIMARY KEY,tenant TEXT NOT NULL,owner TEXT NOT NULL,model TEXT,revision TEXT,hash TEXT NOT NULL,mime TEXT NOT NULL,size INTEGER NOT NULL,manifest TEXT NOT NULL,created TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS selections(id TEXT PRIMARY KEY,tenant TEXT NOT NULL,owner TEXT NOT NULL,model TEXT NOT NULL,revision TEXT NOT NULL,feature TEXT NOT NULL,expires INTEGER NOT NULL);
      CREATE TABLE IF NOT EXISTS selection_faces(selection_id TEXT PRIMARY KEY REFERENCES selections(id) ON DELETE CASCADE,geometry_feature TEXT NOT NULL,face_id TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS cache(tenant TEXT NOT NULL,key TEXT NOT NULL,blob TEXT NOT NULL,PRIMARY KEY(tenant,key));
      CREATE TABLE IF NOT EXISTS audit(seq INTEGER PRIMARY KEY AUTOINCREMENT,event TEXT NOT NULL,data TEXT NOT NULL,previous_hash TEXT NOT NULL,hash TEXT NOT NULL,created TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS outbox(id TEXT PRIMARY KEY,event TEXT NOT NULL,payload TEXT NOT NULL,delivered INTEGER NOT NULL DEFAULT 0);
      CREATE TABLE IF NOT EXISTS scheduler(tenant TEXT NOT NULL,owner TEXT NOT NULL,last_tick INTEGER NOT NULL,PRIMARY KEY(tenant,owner));`);
      requireThat(
        this.get("PRAGMA synchronous").synchronous === 2 &&
          this.get("PRAGMA journal_mode").journal_mode === "wal",
        "INTEGRITY_FAILURE",
        "Datenspeicher benötigt WAL mit vollständiger Synchronisation.",
      );
      if (version < 4)
        this.atomic(() => {
          this.run("PRAGMA user_version=4");
          this.audit("storage_durability_migrated", {
            from_store_version: version,
            to_store_version: 4,
            synchronized_blobs: synchronized,
          });
        });
      chmodSync(join(this.root, "models.sqlite"), 0o600);
    } catch (error) {
      this.db!?.close();
      closeSync(this.lock);
      throw error;
    }
  }
  get(sql: string, ...params: any[]): any {
    return this.db.prepare(sql).get(...params);
  }
  all(sql: string, ...params: any[]): any[] {
    return this.db.prepare(sql).all(...params);
  }
  run(sql: string, ...params: any[]) {
    return this.db.prepare(sql).run(...params);
  }
  atomic<T>(fn: () => T): T {
    this.db.exec("BEGIN IMMEDIATE");
    try {
      const result = fn();
      this.db.exec("COMMIT");
      return result;
    } catch (e) {
      this.db.exec("ROLLBACK");
      throw e;
    }
  }
  dedupe<T>(p: Principal, key: string, request: unknown, fn: () => T): T {
    return this.atomic(() => {
      const digest = hash(request);
      const prior = this.get(
        "SELECT * FROM idempotency WHERE tenant=? AND owner=? AND key=?",
        p.tenant,
        p.user,
        key,
      );
      if (prior) {
        requireThat(
          prior.request_hash === digest,
          "IDEMPOTENCY_CONFLICT",
          "Idempotenzschlüssel wurde mit anderen Argumenten verwendet.",
        );
        return JSON.parse(prior.result);
      }
      const result = fn();
      this.run(
        "INSERT INTO idempotency VALUES(?,?,?,?,?)",
        p.tenant,
        p.user,
        key,
        digest,
        JSON.stringify(result),
      );
      return result;
    });
  }
  model(p: Principal, model: string, scope = "model:read") {
    const row = this.get("SELECT * FROM models WHERE id=?", model);
    requireThat(row, "ACCESS_DENIED", "Modell nicht zugänglich.");
    authorize(p, scope, row);
    return row;
  }
  revision(p: Principal, model: string, revision?: string) {
    const m = this.model(p, model);
    const r = this.get(
      "SELECT * FROM revisions WHERE id=? AND model=?",
      revision ?? m.head,
      model,
    );
    requireThat(r, "STALE_REVISION", "Revision nicht verfügbar.");
    return {
      ...r,
      ir: JSON.parse(r.ir),
      geometry: r.geometry ? JSON.parse(r.geometry) : null,
    };
  }
  transaction(p: Principal, tx: string) {
    const row = this.get("SELECT * FROM transactions WHERE id=?", tx);
    requireThat(row, "ACCESS_DENIED", "Kandidat nicht zugänglich.");
    authorize(p, "model:read", row);
    return {
      ...row,
      plan: JSON.parse(row.plan),
      result: row.result ? JSON.parse(row.result) : null,
      validation: row.validation ? JSON.parse(row.validation) : null,
    };
  }
  audit(event: string, data: Record<string, unknown>) {
    const previous =
      this.get("SELECT hash FROM audit ORDER BY seq DESC LIMIT 1")?.hash ??
      "0".repeat(64);
    const created = new Date().toISOString();
    const h = hash({ event, data, previous, created });
    this.run(
      "INSERT INTO audit(event,data,previous_hash,hash,created) VALUES(?,?,?,?,?)",
      event,
      JSON.stringify(data),
      previous,
      h,
      created,
    );
  }
  blob(data: Uint8Array | string) {
    return durableBlob(this.root, data);
  }
  readBlob(h: string) {
    requireThat(
      /^[a-f0-9]{64}$/.test(h),
      "ACCESS_DENIED",
      "Ungültige Blob-ID.",
    );
    const data = readFileSync(join(this.root, "blobs", h));
    requireThat(
      bytesHash(data) === h,
      "INTEGRITY_FAILURE",
      "Artefakt-Prüfsumme stimmt nicht.",
    );
    return data;
  }
  artifact(
    p: Principal,
    data: Uint8Array | string,
    mime: string,
    model: string | null,
    revision: string | null,
    manifest: Record<string, unknown>,
  ) {
    const h = this.blob(data),
      aid = id("art");
    this.run(
      "INSERT INTO artifacts VALUES(?,?,?,?,?,?,?,?,?,?)",
      aid,
      p.tenant,
      p.user,
      model,
      revision,
      h,
      mime,
      Buffer.byteLength(data),
      JSON.stringify(manifest),
      new Date().toISOString(),
    );
    return {
      artifact_id: aid,
      uri: `cad://artifacts/${aid}/manifest`,
      download: `/api/artifacts/${aid}`,
      hash: h,
      mime,
      size: Buffer.byteLength(data),
      manifest,
    };
  }
  getArtifact(p: Principal, aid: string) {
    const row = this.get("SELECT * FROM artifacts WHERE id=?", aid);
    requireThat(row, "ACCESS_DENIED", "Artefakt nicht zugänglich.");
    authorize(p, "model:read", row);
    return { ...row, manifest: JSON.parse(row.manifest) };
  }
  close() {
    this.db.close();
    closeSync(this.lock);
  }
}
