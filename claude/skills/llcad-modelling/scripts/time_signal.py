#!/usr/bin/env python3
"""Time signal (PostToolUse / UserPromptSubmit): adds 'elapsed X min / budget Y min' as
additionalContext when a run contract (.llcad/run.json with started_at and budget_min) exists.

Throttled: at most every run.time_signal_min minutes (default 5) plus once when crossing 50, 75,
90 and 100 % of the budget, each with the phase advice of the time box.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from llcad_core import elapsed_min, emit, llcad_dir, load_json, read_hook_input, save_json  # noqa: E402

ADVICE = [
    (1.0, "OVER BUDGET: stop adding detail; commit, run gates, write assumptions + report now."),
    (0.9, "90%: finish: final gates, assumptions list, report. No new features."),
    (0.75, "75%: details frozen; gates + judge + at most one fix round."),
    (0.5, "50%: MVP must be committed and gated; then details by ledger priority."),
]


def main():
    inp = read_hook_input()
    d = llcad_dir(inp.get("cwd"))
    if not d:
        return
    run = load_json(d / "run.json", {}) or {}
    el = elapsed_min(run)
    budget = run.get("budget_min")
    if el is None or not budget:
        return
    frac = el / float(budget)
    st_p = d / "time_signal.json"
    st = load_json(st_p, {}) or {}
    now = time.time()
    crossed = [t for t, _ in ADVICE if frac >= t and t not in st.get("crossed", [])]
    interval = float(run.get("time_signal_min", 5)) * 60
    if not crossed and now - st.get("last", 0) < interval:
        return
    msg = "LLcad time: elapsed %.0f min / budget %s min (%.0f%%)." % (el, budget, 100 * frac)
    for t, advice in ADVICE:
        if frac >= t:
            msg += " " + advice
            break
    st["last"] = now
    st["crossed"] = sorted(set(st.get("crossed", [])) | set(crossed))
    save_json(st_p, st)
    event = inp.get("hook_event_name") or "PostToolUse"
    emit({"hookSpecificOutput": {"hookEventName": event, "additionalContext": msg}})


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        sys.stderr.write("llcad time_signal: %s\n" % e)
    sys.exit(0)
