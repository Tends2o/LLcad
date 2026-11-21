#!/usr/bin/env python3
"""PostToolUse hook after a commit (cad_commit, a committing cad_apply_patch transaction, or a
Bash run of the DSL client that committed): run the deterministic gate pack on the new head
(cad_gates; fallback cad_get_model quality) and inject a <=2 KB ranked residual table as
additionalContext. Also records the commit and the gate result in .llcad/ledger.json.

Floor, not finish: the text never says "done"; it asks for the largest residual to be fixed or
justified. Pass-through on errors.
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from llcad_core import (emit, hook_log, ledger_event, ledger_update, parse_tool_response, read_hook_input,  # noqa: E402
                        run_gates, server_config, short_tool, track_transaction)


def committed_target(tool, inp):
    """(model_id, revision) of a fresh commit, or None."""
    args = inp.get("tool_input") or {}
    if tool in ("cad_commit", "cad_apply_patch", "cad_job_get"):
        p = parse_tool_response(inp.get("tool_response"))
        if not isinstance(p, dict):
            return None
        res = p.get("result") if isinstance(p.get("result"), dict) else {}
        rev = p.get("revision") or res.get("revision")
        committed = p.get("status") == "committed" or p.get("committed") is True or res.get("committed") is True
        if rev and committed:
            if p.get("transaction_id"):
                track_transaction(inp.get("cwd"), {**p, "revision": rev}, "committed")
            return p.get("model_id") or args.get("model_id"), rev
        return None
    if tool == "Bash":
        tr = inp.get("tool_response")
        out = tr.get("stdout", "") if isinstance(tr, dict) else str(tr or "")
        if '"steps":[' not in out or '"head":"rev-' not in out:  # not a DSL client summary
            return None
        for line in reversed(out.strip().splitlines()[-20:]):
            line = line.strip()
            if line.startswith("{") and '"head"' in line:
                try:
                    d = json.loads(line)
                except Exception:
                    m = re.search(r'"model_id":"([^"]+)".*"head":"(rev-[^"]+)"', line)
                    return (m.group(1), m.group(2)) if m and '"ok":true' in line else None
                if d.get("ok") and d.get("head") and d.get("model_id") and any(s.get("status") == "committed" for s in d.get("steps") or []):
                    return d["model_id"], d["head"]
                return None
    return None


def fmt(g, rev):
    if g.get("error"):
        return "LLcad gates @%s: gate run failed (%s). Check with cad_get_model." % (rev, g["error"].get("message"))
    lines = ["LLcad gates @%s (%s): floor_passed=%s" % (rev, g.get("source"), g.get("floor_passed"))]
    if g.get("source") == "cad_gates":
        lines.append("%d residuals, largest first:" % g.get("residuals", 0))
        for r in g.get("top") or []:
            lines.append("- %s: measured %s vs target %s %s [%s]%s" % (r.get("check"), r.get("measured"), r.get("target"), r.get("unit", ""),
                                                                    r.get("severity", ""), (" at " + json.dumps(r["where"], ensure_ascii=False)[:120]) if r.get("where") else ""))
    else:
        lines.append("quality=%s bounds=%s features=%s reference=%s (no cad_gates on this server: check silhouette/"
                     "controls/colours yourself with a 4-view look)" % (g.get("quality"), g.get("bounds"), g.get("features"), g.get("reference")))
    lines.append("Floor, not finish: fix the largest residual or justify it as source-limited (assumptions list); "
                 "do not treat floor_passed as done.")
    return "\n".join(lines)[:2000]


def main():
    inp = read_hook_input()
    tool = inp.get("tool_name", "")
    tool = "Bash" if tool == "Bash" else short_tool(tool)
    if tool == "cad_discard":
        p = parse_tool_response(inp.get("tool_response"))
        if isinstance(p, dict) and p.get("status") != "failed":
            track_transaction(inp.get("cwd"), {"transaction_id": (inp.get("tool_input") or {}).get("transaction_id"),
                                               "model_id": (inp.get("tool_input") or {}).get("model_id")}, "discarded")
        return
    target = committed_target(tool, inp)
    if not target or not target[0]:
        return
    model_id, rev = target
    cwd = inp.get("cwd")
    if os.environ.get("LLCAD_GATES_HOOK", "1") == "0":
        return
    g = run_gates(model_id, rev, cfg=server_config(cwd))

    def rec(led):
        led["model_id"] = led.get("model_id") or model_id
        led["head"] = rev
        top = ["%s %s/%s" % (r.get("check"), r.get("measured"), r.get("target")) for r in (g.get("top") or [])[:5]]
        led["gates"] = {"revision": rev, "floor_passed": g.get("floor_passed"), "source": g.get("source"), "top": top}
        ledger_event(led, "gates %s floor=%s" % (rev[:12], g.get("floor_passed")))
    ledger_update(cwd, rec)
    hook_log(cwd, {"hook": "gates", "rev": rev, "floor": g.get("floor_passed"), "source": g.get("source")})
    emit({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": fmt(g, rev)}})


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        sys.stderr.write("llcad gates_hook: %s\n" % e)
    sys.exit(0)
