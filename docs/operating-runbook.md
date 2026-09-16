# Operations

## Local operation

`npm start` runs the built HTTP server on `127.0.0.1:4310`; `npm run dev` runs the
TypeScript sources directly. Data lives in `data/` by default. The local key is written with
file mode 0600 to `data/local-token`; it belongs neither in the repository nor in logs. New
keys are created by stopping the service, replacing the key file and starting again; existing
browser cookies then become invalid.

`MATHFORGE_DATA`, `HOST` and `PORT` are configurable. Local authentication may only listen
on loopback. A data directory is opened exclusively through a `flock` lock held for the
lifetime of the process; a second server, the demo or an offline maintenance command receives
`STORE_BUSY`. Stop the service before offline maintenance; after a crash the operating system
releases the lock. The file `.store.lock` stays on purpose and must not be replaced or deleted
while the service runs. Database schema versions 1 and 2 are extended additively to version 3
on open (durable scheduler order and face bindings); unknown versions are rejected unchanged.

For a single agent, `deployment/start-mcp.sh` is the stdio entry point. The client starts and
stops the process; there is no browser login and no network listener unless the agent calls
`cad_viewer_open`, which binds a loopback-only viewer for the lifetime of the process or until
`cad_viewer_close`. The same private local user as in local HTTP mode is used. Only trusted
local processes may start this entry point; a remote HTTP connection still needs the existing
authentication.

Face history is stored with every B-Rep as a private hashed blob. Cache reuse checks the
fingerprints of all faces. Backup, restore, GC and tenant erasure cover these blobs through
the same revision and cache references.

The job queue is durable and round-robin between waiting users, first-in-first-out within a
user. Scheduler state survives a restart. By default one isolated worker runs at a time
(`MATHFORGE_WORKERS` 1–4, `MATHFORGE_WARM_WORKERS` 0–2 for the pre-warmed pool); no runtime
fairness between jobs of different length is promised.

## Systemd

`deployment/llcad-local.service` is a template for the local HTTP mode (bearer key on
loopback), `deployment/mathforge.service` with `deployment/environment.example` for the
remote OAuth mode. Both limit memory, tasks, write paths and privileges. Adjust
`WorkingDirectory`, `ExecStart` and the data path to your installation.

## Preparing a remote deployment

1. Install the code and locked dependencies (for example under `/opt/mathforge`), create a
   dedicated user and a private data directory.
2. Copy `deployment/environment.example` and replace every example address with real values.
   Never store real keys in the repository.
3. Set `MATHFORGE_AUTH=oauth`. The identity provider must issue signed tokens with `sub`,
   `tenant_id`, `scope`, `iat`, `exp` and exactly the configured audience. JWKS and issuer are
   fixed HTTPS addresses.
4. Adapt the systemd template to the real Node installation.
5. Configure the HTTPS proxy from `deployment/Caddyfile` with your own domain. It must keep
   the original public host; the gateway port stays bound to loopback.
6. Verify Bubblewrap under the real service account. AppArmor or user-namespace policies of
   the target machine can block the start. Do not disable them globally without review; the
   service fails closed.
7. Run `npm run verify`, the benchmarks and the real host test before approving the setup.

## Backup and restore

```bash
npx tsx scripts/maintenance.ts backup data /safe/path/backup-2026-09-13
npx tsx scripts/maintenance.ts restore /safe/path/backup-2026-09-13 /new/data/directory
```

Both targets must be new. Stop the service first. The database is copied with the SQLite
backup API. All blobs are checked against manifest and file size before a restore and the
database must pass `PRAGMA integrity_check`. The source directory is never overwritten. The
automated restore test builds a real second database, reads the original revision from it,
checks its blobs and rejects a deliberately corrupted backup blob.

## Failures

With `MATHFORGE_DEBUG_ERRORS=1` the service prints the internal exception and the last 2000
characters of the native worker's stderr for failed jobs. This diagnostic is never stored and
never returned to clients because it may contain imported data.

- **Worker terminated:** the job status carries a safe diagnosis; the authoritative revision
  is kept. Every worker has its own process group that is cleaned up on cancel, timeout and
  exit. A crash during the sandbox start must not keep an inherited stderr channel open. A
  lost process with a valid lease is retried after the lease expires.
- **Job takes too long:** `cad_job_cancel` stops the process. A budget error never changes a
  tolerance; choose a smaller domain or fewer instances explicitly.
- **Stale revision:** read the current revision, plan the change again and validate a new
  candidate. There is no silent rebase.
- **`BUILD_MISMATCH`:** the stored candidate or revision belongs to another compiler or worker
  build. For a construction that still compiles, call `cad_rebuild` in plan mode with the
  current registry hash, then compute an explicit candidate, compare measurements, validate
  and commit it as a new revision. Old facts and IR stay. Discard stale uncommitted candidates
  and plan again from the current base. A construction that no longer compiles needs a
  separate schema or operator migration. A running server also reports `BUILD_MISMATCH` when
  its sources changed underneath it; restart it with the verified build.
- **Validation failed:** read the report through `cad://transactions/{id}/validation`, fix or
  discard the candidate; never send invented check fields.
- **`SANDBOX_UNAVAILABLE`:** examine Bubblewrap, user namespaces and bind-mount rights under
  the service account. Do not enable an unsandboxed fallback.
- **OAuth rejected:** check issuer, audience, time, scopes and signed tenant claims. Do not
  log tokens while debugging.
- **Commit outbox blocked:** repair the mandatory hook and keep the service running. The
  committed revision stays valid; the outbox is retried.

## Retention

```bash
npx tsx scripts/maintenance.ts gc data
```

GC is an offline operator command. It deletes only unreferenced blobs older than seven days;
current revisions, candidates, cache references and export artifacts stay.

Tenant-scoped erasure of the active store, after stopping the service:

```bash
npx tsx scripts/maintenance.ts erase-tenant data TENANT
npx tsx scripts/maintenance.ts erase-tenant data TENANT --apply
```

The first call lists the affected object counts; the second performs the erasure. Removed
are models, revisions, candidates, jobs, selections, uploads, exports, cache entries,
idempotency records, scheduler state and associated outbox events, followed by all orphaned
blobs; content still referenced by other tenants stays. SQLite runs `secure_delete`, a WAL
checkpoint and `VACUUM`. Associated audit events are redacted; the order and original times
of the remaining events stay and their hash chain is rebuilt. The event
`retention_redaction` records the previous chain end and the number of removed events; take
this explicit break into account for externally stored audit anchors.

**Backups, file-system snapshots and already downloaded copies are outside this command.**
The operator must include them in the erasure inventory, remove them after the applicable
retention period and block restores until then. No forensic erasure guarantee is given for
SSDs or external storage services.

## Release manifest

`npm run release:check` writes `reports/release-manifest.json`. Exit code 2 means open
acceptance criteria. Current build, registry and dependency hashes, tests and benchmark must
match; an additional implementation digest binds the evidence to gateway, viewer, jobs, tests
and deployment code. A missing target-host test or a missing HTTPS and identity-provider
configuration is never replaced by a placeholder success.
