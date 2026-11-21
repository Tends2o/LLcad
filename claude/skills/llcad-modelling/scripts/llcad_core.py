"""Shared plumbing for the LLcad skill: server access, job waiting, feature probes,
compact results, run contract, ledger and the patch linter.

Hooks import only this module (fast start, no fontTools/numpy). The DSL client
(llcad.py) builds on it. The access token is read from a file and never printed.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import sys
import time
import urllib.error
import urllib.request
from decimal import Decimal
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
DEFAULT_URL = "http://127.0.0.1:4310"
DEFAULT_TOKEN_FILE = "/root/projects/LLcad/data/local-token"
DEFAULT_ROOT = "/root/projects/LLcad"
TERMINAL = ("succeeded", "failed", "cancelled")
ASYNC_TOOLS = ("cad_apply_patch", "cad_validate", "cad_measure", "cad_render", "cad_export",
               "cad_rebuild", "cad_import", "cad_revert", "cad_solve_constraints")
MAX_OPS = 64

# ---------------------------------------------------------------- run contract


def find_run_dir(cwd: str | os.PathLike | None = None) -> Path | None:
    """The directory holding `.llcad/` (cwd or a parent up to 6 levels)."""
    p = Path(cwd or os.getcwd()).resolve()
    for _ in range(7):
        if (p / ".llcad").is_dir():
            return p
        if p.parent == p:
            break
        p = p.parent
    return None


def llcad_dir(cwd=None, create=False) -> Path | None:
    base = find_run_dir(cwd)
    if base is None and create:
        base = Path(cwd or os.getcwd()).resolve()
        (base / ".llcad").mkdir(parents=True, exist_ok=True)
    return base / ".llcad" if base else None


def load_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp%d" % os.getpid())
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    os.replace(tmp, path)


@contextlib.contextmanager
def locked(dirpath: Path, name="lock"):
    """Advisory lock for read-modify-write of .llcad state (hooks may run in parallel)."""
    dirpath.mkdir(parents=True, exist_ok=True)
    fh = open(dirpath / ("." + name), "a+")
    try:
        try:
            if os.name == "nt":
                # Windows: byte-range lock on the first byte (retries for about 10 s).
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(fh, fcntl.LOCK_EX)
        except Exception:
            pass
        yield
    finally:
        fh.close()


def load_run(cwd=None) -> dict | None:
    d = llcad_dir(cwd)
    return load_json(d / "run.json") if d else None


# ---------------------------------------------------------------- server access


def server_config(cwd=None) -> dict:
    run = load_run(cwd) or {}
    srv = run.get("server") or {}
    return {
        "url": (os.environ.get("LLCAD_URL") or srv.get("url") or DEFAULT_URL).rstrip("/"),
        "token_file": os.environ.get("LLCAD_TOKEN_FILE") or srv.get("token_file") or DEFAULT_TOKEN_FILE,
        "root": os.environ.get("LLCAD_ROOT") or srv.get("root") or DEFAULT_ROOT,
    }


_TOKEN_CACHE: dict = {}


def _token(cfg) -> str:
    tf = cfg["token_file"]
    if tf not in _TOKEN_CACHE:
        _TOKEN_CACHE[tf] = Path(tf).read_text().strip()
    return _TOKEN_CACHE[tf]


def failure(code, message, **details):
    return {"status": "failed", "errors": [{"code": code, "message": message, **({"details": details} if details else {})}]}


_STARTED = False


def start_service(cfg) -> bool:
    """On-demand hosts (Windows) run the service only while it is used: start it through
    the bridge next to the LLcad checkout, once per process. True if a start was tried."""
    global _STARTED
    bridge = Path(cfg["root"]) / "bin" / "llcad-bridge.mjs"
    if _STARTED or not bridge.is_file():
        return False
    _STARTED = True
    node = Path(cfg["root"]).parent / "tools" / "node" / ("node.exe" if os.name == "nt" else "node")
    try:
        import subprocess
        subprocess.run([str(node) if node.is_file() else "node", str(bridge), "start"], timeout=90,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception:
        return False
    return True


def call(tool: str, args: dict, timeout: float = 150, cfg: dict | None = None) -> dict:
    """POST /api/tools/<tool> (same dispatcher as MCP). Never raises; failures come back as
    {"status":"failed","errors":[...]}. A refused connection starts an on-demand service once."""
    cfg = cfg or server_config()
    resp = _call(tool, args, timeout, cfg)
    error = (resp.get("errors") or [{}])[0]
    if error.get("code") == "UNREACHABLE" and (error.get("details") or {}).get("refused") and start_service(cfg):
        resp = _call(tool, args, timeout, cfg)
    return resp


def _call(tool: str, args: dict, timeout: float, cfg: dict) -> dict:
    try:
        token = _token(cfg)
    except OSError as e:
        return failure("NO_TOKEN", "token file not readable: %s (%s)" % (cfg["token_file"], e.__class__.__name__))
    req = urllib.request.Request(
        cfg["url"] + "/api/tools/" + tool,
        data=json.dumps(args).encode(),
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode() or "{}")
            if isinstance(body, dict):
                body.setdefault("status", "failed")
                return body
        except Exception:
            pass
        return failure("HTTP_%d" % e.code, "HTTP error from LLcad")
    except Exception as e:  # URLError, timeout, reset
        refused = isinstance(e, ConnectionRefusedError) or isinstance(getattr(e, "reason", None), ConnectionRefusedError)
        return failure("UNREACHABLE", "%s: %s" % (e.__class__.__name__, str(e)[:200]), url=cfg["url"],
                       **({"refused": True} if refused else {}))


def first_error(resp: dict) -> dict | None:
    errs = (resp or {}).get("errors") or []
    if errs:
        return errs[0]
    err = (resp or {}).get("error")
    return err if isinstance(err, dict) else None


def is_unknown_field(resp: dict) -> bool:
    e = first_error(resp)
    if not e or e.get("code") != "INVALID_SCHEMA":
        return False
    issues = ((e.get("details") or {}).get("issues") or [])
    return any(i.get("code") == "unrecognized_keys" for i in issues)


def is_unknown_tool(resp: dict) -> bool:
    e = first_error(resp)
    if not e or e.get("code") != "INVALID_SCHEMA":
        return False
    msg = (e.get("message") or "").lower()
    return "unknown tool" in msg and not (e.get("details") or {}).get("issues")


# ---------------------------------------------------------------- feature probes

_PROBE_ID = "00000000-0000-4000-8000-000000000000"
PROBES = {
    # name: (tool, args) -> supported unless the server answers unrecognized_keys / unknown tool
    "job_wait_ms": ("cad_job_get", {"job_id": "job-" + _PROBE_ID, "wait_ms": 0}),
    "job_detail": ("cad_job_get", {"job_id": "job-" + _PROBE_ID, "detail": "brief"}),
    "apply_patch_ref": ("cad_apply_patch", {"model_id": "model-" + _PROBE_ID, "base_revision": "rev-" + _PROBE_ID,
                                            "idempotency_key": "llcad-skill-probe-000000", "patch_ref": {"path": "/nonexistent.json"}}),
    "apply_transaction": ("cad_apply_patch", {"model_id": "model-" + _PROBE_ID, "base_revision": "rev-" + _PROBE_ID,
                                              "idempotency_key": "llcad-skill-probe-000001", "commit": "if_valid",
                                              "operations": [{"op": "set_outputs", "outputs": ["probe"]}]}),
    "apply_wait_ms": ("cad_apply_patch", {"model_id": "model-" + _PROBE_ID, "base_revision": "rev-" + _PROBE_ID,
                                          "idempotency_key": "llcad-skill-probe-000002", "wait_ms": 0,
                                          "operations": [{"op": "set_outputs", "outputs": ["probe"]}]}),
    "tool_cad_gates": ("cad_gates", {"model_id": "model-" + _PROBE_ID}),
    "tool_cad_view": ("cad_view", {"model_id": "model-" + _PROBE_ID}),
    "tool_cad_context": ("cad_context", {"model_id": "model-" + _PROBE_ID}),
    "tool_cad_model_admin": ("cad_model_admin", {"action": "rename", "model_id": "model-" + _PROBE_ID,
                                                 "idempotency_key": "llcad-skill-probe-000003"}),
}


def _caps_cache_path(cfg) -> Path:
    key = hashlib.sha256(cfg["url"].encode()).hexdigest()[:10]
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "llcad-skill" / ("caps-%s.json" % key)


def capabilities(cfg=None, refresh=False) -> dict:
    """cad_capabilities (operators + hashes) plus probed optional features; cached per
    implementation_hash so a server rebuild invalidates it."""
    cfg = cfg or server_config()
    caps = call("cad_capabilities", {}, timeout=30, cfg=cfg)
    if caps.get("status") == "failed":
        return {"reachable": False, "error": first_error(caps)}
    impl = caps.get("implementation_hash") or caps.get("registry_hash")
    path = _caps_cache_path(cfg)
    cached = load_json(path, {}) or {}
    if not refresh and cached.get("implementation_hash") == impl and cached.get("probes"):
        probes = cached["probes"]
    else:
        probes = {}
        for name, (tool, args) in PROBES.items():
            r = call(tool, args, timeout=20, cfg=cfg)
            probes[name] = not (is_unknown_field(r) or is_unknown_tool(r))
        with contextlib.suppress(Exception):
            save_json(path, {"implementation_hash": impl, "probes": probes, "at": time.time()})
    ops = {o["name"]: o.get("parameters", []) for o in caps.get("operators", []) if isinstance(o, dict)}
    return {"reachable": True, "implementation_hash": impl, "registry_hash": caps.get("registry_hash"),
            "operators": ops, "limits": caps.get("limits") or {}, "probes": probes}


def probe_accepts(name: str, tool: str, args: dict, cfg=None) -> bool:
    """Does the RUNNING server accept these arguments' shape (no unrecognized key / unknown tool)?
    Probes use a non-existent model, so nothing is created; cached with the capability probes."""
    cfg = cfg or server_config()
    path = _caps_cache_path(cfg)
    cached = load_json(path, {}) or {}
    extra = cached.setdefault("extra", {})
    if name in extra:
        return extra[name]
    r = call(tool, args, timeout=20, cfg=cfg)
    if first_error(r) and first_error(r).get("code") == "UNREACHABLE":
        return False
    ok = not (is_unknown_field(r) or is_unknown_tool(r))
    extra[name] = ok
    with contextlib.suppress(Exception):
        save_json(path, cached)
    return ok


def probe_feature_field(field: str, feature: dict, cfg=None) -> bool:
    return probe_accepts("feature:" + field, "cad_plan_edit", {
        "model_id": "model-" + _PROBE_ID, "base_revision": "rev-" + _PROBE_ID, "idempotency_key": "llcad-skill-probe-feature",
        "operations": [{"op": "add_feature", "feature": feature}]}, cfg)


# ---------------------------------------------------------------- jobs


def job_id_of(resp: dict) -> str | None:
    if not isinstance(resp, dict):
        return None
    return resp.get("job_id") or ((resp.get("job") or {}).get("job_id") if isinstance(resp.get("job"), dict) else None)


def wait(job_id: str, budget_s: float = 170, cfg=None, use_wait_ms: bool | None = None) -> dict:
    """Wait until the job is terminal. Uses server-side wait_ms when available (one long call),
    else polls (the server already blocks up to LLCAD_JOB_WAIT_MS per call)."""
    cfg = cfg or server_config()
    deadline = time.time() + budget_s
    last = {}
    while True:
        left = deadline - time.time()
        if left <= 0:
            last = dict(last or {"job_id": job_id})
            last["hook_note"] = "wait budget exhausted after %.0f s; call cad_job_get again" % budget_s
            return last
        args = {"job_id": job_id}
        if use_wait_ms is not False:
            args["wait_ms"] = int(max(0, min(110000, (left - 5) * 1000)))
        r = call("cad_job_get", args, timeout=min(left + 30, 180), cfg=cfg)
        if use_wait_ms is None and is_unknown_field(r):
            use_wait_ms = False
            continue
        if r.get("status") == "failed" and r.get("job_id") is None and first_error(r):
            return r  # access/transport error, not a job state
        last = r
        if r.get("status") in TERMINAL:
            return r
        time.sleep(0.4 if use_wait_ms is False else 0.1)


# ---------------------------------------------------------------- gates


def run_gates(model_id, revision, cfg=None, limit=6):
    """cad_gates (deterministic floor + ranked residuals) or, on older servers, cad_get_model quality.
    Never returns a 'done' verdict: floor_passed is a floor, not a finish."""
    r = call("cad_gates", {"model_id": model_id, "revision": revision}, timeout=200, cfg=cfg)
    if is_unknown_tool(r):
        g = call("cad_get_model", {"model_id": model_id, "revision": revision, "limit": 1}, cfg=cfg)
        m = g.get("measurements") or {}
        return {"source": "cad_get_model", "quality": g.get("quality"), "floor_passed": g.get("quality") == "checks_passed_within_profile",
                "bounds": [round(x, 2) for x in m.get("bounds", [])], "features": g.get("feature_count"),
                "reference": (g.get("reference_validation") or {}).get("status")}
    if r.get("job_id") and r.get("status") in ("queued", "running"):
        j = wait(r["job_id"], 170, cfg=cfg)
        r = j.get("result") if isinstance(j.get("result"), dict) else j
    if r.get("status") == "failed" and first_error(r):
        e = first_error(r)
        return {"source": "cad_gates", "error": {"code": e.get("code"), "message": str(e.get("message"))[:200]}}
    res = r.get("residuals") or []
    top = []
    for x in res[:limit]:
        top.append({k: x.get(k) for k in ("check", "target", "measured", "unit", "severity", "where") if x.get(k) is not None})
    return {"source": "cad_gates", "floor_passed": r.get("floor_passed"), "residuals": len(res), "top": top}


# ---------------------------------------------------------------- compact results

HINTS = {
    "INVALID_SCHEMA": "Field names/types: see reference/operators.md; numbers as decimal strings; <=64 ops; lint with llcad.py lint.",
    "STALE_REVISION": "base_revision or expected_hash is outdated: read cad_get_model (head_revision, structure.hash) and rebuild the patch.",
    "IDEMPOTENCY_CONFLICT": "Key reused with different arguments: use a fresh idempotency_key per attempt.",
    "GEOMETRY_INVALID": "Check CCW profiles, equal loft segment counts, cutters that actually intersect, near-tangent cuts (offset >=0.5 mm).",
    "PRECISION_UNSUPPORTED": "Kernel tolerance above model budget: avoid near-tangent booleans/fillets on compounds, or use tolerance preset visual_reconstruction if the run allows it.",
    "KERNEL_FAILURE": "Worker died (CPU/memory): split the patch, coarser tessellation (0.12-0.18 mm), fewer glyph features per patch.",
    "BUILD_MISMATCH": "Candidate from another build: cad_rebuild the model to the current registry, then re-apply.",
    "BUDGET_EXCEEDED": "Too big: split into smaller patches, coarser deflection, fewer instances.",
    "AMBIGUOUS_SELECTION": "Selector matched nothing/too much: narrow edge_selector or use between_features.",
    "VALIDATION_REQUIRED": "Commit needs the digest from a passing cad_validate on this transaction.",
    "OUT_OF_SCOPE": "Operation not allowed here (scope/representation); check part ownership and model access.",
    "CONSTRAINT_CONFLICT": "Duplicate id or wrong state; use unique feature ids with a part prefix.",
    "UNREACHABLE": "LLcad HTTP endpoint not reachable; check LLCAD_URL / service.",
}
MESSAGE_HINTS = [
    ("different segment counts", "Loft sections need equal segment counts (same number of profile points per station)."),
    ("cuts no material", "Cutter misses the body: hole/pocket/groove cut DOWN from z; move z to the top face."),
    ("profile is degenerate", "Profile needs >=3 distinct coplanar points in CCW order."),
    ("falls apart", "Strip paths/pads must form one connected region in one z plane."),
    ("shell opening", "shell opens exactly one planar top/bottom face; the body needs a single flat face there."),
    ("Part outputs must be unique", "Part outputs must be features owned by that part (owner_part)."),
    ("Model outputs must be registered", "Every model output must be listed in some part's outputs."),
    ("feature frame must lie", "Feature local_frame must lie inside its part's frame."),
    ("source state changed", "LLcad sources changed while the service runs (deployment in progress): retry after the service restart with a new idempotency_key; not a model error."),
    ("original geometry build", "The head revision was built by an older LLcad build: cad_rebuild (plan, then candidate) to target_registry_hash, validate, commit, then continue."),
    ("Structure units need unique ids", "Structure ids (project, frames, assemblies, parts) must be unique, not 'world', and differ from every feature id (e.g. parts 'pt-grip', features 'p01-...')."),
    ("negative", "Clockwise profile -> negative volume: reverse the point order (CCW seen from +Z)."),
]


def hint_for(err: dict | None) -> str | None:
    if not err:
        return None
    msg = str(err.get("message") or "")
    for needle, hint in MESSAGE_HINTS:
        if needle.lower() in msg.lower():
            return hint
    return HINTS.get(err.get("code"))


def _round(v, nd=4):
    if isinstance(v, float):
        return round(v, nd)
    if isinstance(v, list):
        return [_round(x, nd) for x in v]
    return v


def failed_checks(result: dict, limit=8) -> list:
    out = []
    for c in (result or {}).get("checks") or []:
        if isinstance(c, dict) and c.get("status") in ("failed", "violated", "error"):
            out.append({k: _round(c.get(k)) for k in ("check_id", "measured", "requested", "message") if c.get(k) is not None})
    return out[:limit]


def compact_job(job: dict, limit_chars=1400) -> dict:
    """Terse terminal job result: ids, status, key measurements, failed checks, error + fix hint."""
    if not isinstance(job, dict):
        return {"status": "unknown"}
    out = {"status": job.get("status")}
    for k in ("job_id", "model_id", "transaction_id", "phase"):
        if job.get(k):
            out[k] = job[k]
    elapsed = job.get("elapsed_s") if job.get("elapsed_s") is not None else job.get("elapsed_seconds")
    if elapsed is not None:  # brief job views say elapsed_s, full ones elapsed_seconds
        out["elapsed_s"] = _round(elapsed, 2)
    r = job.get("result") if isinstance(job.get("result"), dict) else {}
    if r:
        rr = {}
        for k in ("status", "candidate_revision", "revision", "transaction_id", "validation_status", "profile",
                  "digest", "check_count", "committed", "quality", "artifact_id", "format"):
            if r.get(k) not in (None, [], {}):
                rr[k] = r[k]
        m = r.get("measurements")
        if isinstance(m, dict):
            rr["measurements"] = {k: _round(m[k], 3) for k in ("bounds", "volume", "solids", "faces", "valid") if k in m}
        if r.get("changed_features"):
            rr["changed_features"] = len(r["changed_features"])
        fc = failed_checks(r)
        if fc:
            rr["failed_checks"] = fc
        if r.get("warnings"):
            rr["warnings"] = [str(w)[:160] for w in r["warnings"][:4]]
        arts = r.get("artifacts") or job.get("artifacts") or []
        if arts:
            rr["artifacts"] = [a.get("artifact_id") or a.get("id") if isinstance(a, dict) else a for a in arts[:6]]
        if r.get("gates"):
            rr["gates"] = r["gates"]
        out["result"] = rr
    if job.get("warnings"):
        out["warnings"] = [str(w)[:160] for w in job["warnings"][:4]]
    err = job.get("error") if isinstance(job.get("error"), dict) else first_error(job) if job.get("status") == "failed" else None
    if err:
        out["error"] = {"code": err.get("code"), "message": str(err.get("message", ""))[:300]}
        h = hint_for(err)
        if h:
            out["error"]["hint"] = h
    nxt = next_action(out)
    if nxt:
        out["next"] = nxt
    if job.get("hook_note"):
        out["note"] = job["hook_note"]
    s = json.dumps(out, ensure_ascii=False)
    if len(s) > limit_chars and "result" in out:
        out["result"].pop("warnings", None)
        if len(json.dumps(out, ensure_ascii=False)) > limit_chars:
            out["result"].pop("failed_checks", None)
            out["truncated"] = True
    return out


def next_action(c: dict) -> str | None:
    r = c.get("result") or {}
    if c.get("status") == "failed":
        return "fix the error, then re-apply with a new idempotency_key"
    st = r.get("status")
    if st == "candidate_ready":
        return "cad_validate transaction_id=%s" % (c.get("transaction_id") or r.get("transaction_id"))
    if st == "checks_passed_within_profile" and r.get("digest") and not r.get("revision"):
        return "cad_commit transaction_id=%s validation_digest=%s" % (c.get("transaction_id"), r["digest"])
    if st in ("validation_failed", "checks_failed"):
        return "fix failed_checks (or discard the candidate)"
    return None


# ---------------------------------------------------------------- MCP tool_response parsing


def parse_tool_response(tr):
    """Claude Code gives MCP results as a list of content blocks, a string or a dict. Return the
    LLcad JSON payload (dict) or None."""
    if tr is None:
        return None
    if isinstance(tr, dict):
        if "status" in tr and ("job_id" in tr or "errors" in tr or "result_schema_version" in tr):
            return tr
        if isinstance(tr.get("structuredContent"), dict):
            return tr["structuredContent"]
        if "content" in tr:
            return parse_tool_response(tr["content"])
        return tr
    if isinstance(tr, list):
        for block in tr:
            if isinstance(block, dict) and block.get("type") == "text":
                p = parse_tool_response(block.get("text"))
                if isinstance(p, dict):
                    return p
        return None
    if isinstance(tr, str):
        s = tr.strip()
        if s.startswith("{"):
            with contextlib.suppress(Exception):
                return json.loads(s)
        return None
    return None


def shaped_like(original, payload: dict):
    """Return `payload` in the same container shape as the original tool_response."""
    text = json.dumps(payload, ensure_ascii=False)
    if isinstance(original, list):
        return [{"type": "text", "text": text}]
    if isinstance(original, str):
        return text
    if isinstance(original, dict) and "content" in original:
        out = {"content": [{"type": "text", "text": text}]}
        if "structuredContent" in original:
            out["structuredContent"] = payload
        return out
    return payload


# ---------------------------------------------------------------- hook I/O


def read_hook_input() -> dict:
    try:
        return json.load(sys.stdin)
    except Exception:
        return {}


def emit(obj: dict):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False))
    sys.stdout.flush()


def hook_log(cwd, record: dict):
    d = llcad_dir(cwd)
    if not d:
        return
    with contextlib.suppress(Exception):
        with open(d / "hook-log.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"t": round(time.time(), 1), **record}, ensure_ascii=False)[:2000] + "\n")


def short_tool(name: str) -> str:
    return name.split("__")[-1] if name else ""


# ---------------------------------------------------------------- ledger


def ledger_path(cwd=None, create=False) -> Path | None:
    d = llcad_dir(cwd, create=create)
    return d / "ledger.json" if d else None


def ledger_update(cwd, fn):
    """Apply fn(ledger)->None under a lock. No-op when no .llcad directory exists."""
    d = llcad_dir(cwd)
    if not d:
        return None
    with locked(d, "ledger.lock"):
        led = load_json(d / "ledger.json", None) or new_ledger()
        fn(led)
        led["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        save_json(d / "ledger.json", led)
        return led


def new_ledger(model_id=None):
    return {"version": 1, "model_id": model_id, "head": None, "items": [], "transactions": {}, "gates": None, "events": []}


def ledger_event(led: dict, text: str):
    ev = led.setdefault("events", [])
    ev.append(time.strftime("%H:%M:%S ") + text[:200])
    del ev[:-30]


def track_transaction(cwd, payload: dict, state: str, base_revision=None):
    tx = (payload or {}).get("transaction_id")
    if not tx:
        return

    def fn(led):
        t = led.setdefault("transactions", {}).setdefault(tx, {})
        t.update({k: v for k, v in {"state": state, "base_revision": base_revision or t.get("base_revision"),
                                    "candidate_revision": payload.get("candidate_revision") or (payload.get("result") or {}).get("candidate_revision") or t.get("candidate_revision"),
                                    "model_id": payload.get("model_id") or t.get("model_id"),
                                    "at": time.strftime("%H:%M:%S")}.items() if v})
        if payload.get("model_id") and not led.get("model_id"):
            led["model_id"] = payload["model_id"]
        rev = payload.get("revision") or (payload.get("result") or {}).get("revision")
        if state == "committed" and rev:
            led["head"] = rev
            t["revision"] = rev
        ledger_event(led, "%s %s" % (state, tx[:15]))
    ledger_update(cwd, fn)


def open_transactions(led: dict) -> list:
    return [(k, v) for k, v in (led or {}).get("transactions", {}).items()
            if v.get("state") not in ("committed", "discarded", "failed", "superseded")]


def ledger_summary(led: dict, run: dict | None = None, limit=1500) -> str:
    if not led:
        return ""
    lines = ["LLcad ledger: model %s head %s" % (led.get("model_id"), led.get("head"))]
    if run:
        el = elapsed_min(run)
        if el is not None:
            lines.append("time: elapsed %.0f min / budget %s min" % (el, run.get("budget_min")))
    items = led.get("items") or []
    if items:
        by = {}
        for it in items:
            by.setdefault(it.get("status", "open"), []).append(it)
        lines.append("items: " + ", ".join("%s %d" % (k, len(v)) for k, v in sorted(by.items())))
        for it in [i for i in items if i.get("status") not in ("verified", "waived")][:12]:
            lines.append("- %s [%s] %s: %s" % (it.get("id"), it.get("priority", "P1"), it.get("part", ""), str(it.get("acceptance", ""))[:90]))
    ot = open_transactions(led)
    if ot:
        lines.append("open candidates: " + ", ".join(k for k, _ in ot[:5]))
    g = led.get("gates")
    if g:
        top = "; ".join(g.get("top", [])[:3])
        lines.append("gates @%s: floor_passed=%s%s" % (g.get("revision"), g.get("floor_passed"), ("; top: " + top) if top else ""))
    ev = led.get("events") or []
    if ev:
        lines.append("recent: " + " | ".join(ev[-5:]))
    s = "\n".join(lines)
    return s[:limit]


def elapsed_min(run: dict) -> float | None:
    st = (run or {}).get("started_at")
    if not st:
        return None
    try:
        if isinstance(st, (int, float)):
            t0 = float(st)
        else:
            import datetime as dt
            t0 = dt.datetime.fromisoformat(str(st).replace("Z", "+00:00")).timestamp()
        return (time.time() - t0) / 60
    except Exception:
        return None


# ---------------------------------------------------------------- numbers and hashes


def num(v) -> str:
    """Decimal string without exponent, as the IR requires ("-0" -> "0")."""
    if isinstance(v, str):
        return v
    if isinstance(v, bool):
        raise TypeError("boolean is not a number")
    if isinstance(v, float):
        if not math.isfinite(v):
            raise ValueError("non-finite number %r" % v)
        v = round(v, 9)
    d = Decimal(str(v)).normalize()
    s = format(d, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    if s in ("-0", ""):
        s = "0"
    return s


def canonical(value) -> str:
    """Same canonical JSON as packages/semantic-ir/hash.ts (sorted keys, JSON.stringify leaves)."""
    if isinstance(value, list):
        return "[" + ",".join(canonical(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{" + ",".join(json.dumps(k, ensure_ascii=False) + ":" + canonical(v)
                              for k, v in sorted(value.items()) if v is not None) + "}"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def sha(value) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def idem_key(prefix: str, payload) -> str:
    clean = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in prefix)[:80] or "llcad"
    return (clean + "-" + sha(payload)[:16])[:128]


def _keys_path() -> Path:
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "llcad-skill" / "keys.json"


def attempt_key(key: str) -> str:
    """Content-derived keys replay the previous response. After a failed attempt a fresh suffix is
    used, so a retry really runs again (a successful key is kept: an exact replay is harmless)."""
    reg = load_json(_keys_path(), {}) or {}
    n = (reg.get(key) or {}).get("failed", 0)
    return key if n == 0 else ("%s-r%d" % (key[:120], n))


def record_key(key: str, ok: bool):
    with contextlib.suppress(Exception):
        path = _keys_path()
        with locked(path.parent, "keys.lock"):
            reg = load_json(path, {}) or {}
            base = key.rsplit("-r", 1)[0] if "-r" in key[-4:] else key
            e = reg.setdefault(base, {"failed": 0})
            if not ok:
                e["failed"] = e.get("failed", 0) + 1
            e["t"] = int(time.time())
            if len(reg) > 2000:
                for k in sorted(reg, key=lambda k: reg[k].get("t", 0))[:500]:
                    reg.pop(k, None)
            save_json(path, reg)


# ---------------------------------------------------------------- patch lint (shared by DSL and PreToolUse hook)

CURVE_OPS = {"profile", "circle", "line", "arc", "bezier", "bspline", "nurbs_curve", "point", "plane", "sketch"}
SOLID_BUILDERS = {"extrude", "revolve", "loft", "sweep"}


def _f(v):
    try:
        return float(v)
    except Exception:
        return None


def newell_normal(points):
    nx = ny = nz = 0.0
    n = len(points)
    for i in range(n):
        x1, y1, z1 = points[i]
        x2, y2, z2 = points[(i + 1) % n]
        nx += (y1 - y2) * (z1 + z2)
        ny += (z1 - z2) * (x1 + x2)
        nz += (x1 - x2) * (y1 + y2)
    return nx, ny, nz


def signed_area_xy(points) -> float:
    a = 0.0
    n = len(points)
    for i in range(n):
        x1, y1 = points[i][0], points[i][1]
        x2, y2 = points[(i + 1) % n][0], points[(i + 1) % n][1]
        a += x1 * y2 - x2 * y1
    return a / 2


def profile_issue(points) -> tuple[str, str] | None:
    """('error'|'warn', text) for a profile point list, or None."""
    pts = []
    for p in points or []:
        c = [_f(x) for x in p] if isinstance(p, (list, tuple)) and len(p) == 3 else None
        if not c or None in c:
            return ("error", "profile point %r is not [x,y,z] decimal strings" % (p,))
        pts.append(c)
    if len(pts) < 3:
        return ("error", "profile needs >= 3 points")
    if pts[0] == pts[-1]:
        return ("warn", "profile repeats its first point at the end (it is closed automatically)")
    nx, ny, nz = newell_normal(pts)
    mag = math.sqrt(nx * nx + ny * ny + nz * nz)
    if mag < 1e-12:
        return ("error", "profile is degenerate (zero area)")
    # planarity
    cx = sum(p[0] for p in pts) / len(pts)
    cy = sum(p[1] for p in pts) / len(pts)
    cz = sum(p[2] for p in pts) / len(pts)
    ux, uy, uz = nx / mag, ny / mag, nz / mag
    span = max(max(p[i] for p in pts) - min(p[i] for p in pts) for i in range(3)) or 1
    dev = max(abs((p[0] - cx) * ux + (p[1] - cy) * uy + (p[2] - cz) * uz) for p in pts)
    if dev > max(1e-6, span * 1e-7):
        return ("error", "profile points are not coplanar (max off-plane %.4g mm)" % dev)
    if abs(uz) > 0.999 and uz < 0:
        return ("cw", "profile is clockwise seen from +Z (signed area %.4g mm2): reverse the point order" % signed_area_xy(pts))
    return None


def _feature_edge_count(f: dict) -> int | None:
    c = f.get("construction") or {}
    op = c.get("operator")
    if op == "profile":
        return len(c.get("points") or [])
    if op == "circle":
        return 1
    if op == "sketch":
        segs = c.get("segments")
        return len(segs) if isinstance(segs, list) else None
    return None


def lint_operations(ops: list, cw_policy: str = "error", prefix: str | None = None) -> dict:
    """Pre-submit checks. Returns {"errors": [...], "warnings": [...]}; errors are deterministic
    reasons a server call would fail or build wrong geometry."""
    errors, warnings = [], []
    if not isinstance(ops, list):
        return {"errors": ["operations must be a list"], "warnings": []}
    if len(ops) > MAX_OPS:
        errors.append("%d operations > %d: split into %d patches (<=%d ops each; llcad.py splits automatically)"
                      % (len(ops), MAX_OPS, math.ceil(len(ops) / MAX_OPS), MAX_OPS))
    feats = {}
    extrude_inputs = set()
    for op in ops:
        if isinstance(op, dict) and op.get("op") == "add_feature" and isinstance(op.get("feature"), dict):
            f = op["feature"]
            feats[f.get("id")] = f
            c = f.get("construction") or {}
            if c.get("operator") == "extrude":
                extrude_inputs.update(f.get("depends_on") or [])
    seen_ids = set()
    for i, op in enumerate(ops):
        if not isinstance(op, dict):
            errors.append("op %d is not an object" % i)
            continue
        kind = op.get("op")
        if kind == "set_structure" and not op.get("expected_hash"):
            errors.append("op %d set_structure without expected_hash: read structure.hash via cad_get_model/cad_structure" % i)
        if kind in ("remove_feature", "set_construction", "set_feature_dependencies", "set_feature_context") and not op.get("expected_hash"):
            errors.append("op %d %s without expected_hash" % (i, kind))
        if kind != "add_feature":
            continue
        f = op.get("feature") or {}
        fid = f.get("id")
        if fid in seen_ids:
            errors.append("duplicate feature id %s" % fid)
        seen_ids.add(fid)
        if prefix and fid and not str(fid).startswith(prefix):
            errors.append("feature %s outside this lane's id prefix %r" % (fid, prefix))
        c = f.get("construction") or {}
        oper = c.get("operator")
        if f.get("kind") and oper and f["kind"] != oper:
            warnings.append("feature %s kind %r != construction.operator %r" % (fid, f["kind"], oper))
        for pname, q in (f.get("parameters") or {}).items():
            if isinstance(q, dict) and not isinstance(q.get("value"), str):
                warnings.append("feature %s parameter %s value is not a decimal string (old servers reject numbers)" % (fid, pname))
        if oper == "profile":
            iss = profile_issue(c.get("points"))
            if iss:
                level, text = iss
                if level == "cw":
                    consumer = fid in extrude_inputs
                    msg = "feature %s: %s" % (fid, text)
                    if cw_policy == "error" and consumer:
                        errors.append(msg)
                    else:
                        warnings.append(msg + ("" if consumer else " (not extruded in this patch; check if it feeds an extrude)"))
                elif level == "error":
                    errors.append("feature %s: %s" % (fid, text))
                else:
                    warnings.append("feature %s: %s" % (fid, text))
        if oper == "strip":
            zs = {str(pt[2]) for path in (c.get("paths") or []) for pt in path if isinstance(pt, (list, tuple)) and len(pt) == 3}
            if len(zs) > 1:
                errors.append("feature %s: strip paths must share one z plane (found %s)" % (fid, sorted(zs)[:3]))
        if oper == "loft":
            counts = {}
            for d in f.get("depends_on") or []:
                if d in feats:
                    n = _feature_edge_count(feats[d])
                    if n is not None:
                        counts[d] = n
            if len(set(counts.values())) > 1 and c.get("check_compatibility", True):
                errors.append("feature %s: loft sections have unequal segment counts %s; resample every station to the same point count"
                              % (fid, dict(list(counts.items())[:6])))
        if oper in ("union", "difference", "intersection") and len(f.get("depends_on") or []) > 16:
            errors.append("feature %s: %s with %d inputs > 16; stage as a tree" % (fid, oper, len(f["depends_on"])))
    for i, op in enumerate(ops):
        if isinstance(op, dict) and op.get("op") == "set_structure" and isinstance(op.get("structure"), dict):
            st = op["structure"]
            sids = [(st.get("project") or {}).get("id")] + [e.get("id") for key in ("frames", "assemblies", "parts") for e in st.get(key) or []]
            dup = sorted({x for x in sids if x and (sids.count(x) > 1 or x == "world" or x in feats)})
            if dup:
                errors.append("op %d set_structure: ids %s duplicate each other, use 'world' or equal feature ids "
                              "(use e.g. parts 'pt-...', features 'p01-...')" % (i, dup[:6]))
    for op in ops:
        if isinstance(op, dict) and op.get("op") == "set_outputs":
            for o in op.get("outputs") or []:
                f = feats.get(o)
                if f and (f.get("construction") or {}).get("operator") in CURVE_OPS:
                    errors.append("output %s is a curve/sketch (%s); outputs must be solids (extrude/revolve it)"
                                  % (o, f["construction"]["operator"]))
    return {"errors": errors, "warnings": warnings}


def normalise_numbers(obj, key=None):
    """Numbers inside Quantity objects and point lists -> decimal strings (safe on old and new servers).
    Returns (new_obj, changed_count)."""
    changed = 0
    POINT_KEYS = {"points", "poles", "paths", "origin", "axis", "translation", "matrix", "center", "start", "end",
                  "knots", "weights", "min", "max", "normal", "direction", "point", "x_axis", "z_axis"}

    def conv(v, k):
        nonlocal changed
        if isinstance(v, dict):
            if set(v.keys()) == {"value", "unit"} and isinstance(v["value"], (int, float)) and not isinstance(v["value"], bool):
                changed += 1
                return {"value": num(v["value"]), "unit": v["unit"]}
            return {kk: conv(vv, kk) for kk, vv in v.items()}
        if isinstance(v, list):
            if k in POINT_KEYS:
                out = []
                for x in v:
                    if isinstance(x, (int, float)) and not isinstance(x, bool):
                        changed += 1
                        out.append(num(x))
                    else:
                        out.append(conv(x, k))
                return out
            return [conv(x, k) for x in v]
        return v

    return conv(obj, key), changed
