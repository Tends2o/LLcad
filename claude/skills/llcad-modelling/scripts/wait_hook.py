#!/usr/bin/env python3
"""PostToolUse hook for asynchronous LLcad tools (and cad_job_get).

If the tool answered `queued`/`running` with a job_id, poll cad_job_get over HTTP (server-side
wait_ms when available) for up to LLCAD_HOOK_WAIT_S (default 100 s) and replace the tool output
with the compact terminal result, so the model never spends turns polling. The job id stays in
the replacement for audit. Optional (run.json hooks.auto_validate): after a ready candidate the
hook also runs cad_validate and hands back the digest for cad_commit.

Pass-through (no output, exit 0) on anything unexpected: never hides a failure, never blocks.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from llcad_core import (ASYNC_TOOLS, TERMINAL, call, compact_job, emit, hook_log, idem_key, load_run,  # noqa: E402
                        parse_tool_response, read_hook_input, record_key, server_config, shaped_like, short_tool,
                        track_transaction, wait)

TX_TOOLS = ("cad_apply_patch", "cad_import", "cad_revert", "cad_rebuild")


def main():
    inp = read_hook_input()
    tool = short_tool(inp.get("tool_name", ""))
    if tool not in ASYNC_TOOLS and tool != "cad_job_get":
        return
    original = inp.get("tool_response")
    payload = parse_tool_response(original)
    if not isinstance(payload, dict):
        return
    cwd = inp.get("cwd")
    args = inp.get("tool_input") or {}
    if tool in TX_TOOLS and payload.get("transaction_id"):
        track_transaction(cwd, payload, "committed" if payload.get("revision") else "candidate", args.get("base_revision"))
    if payload.get("status") == "failed" and args.get("idempotency_key"):
        record_key(args["idempotency_key"], False)
    job_id = payload.get("job_id")
    if payload.get("status") not in ("queued", "running") or not job_id:
        return
    cfg = server_config(cwd)
    budget = float(os.environ.get("LLCAD_HOOK_WAIT_S", "100"))
    if tool == "cad_apply_patch" and (args.get("validate") or args.get("commit") == "if_valid"):
        # One-call transaction still running: the IDENTICAL call resumes it (evaluate -> validate
        # -> commit); waiting on the evaluate job alone would stop at candidate_ready.
        deadline = time.time() + budget
        r = payload
        while r.get("status") in ("queued", "running") and time.time() < deadline:
            r = call("cad_apply_patch", {**args, "wait_ms": int(max(1000, min(100000, (deadline - time.time() - 5) * 1000)))},
                     timeout=max(10, deadline - time.time() + 30), cfg=cfg)
        if r.get("status") in ("queued", "running"):
            emit({"hookSpecificOutput": {"hookEventName": "PostToolUse",
                                         "additionalContext": "LLcad transaction still running after %.0f s (hook waited); repeat the identical cad_apply_patch call once." % budget}})
            return
        if r.get("transaction_id"):
            track_transaction(cwd, r, "committed" if r.get("revision") else "candidate", args.get("base_revision"))
        r = dict(r, waited_by_hook=True)
        field = os.environ.get("LLCAD_HOOK_OUTPUT_FIELD", "updatedMCPToolOutput")
        hook_log(cwd, {"hook": "wait", "tool": tool, "job": job_id, "status": r.get("status")})
        emit({"hookSpecificOutput": {"hookEventName": "PostToolUse", field: shaped_like(original, r)}})
        return
    job = wait(job_id, budget, cfg)
    if not isinstance(job, dict) or job.get("status") not in TERMINAL:
        emit({"hookSpecificOutput": {"hookEventName": "PostToolUse",
                                     "additionalContext": "LLcad job %s still running after %.0f s (hook waited); call cad_job_get once." % (job_id, budget)}})
        return
    result = compact_job(job)
    result["waited_by_hook"] = True
    if job.get("status") != "succeeded" and args.get("idempotency_key"):
        record_key(args["idempotency_key"], False)
    if tool in TX_TOOLS and payload.get("transaction_id"):
        state = "failed" if job.get("status") != "succeeded" else ("committed" if (job.get("result") or {}).get("revision") else "candidate")
        track_transaction(cwd, {**payload, **(job.get("result") or {})}, state)
    run = load_run(cwd) or {}
    if (tool == "cad_apply_patch" and (run.get("hooks") or {}).get("auto_validate")
            and (job.get("result") or {}).get("status") == "candidate_ready" and args.get("model_id") and args.get("base_revision")):
        tx = payload.get("transaction_id")
        v = call("cad_validate", {"model_id": args["model_id"], "base_revision": args["base_revision"], "transaction_id": tx,
                                  "idempotency_key": idem_key("hook-validate", {"tx": tx})}, cfg=cfg)
        if v.get("job_id"):
            vj = wait(v["job_id"], max(10.0, budget - float(result.get("elapsed_s") or 0)), cfg)
            vc = compact_job(vj)
            result["validation"] = {k: vc.get("result", {}).get(k) for k in ("status", "digest", "failed_checks") if vc.get("result", {}).get(k)}
            result["next"] = vc.get("next") or result.get("next")
            if vj.get("status") == "succeeded":
                track_transaction(cwd, {"transaction_id": tx, "model_id": args["model_id"]}, "validated")
    field = os.environ.get("LLCAD_HOOK_OUTPUT_FIELD", "updatedMCPToolOutput")
    hook_log(cwd, {"hook": "wait", "tool": tool, "job": job_id, "status": result.get("status")})
    emit({"hookSpecificOutput": {"hookEventName": "PostToolUse", field: shaped_like(original, result)}})


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # pass-through: the original tool output stays
        sys.stderr.write("llcad wait_hook: %s\n" % e)
    sys.exit(0)
