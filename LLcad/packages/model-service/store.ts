import { DatabaseSync } from "node:sqlite";
import { execFileSync } from "node:child_process";
import {
  mkdirSync,
  readFileSync,
  writeFileSync,
  existsSync,
  chmodSync,
  openSync,
  closeSync,
  writeSync,
  unlinkSync,
} from "node:fs";
import { join, resolve } from "node:path";
import { bytesHash, hash, id } from "../semantic-ir/hash.js";
import { requireThat } from "../semantic-ir/errors.js";
import { Principal, authorize } from "../policy/index.js";
import { durableBlob, prepareBlobStorage } from "./durable-files.js";
import { ProjectAccess } from "./access.js";
import { IS_WINDOWS } from "../semantic-ir/host.js";

/** Windows has no flock: an exclusively created owner file names the process that
 *  holds the data folder. One left behind by a process that no longer runs is taken
 *  over; the file is removed again when the store closes or the process exits. */
function holdWindowsLock(root: string) {
  const path = join(root, ".store.owner");
  const alive = (pid: number) => {
    try {
      process.kill(pid, 0);
      return true;
    } catch (error: any) {
      return error.code === "EPERM";
    }
  };
  for (let attempt = 0; attempt < 3; attempt++) {
    try {
      const fd = openSync(path, "wx", 0o600);
      writeSync(fd, String(process.pid));
      closeSync(fd);
      const release = () => {
        try {
          if (readFileSync(path, "utf8").trim() === String(process.pid))
            unlinkSync(path);
        } catch {
          /* already gone */
        }
      };
      process.once("exit", release);
      return release;
    } catch (error: any) {
      if (error.code !== "EEXIST") throw error;
      let owner = 0;
      try {
        owner = Number(readFileSync(path, "utf8").trim());
      } catch {
        /* removed meanwhile */
      }
      if (owner && owner !== process.pid && alive(owner))
        throw new Error("data folder in use by process " + owner);
      try {
        unlinkSync(path);
      } catch {
        /* another starter removed it first */
      }
    }
  }
  throw new Error("data folder lock contended");
}

export class Store {
  db!: DatabaseSync;
  root: string;
  private lock: number;
  private releaseLock: () => void = () => {};
  access: ProjectAccess;
  constructor(root: string) {
    this.root = resolve(root);
    this.access = new ProjectAccess(this);
    const firstCreated = mkdirSync(this.root, { recursive: true, mode: 0o700 });
    mkdirSync(join(this.root, "blobs"), { recursive: true, mode: 0o700 });
    this.lock = openSync(join(this.root, ".store.lock"), "a", 0o600);
    try {
      // flock belongs to the inherited open-file description. Keeping this fd
      // open holds the lock even after the small helper process has exited.
      if (IS_WINDOWS) this.releaseLock = holdWindowsLock(this.root);
      else
        execFileSync("/usr/bin/flock", ["--exclusive", "--nonblock", "3"], {
          stdio: ["ignore", "ignore", "ignore", this.lock],
        });
    } catch {
      closeSync(this.lock);
      requireThat(
        false,
        "STORE_BUSY",
        "The data directory is already in use or flock is not available. Stop the service before offline maintenance.",
      );
    }
    try {
      this.db = new DatabaseSync(join(this.root, "models.sqlite"));
      const version = (this.db.prepare("PRAGMA user_version").get() as any)
        .user_version;
      requireThat(
        [0, 1, 2, 3, 4, 5, 6, 7].includes(version),
        "BUILD_MISMATCH",
        "Unknown database schema version; explicit migration required.",
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
      CREATE TABLE IF NOT EXISTS transactions(id TEXT PRIMARY KEY,model TEXT NOT NULL REFERENCES models(id),tenant TEXT NOT NULL,owner TEXT NOT NULL,base TEXT NOT NULL,candidate TEXT NOT NULL,state TEXT NOT NULL,plan TEXT NOT NULL,result TEXT,validation TEXT,committed_revision TEXT,repair_of TEXT,repair_cause TEXT);
      CREATE TABLE IF NOT EXISTS metrics(name TEXT PRIMARY KEY,count INTEGER NOT NULL,sum REAL NOT NULL,max REAL NOT NULL,updated TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS publications(id TEXT PRIMARY KEY,tenant TEXT NOT NULL,owner TEXT NOT NULL,model TEXT NOT NULL REFERENCES models(id) ON DELETE CASCADE,revision TEXT NOT NULL,package_artifact TEXT NOT NULL,package_hash TEXT NOT NULL,recipient TEXT NOT NULL,approval_request TEXT NOT NULL,expires INTEGER NOT NULL,created TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS publication_artifacts(publication TEXT NOT NULL REFERENCES publications(id) ON DELETE CASCADE,artifact TEXT NOT NULL REFERENCES artifacts(id) ON DELETE CASCADE,PRIMARY KEY(publication,artifact));
      CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,tenant TEXT NOT NULL,owner TEXT NOT NULL,model TEXT NOT NULL,tx TEXT,kind TEXT NOT NULL,state TEXT NOT NULL,request TEXT NOT NULL,result TEXT,error TEXT,lease TEXT,lease_until INTEGER,attempts INTEGER NOT NULL DEFAULT 0,created TEXT NOT NULL,phase TEXT,heartbeat INTEGER,budget_seconds INTEGER,started INTEGER,finished INTEGER);
      CREATE TABLE IF NOT EXISTS idempotency(tenant TEXT NOT NULL,owner TEXT NOT NULL,key TEXT NOT NULL,request_hash TEXT NOT NULL,result TEXT NOT NULL,PRIMARY KEY(tenant,owner,key));
      CREATE TABLE IF NOT EXISTS artifacts(id TEXT PRIMARY KEY,tenant TEXT NOT NULL,owner TEXT NOT NULL,model TEXT,revision TEXT,hash TEXT NOT NULL,mime TEXT NOT NULL,size INTEGER NOT NULL,manifest TEXT NOT NULL,created TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS selections(id TEXT PRIMARY KEY,tenant TEXT NOT NULL,owner TEXT NOT NULL,model TEXT NOT NULL,revision TEXT NOT NULL,feature TEXT NOT NULL,expires INTEGER NOT NULL);
      CREATE TABLE IF NOT EXISTS selection_faces(selection_id TEXT PRIMARY KEY REFERENCES selections(id) ON DELETE CASCADE,geometry_feature TEXT NOT NULL,face_id TEXT NOT NULL,anchor TEXT);
      CREATE TABLE IF NOT EXISTS cache(tenant TEXT NOT NULL,key TEXT NOT NULL,blob TEXT NOT NULL,created INTEGER,PRIMARY KEY(tenant,key));
      CREATE TABLE IF NOT EXISTS audit(seq INTEGER PRIMARY KEY AUTOINCREMENT,event TEXT NOT NULL,data TEXT NOT NULL,previous_hash TEXT NOT NULL,hash TEXT NOT NULL,created TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS outbox(id TEXT PRIMARY KEY,event TEXT NOT NULL,payload TEXT NOT NULL,delivered INTEGER NOT NULL DEFAULT 0);
      CREATE TABLE IF NOT EXISTS scheduler(tenant TEXT NOT NULL,owner TEXT NOT NULL,last_tick INTEGER NOT NULL,PRIMARY KEY(tenant,owner));
      CREATE TABLE IF NOT EXISTS render_cache(tenant TEXT NOT NULL,key TEXT NOT NULL,result TEXT NOT NULL,artifacts TEXT NOT NULL,created INTEGER NOT NULL,PRIMARY KEY(tenant,key));
      CREATE TABLE IF NOT EXISTS annotations(id TEXT PRIMARY KEY,tenant TEXT NOT NULL,owner TEXT NOT NULL,model TEXT NOT NULL,revision TEXT NOT NULL,created TEXT NOT NULL,note TEXT NOT NULL,state TEXT NOT NULL,payload TEXT NOT NULL,region_artifact TEXT,view_artifact TEXT,run TEXT);
      CREATE INDEX IF NOT EXISTS annotations_model ON annotations(tenant,model,created);
      CREATE INDEX IF NOT EXISTS transactions_committed ON transactions(committed_revision);
      CREATE TABLE IF NOT EXISTS agent_settings(tenant TEXT PRIMARY KEY,config TEXT NOT NULL,updated TEXT NOT NULL);`);
      requireThat(
        this.get("PRAGMA synchronous").synchronous === 2 &&
          this.get("PRAGMA journal_mode").journal_mode === "wal",
        "INTEGRITY_FAILURE",
        "The data store needs WAL with full synchronisation.",
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
      if (version < 5)
        this.atomic(() => {
          this.db.exec(`
          CREATE TABLE IF NOT EXISTS model_acl(model TEXT PRIMARY KEY REFERENCES models(id) ON DELETE CASCADE,generation INTEGER NOT NULL);
          CREATE TABLE IF NOT EXISTS model_grants(id TEXT UNIQUE NOT NULL,model TEXT NOT NULL REFERENCES models(id) ON DELETE CASCADE,tenant TEXT NOT NULL,recipient TEXT NOT NULL,spec TEXT NOT NULL,state TEXT NOT NULL,version INTEGER NOT NULL,expires INTEGER NOT NULL,created TEXT NOT NULL,PRIMARY KEY(model,recipient));
          CREATE TABLE IF NOT EXISTS approval_requests(id TEXT PRIMARY KEY,tenant TEXT NOT NULL,owner TEXT NOT NULL,model TEXT NOT NULL REFERENCES models(id) ON DELETE CASCADE,proposal TEXT NOT NULL,digest TEXT NOT NULL,state TEXT NOT NULL,expires INTEGER NOT NULL,created TEXT NOT NULL,result TEXT);
          CREATE TABLE IF NOT EXISTS approval_consumptions(nonce TEXT PRIMARY KEY,request TEXT NOT NULL REFERENCES approval_requests(id) ON DELETE CASCADE,expires INTEGER NOT NULL);
          CREATE TABLE IF NOT EXISTS job_authorizations(job TEXT PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,grant_id TEXT,grant_version INTEGER,scope TEXT NOT NULL,seconds INTEGER NOT NULL);
          CREATE TABLE IF NOT EXISTS grant_actions(id TEXT PRIMARY KEY,grant_id TEXT NOT NULL REFERENCES model_grants(id) ON DELETE CASCADE,grant_version INTEGER NOT NULL,kind TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS transaction_authorizations(tx TEXT PRIMARY KEY REFERENCES transactions(id) ON DELETE CASCADE,grant_id TEXT,grant_version INTEGER);
          CREATE TABLE IF NOT EXISTS artifact_models(artifact TEXT NOT NULL REFERENCES artifacts(id) ON DELETE CASCADE,model TEXT NOT NULL REFERENCES models(id) ON DELETE CASCADE,PRIMARY KEY(artifact,model));
          CREATE INDEX IF NOT EXISTS model_grants_recipient ON model_grants(tenant,recipient,state,expires);
          CREATE INDEX IF NOT EXISTS job_authorizations_grant ON job_authorizations(grant_id,grant_version);
          CREATE INDEX IF NOT EXISTS grant_actions_grant ON grant_actions(grant_id,grant_version);
          CREATE INDEX IF NOT EXISTS transaction_authorizations_grant ON transaction_authorizations(grant_id,grant_version);
          PRAGMA user_version=5;
        `);
          // Existing source uploads become readable only through projects whose
          // retained revisions already reference them. Do not alter old rows.
          this.run(`INSERT OR IGNORE INTO artifact_models(artifact,model)
            SELECT a.id,r.model FROM revisions r JOIN models m ON m.id=r.model,
            json_each(r.ir,'$.features') f JOIN artifacts a
              ON a.id=json_extract(f.value,'$.construction.artifact_id')
            WHERE json_extract(f.value,'$.construction.operator')='imported'
              AND a.tenant=m.tenant AND a.owner=m.owner
              AND (a.model IS NULL OR a.model=r.model)`);
          this.audit("project_access_migrated", {
            from_store_version: 4,
            to_store_version: 5,
            existing_models_kept_private: this.get(
              "SELECT COUNT(*) AS n FROM models",
            ).n,
            existing_source_links: this.get(
              "SELECT COUNT(*) AS n FROM artifact_models",
            ).n,
          });
        });
      if (version < 6)
        this.atomic(() => {
          // Additive typed columns; existing rows keep NULLs and are never rewritten.
          const add = (table: string, column: string, definition: string) => {
            const present = this.all(`PRAGMA table_info(${table})`).some(
              (c: any) => c.name === column,
            );
            if (!present)
              this.db.exec(
                `ALTER TABLE ${table} ADD COLUMN ${column} ${definition}`,
              );
          };
          add("jobs", "phase", "TEXT");
          add("jobs", "heartbeat", "INTEGER");
          add("jobs", "budget_seconds", "INTEGER");
          add("jobs", "started", "INTEGER");
          add("jobs", "finished", "INTEGER");
          add("selection_faces", "anchor", "TEXT");
          add("cache", "created", "INTEGER");
          add("transactions", "repair_of", "TEXT");
          add("transactions", "repair_cause", "TEXT");
          this.run("PRAGMA user_version=6");
          this.audit("job_progress_publication_migrated", {
            from_store_version: version,
            to_store_version: 6,
            existing_jobs: this.get("SELECT COUNT(*) AS n FROM jobs").n,
            existing_revisions_unchanged: this.get(
              "SELECT COUNT(*) AS n FROM revisions",
            ).n,
          });
        });
      if (version < 7)
        this.atomic(() => {
          // A hidden model keeps everything it has; only the viewer's model
          // list passes it by. The tools still find it.
          if (
            !this.all("PRAGMA table_info(models)").some(
              (c: any) => c.name === "hidden",
            )
          )
            this.db.exec(
              "ALTER TABLE models ADD COLUMN hidden INTEGER NOT NULL DEFAULT 0",
            );
          this.run("PRAGMA user_version=7");
          this.audit("model_visibility_migrated", {
            from_store_version: version,
            to_store_version: 7,
            existing_models_visible: this.get(
              "SELECT COUNT(*) AS n FROM models",
            ).n,
          });
        });
      chmodSync(join(this.root, "models.sqlite"), 0o600);
    } catch (error) {
      this.db!?.close();
      closeSync(this.lock);
      this.releaseLock();
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
  private commitEffects: (() => void)[] | null = null;
  afterCommit(fn: () => void) {
    if (this.commitEffects) this.commitEffects.push(fn);
    else fn();
  }
  atomic<T>(fn: () => T): T {
    this.db.exec("BEGIN IMMEDIATE");
    this.commitEffects = [];
    let result: T;
    let effects: (() => void)[];
    try {
      result = fn();
      this.db.exec("COMMIT");
      effects = this.commitEffects;
    } catch (e) {
      this.db.exec("ROLLBACK");
      throw e;
    } finally {
      this.commitEffects = null;
    }
    for (const effect of effects) {
      try {
        effect();
      } catch {
        // A failed wakeup/cancellation cannot undo the committed SQL state.
        // Lease checks still prevent a cancelled worker from publishing data.
        try {
          this.audit("after_commit_effect_failed", {});
        } catch {
          /* best effort diagnostic */
        }
      }
    }
    return result;
  }
  private savepoints = 0;
  /** Nested all-or-nothing section inside an open transaction. */
  savepoint<T>(fn: () => T): T {
    if (!this.db.isTransaction) return this.atomic(fn);
    const name = "sp" + ++this.savepoints;
    this.db.exec(`SAVEPOINT ${name}`);
    try {
      const result = fn();
      this.db.exec(`RELEASE ${name}`);
      return result;
    } catch (error) {
      this.db.exec(`ROLLBACK TO ${name}`);
      this.db.exec(`RELEASE ${name}`);
      throw error;
    }
  }
  dedupe<T>(
    p: Principal,
    key: string,
    request: unknown,
    fn: () => T,
    check?: (value: T) => void,
  ): T {
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
          "The idempotency key was used with different arguments.",
        );
        const result = JSON.parse(prior.result);
        check?.(result);
        return result;
      }
      const result = fn();
      check?.(result);
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
    return this.access.context(p, model, scope).model;
  }
  revision(p: Principal, model: string, revision?: string) {
    const m = this.model(p, model);
    const r = this.get(
      "SELECT * FROM revisions WHERE id=? AND model=?",
      revision ?? m.head,
      model,
    );
    requireThat(r, "STALE_REVISION", "Revision not available.");
    return {
      ...r,
      ir: JSON.parse(r.ir),
      geometry: r.geometry ? JSON.parse(r.geometry) : null,
    };
  }
  transaction(p: Principal, tx: string) {
    const row = this.get("SELECT * FROM transactions WHERE id=?", tx);
    requireThat(row, "ACCESS_DENIED", "Candidate not accessible.");
    this.model(p, row.model);
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
  /** Where a blob lies on disk. Only for readers that hand the file to someone
   *  else unchanged, such as staging a sandbox; everything else reads it. */
  path(h: string) {
    requireThat(
      /^[a-f0-9]{64}$/.test(h),
      "ACCESS_DENIED",
      "Invalid blob id.",
    );
    return join(this.root, "blobs", h);
  }
  readBlob(h: string) {
    requireThat(
      /^[a-f0-9]{64}$/.test(h),
      "ACCESS_DENIED",
      "Invalid blob id.",
    );
    const data = readFileSync(join(this.root, "blobs", h));
    requireThat(
      bytesHash(data) === h,
      "INTEGRITY_FAILURE",
      "Artifact checksum mismatch.",
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
    authorize(p, "model:read");
    if (model) this.model(p, model);
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
    requireThat(row, "ACCESS_DENIED", "Artifact not accessible.");
    authorize(p, "model:read");
    requireThat(
      row.tenant === p.tenant,
      "ACCESS_DENIED",
      "Artifact not accessible.",
    );
    if (this.access.publishedTo(p, aid))
      return { ...row, manifest: JSON.parse(row.manifest) };
    if (row.model) this.model(p, row.model);
    else if (row.owner !== p.user) {
      const visible = this.access.visible(p, "m");
      requireThat(
        this.get(
          "SELECT 1 FROM artifact_models a JOIN models m ON m.id=a.model WHERE a.artifact=? AND " +
            visible.sql,
          aid,
          ...visible.params,
        ),
        "ACCESS_DENIED",
        "Artifact not accessible.",
      );
    }
    return { ...row, manifest: JSON.parse(row.manifest) };
  }
  bindArtifact(p: Principal, aid: string, model: string) {
    const artifact = this.getArtifact(p, aid);
    this.model(p, model, "model:edit");
    requireThat(
      !artifact.model || artifact.model === model,
      "OUT_OF_SCOPE",
      "Derived artifacts of another project must not be shared silently.",
    );
    requireThat(
      artifact.owner === p.user ||
        artifact.model === model ||
        this.get(
          "SELECT 1 FROM artifact_models WHERE artifact=? AND model=?",
          aid,
          model,
        ),
      "NEEDS_APPROVAL",
      "The source file is not released for this project.",
    );
    this.run("INSERT OR IGNORE INTO artifact_models VALUES(?,?)", aid, model);
  }
  /** A new private model and its first revision (preview_only, no geometry). */
  createModel(p: Principal, name: string, purpose: string, ir: unknown) {
    authorize(p, "model:create");
    const mid = id("model"),
      rev = id("rev"),
      now = new Date().toISOString();
    // Named columns: a model carries more than it did, and a new one must not
    // depend on the order they were added in.
    this.run(
      "INSERT INTO models(id,tenant,owner,name,purpose,head,created) VALUES(?,?,?,?,?,?,?)",
      mid,
      p.tenant,
      p.user,
      name,
      purpose,
      rev,
      now,
    );
    this.run(
      "INSERT INTO revisions VALUES(?,?,?,?,?,?,?,?)",
      rev,
      mid,
      null,
      JSON.stringify(ir),
      hash(ir),
      null,
      "preview_only",
      now,
    );
    this.audit("model_created", { model_id: mid, revision: rev });
    return { model_id: mid, revision: rev };
  }
  /** Rename keeps the id, revisions and grants; only the owner renames a project. */
  renameModel(p: Principal, model: string, name: string) {
    const { model: row, grant } = this.access.context(p, model, "model:edit");
    requireThat(
      !grant,
      "NEEDS_APPROVAL",
      "Only the project owner can rename a model.",
    );
    this.run("UPDATE models SET name=? WHERE id=?", name, model);
    this.audit("model_renamed", {
      model_id: model,
      previous_name: row.name,
      name,
    });
    return { previous_name: row.name as string, name };
  }
  /** The revision a fork copies: readable, and for a shared project only with the
   *  right to export it (a fork is a full copy; no silent sharing). */
  forkSource(p: Principal, model: string, revision?: string) {
    authorize(p, "model:create");
    const { grant } = this.access.context(p, model, "model:read");
    if (grant) this.access.context(p, model, "model:export");
    return { model: this.model(p, model), revision: this.revision(p, model, revision) };
  }
  /** Saved material overrides travel with a fork (after the SQL commit). */
  copyAppearance(source: string, target: string) {
    const from = join(this.root, "colours", `${source}.json`);
    if (!existsSync(from)) return false;
    this.afterCommit(() => {
      const record = JSON.parse(readFileSync(from, "utf8"));
      mkdirSync(join(this.root, "colours"), { recursive: true, mode: 0o700 });
      writeFileSync(
        join(this.root, "colours", `${target}.json`),
        JSON.stringify({ ...record, model_id: target, forked_from: source }, null, 1),
        { mode: 0o600 },
      );
    });
    return true;
  }
  close() {
    this.db.close();
    closeSync(this.lock);
    this.releaseLock();
  }
}
