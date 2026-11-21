#!/usr/bin/env python3
"""SessionStart (matcher compact|resume): re-inject the compact run state after compaction or
resume: run contract essentials, ledger summary (open items, open candidates, last gates) and the
current phase, so the agent does not re-orient from scratch (re-orientation cost 22-56 min before).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from llcad_core import emit, ledger_summary, llcad_dir, load_json, read_hook_input  # noqa: E402


def main():
    inp = read_hook_input()
    if inp.get("source") not in (None, "compact", "resume"):
        return
    d = llcad_dir(inp.get("cwd"))
    if not d:
        return
    run = load_json(d / "run.json", {}) or {}
    led = load_json(d / "ledger.json", {}) or {}
    if not run and not led:
        return
    lines = ["LLcad run state restored after %s (skill llcad-modelling; files in %s):" % (inp.get("source") or "start", d)]
    if run:
        sc = run.get("scope") or {}
        lines.append("model %s | product %s | scope in %s | out %s | acceptance %s" % (
            run.get("model_id"), run.get("product_name"), sc.get("in", [])[:6], sc.get("out", [])[:6], run.get("acceptance")))
    if led.get("phase"):
        lines.append("phase: %s" % led["phase"])
    s = ledger_summary(led, run, limit=1400)
    if s:
        lines.append(s)
    lines.append("Next: read SKILL.md 'Phases' for the current phase; patches live in .llcad/patches (never cat them).")
    emit({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "\n".join(lines)[:2000]}})


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        sys.stderr.write("llcad sessionstart_ledger: %s\n" % e)
    sys.exit(0)
