#!/usr/bin/env python3
"""PreToolUse hook for cad_apply_patch / cad_plan_edit.

Denies, with an actionable reason, what would fail or build wrong geometry:
  > 64 operations, clockwise extruded profiles, non-coplanar profiles, unequal loft segment counts,
  curve outputs, set_structure/remove_feature without expected_hash, structure ids equal to feature
  ids, strip paths on several z planes, lane id-prefix violations (run.json lanes.{agent_type}).
Rewrites the input (updatedInput) to
  - add a missing idempotency_key (sha of tool + canonical arguments),
  - replace a key already used with different arguments, or after a failed attempt, by a fresh one,
  - normalise JSON numbers inside quantities/point lists to decimal strings (safe on every server).
A rewrite is only auto-approved when mcp__llcad is already allowed in the user's/project's settings
or LLCAD_HOOK_ALLOW=1 (viewer runs that pass --allowedTools mcp__llcad); otherwise the hook denies
with the corrected key instead, so it never grants a permission that was not already granted.
Every rewrite is logged to .llcad/hook-log.jsonl.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from llcad_core import (emit, hook_log, idem_key, lint_operations, load_json, load_run, locked,  # noqa: E402
                        normalise_numbers, read_hook_input, save_json, sha, short_tool, _keys_path)


def allowed_by_settings(cwd):
    if os.environ.get("LLCAD_HOOK_ALLOW") == "1":
        return True
    files = [Path.home() / ".claude" / "settings.json"]
    if cwd:
        files += [Path(cwd) / ".claude" / "settings.json", Path(cwd) / ".claude" / "settings.local.json"]
    for f in files:
        s = load_json(f, {}) or {}
        allow = ((s.get("permissions") or {}).get("allow")) or []
        if any(a in ("mcp__llcad", "mcp__llcad__*") or str(a).startswith("mcp__llcad__cad_apply_patch") for a in allow):
            return True
    return False


def deny(reason):
    emit({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                 "permissionDecisionReason": reason[:1800]}})


def main():
    inp = read_hook_input()
    tool = short_tool(inp.get("tool_name", ""))
    if tool not in ("cad_apply_patch", "cad_plan_edit"):
        return
    args = inp.get("tool_input") or {}
    cwd = inp.get("cwd")
    run = load_run(cwd) or {}
    ops = args.get("operations")
    ref = args.get("patch_ref") or {}
    if ops is None and isinstance(ref, dict) and ref.get("path"):
        data = load_json(ref["path"])
        ops = data.get("operations") if isinstance(data, dict) else data
    lanes = run.get("lanes") or {}
    prefix = lanes.get(inp.get("agent_type")) if inp.get("agent_type") else None
    cw = os.environ.get("LLCAD_LINT_CW") or (run.get("hooks") or {}).get("cw") or "error"
    res = lint_operations(ops or [], cw_policy=cw, prefix=prefix) if ops is not None else {"errors": [], "warnings": []}
    if res["errors"]:
        hook_log(cwd, {"hook": "lint", "tool": tool, "deny": res["errors"][:5]})
        deny("LLcad lint (fix and resend; nothing was sent):\n- " + "\n- ".join(res["errors"][:12]))
        return
    new = dict(args)
    notes = []
    if "operations" in new:
        new["operations"], n = normalise_numbers(new["operations"])
        if n:
            notes.append("%d numbers -> decimal strings" % n)
    body = {k: v for k, v in new.items() if k != "idempotency_key"}
    digest = sha({"tool": tool, "args": body})
    key_in = args.get("idempotency_key")
    reg_path = _keys_path().parent / "hook-keys.json"
    with locked(reg_path.parent, "hook-keys.lock"):
        reg = load_json(reg_path, {}) or {}
        failed_reg = load_json(_keys_path(), {}) or {}
        if not key_in:
            key_in = idem_key(tool, {"a": body})
            notes.append("idempotency_key added")
        entry = reg.get(key_in)
        if entry is None:
            entry = {"digest": digest, "sent": key_in, "attempt": 0}
        elif entry.get("digest") != digest:
            entry = {"digest": digest, "sent": ("%s-%s" % (key_in[:100], digest[:10]))[:128], "attempt": 0}
            notes.append("idempotency_key replaced (reused with different arguments)")
        if (failed_reg.get(entry["sent"]) or {}).get("failed"):
            entry["attempt"] = entry.get("attempt", 0) + 1
            entry["sent"] = ("%s-%s-a%d" % (key_in[:96], digest[:8], entry["attempt"]))[:128]
            notes.append("idempotency_key renewed after a failed attempt")
        reg[key_in] = entry
        if len(reg) > 3000:
            for k in list(reg)[:1000]:
                reg.pop(k, None)
        save_json(reg_path, reg)
    key = entry["sent"]
    if args.get("idempotency_key") and key != args["idempotency_key"] and not any("idempotency_key" in n for n in notes):
        notes.append("idempotency_key mapped to the key already sent for these arguments")
    new["idempotency_key"] = key
    ctx = ("LLcad lint warnings: " + "; ".join(res["warnings"][:6]))[:900] if res["warnings"] else None
    if not notes:
        if ctx:
            emit({"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": ctx}})
        return
    hook_log(cwd, {"hook": "lint", "tool": tool, "rewrite": notes, "key": key})
    if not allowed_by_settings(cwd):
        if any("idempotency_key" in n for n in notes):
            deny("LLcad lint: %s. Resend with \"idempotency_key\": \"%s\"." % ("; ".join(notes), key))
        elif ctx:
            emit({"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": ctx}})
        return
    out = {"hookEventName": "PreToolUse", "permissionDecision": "allow",
           "permissionDecisionReason": "llcad lint: " + "; ".join(notes), "updatedInput": new}
    if ctx:
        out["additionalContext"] = ctx
    emit({"hookSpecificOutput": out})


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        sys.stderr.write("llcad lint_hook: %s\n" % e)
    sys.exit(0)
