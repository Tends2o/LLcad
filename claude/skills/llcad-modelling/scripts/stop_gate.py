#!/usr/bin/env python3
"""Stop hook: the run contract gate. Active only when .llcad/run.json exists.

Blocks the end of the turn with the exact missing items:
  - uncommitted candidates (tracked transactions not committed/discarded, or cad_gates open_candidates)
  - deterministic gate floor not passed on the head (cad_gates; fallback: model quality)
  - assumptions list missing or still the template
  - model name vs content (name claims, product keyword, out-of-scope parts, no outputs)
  - optional: open P0 ledger items when acceptance.ledger == "required"
Honours stop_hook_active + max_stop_blocks (default 6, below Claude Code's 8-block override) and
the time box: when either is exhausted it lets the run stop and writes .llcad/stop-report.md
(give-up/budget report listing what is still missing). An explicit .llcad/giveup.md written by the
agent after the last block is accepted as an abort note.
"""
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from llcad_core import (call, elapsed_min, emit, first_error, hook_log, llcad_dir, load_json, open_transactions,  # noqa: E402
                        read_hook_input, run_gates, save_json, server_config)

CLAIM_WORDS = re.compile(r"complete|completed|final|finished|full model", re.I)
PLACEHOLDER = re.compile(r"<[^>]+>|TODO|TBD|\.\.\.$")


def assumptions_missing(base, run):
    path = run.get("assumptions_file") or ".llcad/assumptions.md"
    p = path if os.path.isabs(path) else os.path.join(base, path)
    if not os.path.exists(p):
        return "assumptions list missing: write %s (every [C]-class/estimated item, source limits, open questions)" % path
    items = [ln for ln in open(p, encoding="utf-8").read().splitlines()
             if re.match(r"^\s*[-*]\s+\S", ln) and not PLACEHOLDER.search(ln)]
    if not items:
        return "assumptions list %s has no real entries (template placeholders only)" % path
    return None


def structure_parts(model_id, rev, cfg):
    parts, offset = [], 0
    while offset < 256:
        r = call("cad_structure", {"model_id": model_id, "revision": rev, "kind": "part", "offset": offset, "limit": 16}, cfg=cfg)
        if r.get("status") == "failed":
            break
        parts += [(e.get("definition") or {}).get("semantic_name") or e.get("semantic_name") or "" for e in r.get("entries") or []]
        offset += 16
        if offset >= (r.get("total_matches") or 0):
            break
    return parts


def check(base, run, led, cfg):
    missing = []
    model_id = run.get("model_id") or (led or {}).get("model_id")
    acc = run.get("acceptance") or {}
    if not model_id:
        return ["run.json has no model_id (record it: llcad.py create ... or ledger.py init --model ...)"]
    g = call("cad_get_model", {"model_id": model_id, "limit": 1}, cfg=cfg)
    if g.get("status") == "failed":
        e = first_error(g) or {}
        return ["cannot read model %s: %s %s" % (model_id, e.get("code"), str(e.get("message"))[:120])]
    head = g.get("head_revision") or g.get("revision")
    # 1 uncommitted candidates
    for tx, t in open_transactions(led or {}):
        if t.get("model_id") in (None, model_id):
            missing.append("uncommitted candidate %s (state %s, base %s): validate+commit it or cad_discard it"
                           % (tx, t.get("state"), (t.get("base_revision") or "?")[:16]))
    # 2 gate floor on the head
    if acc.get("gates", "floor") == "floor":
        gl = (led or {}).get("gates") or {}
        if gl.get("revision") == head and gl.get("floor_passed") is not None:
            gate = {"floor_passed": gl.get("floor_passed"), "top": gl.get("top", []), "source": gl.get("source")}
        else:
            gate = run_gates(model_id, head, cfg=cfg)
        if gate.get("error"):
            missing.append("gates could not run on head %s: %s" % (head, gate["error"].get("message")))
        elif gate.get("floor_passed") is not True:
            tops = gate.get("top") or []
            tops = [t if isinstance(t, str) else "%s %s vs %s" % (t.get("check"), t.get("measured"), t.get("target")) for t in tops[:4]]
            missing.append("gate floor not passed on head %s (%s)%s" % (head, gate.get("source"), (": " + "; ".join(tops)) if tops else ""))
        for t in gate.get("top") or []:
            if isinstance(t, dict) and "open_candidate" in str(t.get("check", "")):
                missing.append("server reports open candidate: %s" % json.dumps(t)[:160])
    # 3 assumptions
    if acc.get("assumptions", "required") == "required":
        a = assumptions_missing(base, run)
        if a:
            missing.append(a)
    # 4 name vs content
    name = g.get("name") or ""
    if not g.get("outputs") and not g.get("feature_count"):
        missing.append("model %s has no content (0 features/outputs)" % model_id)
    product = run.get("product_name")
    if product and not all(w.lower() in name.lower() for w in re.findall(r"\w{3,}", product)[:3]):
        missing.append("model name %r does not name the product %r (rename with cad_model_admin action=rename)" % (name, product))
    scope_out = [s for s in ((run.get("scope") or {}).get("out") or []) if isinstance(s, str) and len(s) >= 3]
    if scope_out:
        parts = structure_parts(model_id, head, cfg)
        bad = [p for p in parts for s in scope_out if s.lower() in p.lower()]
        if bad:
            missing.append("parts look out of scope %s (scope.out %s): remove them or model them as configurations" % (bad[:4], scope_out[:4]))
    items = (led or {}).get("items") or []
    open_p0 = [i for i in items if i.get("priority", "P1") == "P0" and i.get("status") not in ("verified", "waived")]
    if CLAIM_WORDS.search(name) and open_p0:
        missing.append("model name %r claims completeness but %d P0 ledger items are open" % (name, len(open_p0)))
    if acc.get("ledger") == "required" and open_p0:
        missing.append("open P0 ledger items: %s" % ", ".join("%s(%s)" % (i.get("id"), i.get("part", "")) for i in open_p0[:8]))
    return missing


def main():
    inp = read_hook_input()
    cwd = inp.get("cwd")
    d = llcad_dir(cwd)
    if not d or not (d / "run.json").exists():
        return
    base = str(d.parent)
    run = load_json(d / "run.json", {}) or {}
    if run.get("stop_gate") is False:
        return
    led = load_json(d / "ledger.json", {}) or {}
    state_p = d / "stop_state.json"
    state = load_json(state_p, {}) or {}
    if not inp.get("stop_hook_active"):
        state["blocks"] = 0
    cfg = server_config(cwd)
    missing = check(base, run, led, cfg)
    max_blocks = int(run.get("max_stop_blocks", 6))
    el = elapsed_min(run)
    budget = run.get("budget_min")
    over = el is not None and budget and el > float(budget) + float(run.get("budget_grace_min", 5))
    giveup = d / "giveup.md"
    explicit = giveup.exists() and giveup.stat().st_mtime > state.get("last_block_t", 0) and state.get("blocks", 0) > 0
    if not missing:
        save_json(state_p, {"blocks": 0, "passed_t": time.time()})
        hook_log(cwd, {"hook": "stop", "result": "pass"})
        return
    if explicit or state.get("blocks", 0) >= max_blocks or over:
        why = "explicit give-up note" if explicit else ("time budget exhausted (%.0f/%s min)" % (el, budget) if over else "%d stop blocks" % state.get("blocks", 0))
        report = ["# LLcad stop report (%s)" % time.strftime("%Y-%m-%d %H:%M"), "", "Stopped with the run contract NOT met: %s." % why, "",
                  "Missing:"] + ["- " + m for m in missing]
        (d / "stop-report.md").write_text("\n".join(report) + "\n", encoding="utf-8")
        save_json(state_p, {"blocks": 0, "gave_up_t": time.time(), "why": why})
        hook_log(cwd, {"hook": "stop", "result": "giveup", "why": why, "missing": len(missing)})
        emit({"systemMessage": "LLcad stop gate: run ends with %d open contract items (%s); see .llcad/stop-report.md" % (len(missing), why)})
        return
    state["blocks"] = state.get("blocks", 0) + 1
    state["last_block_t"] = time.time()
    save_json(state_p, state)
    hook_log(cwd, {"hook": "stop", "result": "block", "n": state["blocks"], "missing": missing[:6]})
    reason = ("LLcad run contract not met (block %d/%d). Fix, then stop again:\n- %s\n"
              "If an item is impossible, say why in .llcad/assumptions.md; to abort deliberately write .llcad/giveup.md."
              % (state["blocks"], max_blocks, "\n- ".join(missing[:10])))
    emit({"decision": "block", "reason": reason[:3000]})


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        sys.stderr.write("llcad stop_gate: %s\n" % e)
    sys.exit(0)
