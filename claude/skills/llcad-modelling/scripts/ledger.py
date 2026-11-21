#!/usr/bin/env python3
"""Run contract + feature ledger for one LLcad run (files under ./.llcad/).

  ledger.py init --model M [--budget 60] [--product "Hand lamp T1"] [--in grip,head] [--out variants,accessories]
  ledger.py add ID --part P --accept "logo centroid within 1 mm of ref, blue #2a6cc0 dE<10" [--priority P0] [--view front] [--crop refs/logo.png]
  ledger.py set ID --status verified|failed|waived|open [--evidence "render rev-.. crop"] [--measured "0.4 mm"] [--revision rev-..]
  ledger.py phase "P3 details"          record the current phase (re-injected after compaction)
  ledger.py assume "grip depth [C] estimated from photo 3" [--cls C]
  ledger.py tx [close TX --state discarded|committed]
  ledger.py show [--json]               compact summary (<=1.5 KB)
Items are the acceptance tests of visible details (position, size, colour, visibility). Status
must be backed by evidence bound to a revision; the stop gate reads this file.
"""
import argparse
import datetime as dt
import json
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from llcad_core import SKILL_DIR, ledger_event, ledger_summary, ledger_update, llcad_dir, load_json, new_ledger, save_json  # noqa: E402


def cmd_init(a):
    d = llcad_dir(create=True)
    tpl = load_json(SKILL_DIR / "templates" / "run.json", {}) or {}
    run = load_json(d / "run.json", None) or tpl
    run.pop("_comment", None)
    if a.model:
        run["model_id"] = a.model
    if a.budget:
        run["budget_min"] = a.budget
    if a.product:
        run["product_name"] = a.product
    sc = run.setdefault("scope", {"in": [], "out": []})
    if a.scope_in:
        sc["in"] = [s.strip() for s in a.scope_in.split(",") if s.strip()]
    if a.scope_out:
        sc["out"] = [s.strip() for s in a.scope_out.split(",") if s.strip()]
    # Hooks run without the generator's environment: record the server this run really uses.
    srv = run.setdefault("server", {})
    for key, env in (("url", "LLCAD_URL"), ("token_file", "LLCAD_TOKEN_FILE"), ("root", "LLCAD_ROOT")):
        if os.environ.get(env):
            srv[key] = os.environ[env]
    if not run.get("started_at") or a.restart:
        run["started_at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    save_json(d / "run.json", run)
    if not (d / "ledger.json").exists():
        save_json(d / "ledger.json", new_ledger(run.get("model_id")))
    for name in ("assumptions.md", "scope.md"):
        dst = d / name
        if not dst.exists():
            shutil.copy(SKILL_DIR / "templates" / name, dst)
    (d / "patches").mkdir(exist_ok=True)
    print(json.dumps({"run": str(d / "run.json"), "model_id": run.get("model_id"), "budget_min": run.get("budget_min"),
                      "started_at": run.get("started_at")}))


def cmd_add(a):
    def fn(led):
        items = led.setdefault("items", [])
        if any(i["id"] == a.id for i in items):
            raise SystemExit("item %s exists; use set" % a.id)
        it = {"id": a.id, "part": a.part, "acceptance": a.accept, "priority": a.priority, "status": "open"}
        if a.view:
            it["view"] = a.view
        if a.crop:
            it["source_crop"] = a.crop
        items.append(it)
        ledger_event(led, "add %s" % a.id)
    ledger_update(None, fn) if llcad_dir() else sys.exit("no .llcad/: run ledger.py init first")
    print("added %s" % a.id)


def cmd_set(a):
    def fn(led):
        for it in led.get("items", []):
            if it["id"] == a.id:
                if a.status:
                    it["status"] = a.status
                if a.evidence:
                    it.setdefault("evidence", []).append(a.evidence)
                    del it["evidence"][:-5]
                if a.measured:
                    it["measured"] = a.measured
                it["revision"] = a.revision or led.get("head")
                if it.get("status") == "verified" and not (it.get("evidence") and it.get("revision")):
                    raise SystemExit("verified needs --evidence and a revision")
                ledger_event(led, "%s -> %s" % (a.id, it.get("status")))
                return
        raise SystemExit("no item %s" % a.id)
    ledger_update(None, fn)
    print("ok")


def cmd_phase(a):
    ledger_update(None, lambda led: (led.__setitem__("phase", a.name), ledger_event(led, "phase " + a.name)))
    print("phase: %s" % a.name)


def cmd_assume(a):
    d = llcad_dir()
    run = load_json(d / "run.json", {}) or {}
    p = Path(run.get("assumptions_file") or ".llcad/assumptions.md")
    p = p if p.is_absolute() else d.parent / p
    with open(p, "a", encoding="utf-8") as fh:
        fh.write("- [%s] %s\n" % (a.cls, a.text))
    print("appended to %s" % p)


def cmd_tx(a):
    if a.action == "close":
        def fn(led):
            t = led.setdefault("transactions", {}).get(a.tx)
            if not t:
                raise SystemExit("unknown transaction %s" % a.tx)
            t["state"] = a.state
            ledger_event(led, "tx %s %s" % (a.tx[:15], a.state))
        ledger_update(None, fn)
        print("ok")
        return
    led = load_json(llcad_dir() / "ledger.json", {}) or {}
    print(json.dumps({k: v for k, v in led.get("transactions", {}).items() if v.get("state") not in ("committed", "discarded")}))


def cmd_show(a):
    d = llcad_dir()
    if not d:
        sys.exit("no .llcad/")
    led = load_json(d / "ledger.json", {}) or {}
    run = load_json(d / "run.json", {}) or {}
    if a.json:
        print(json.dumps(led, ensure_ascii=False)[:6000])
    else:
        print(ledger_summary(led, run))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("init")
    s.add_argument("--model")
    s.add_argument("--budget", type=int)
    s.add_argument("--product")
    s.add_argument("--in", dest="scope_in")
    s.add_argument("--out", dest="scope_out")
    s.add_argument("--restart", action="store_true", help="reset started_at")
    s = sub.add_parser("add")
    s.add_argument("id")
    s.add_argument("--part", required=True)
    s.add_argument("--accept", required=True)
    s.add_argument("--priority", default="P1", choices=["P0", "P1", "P2"])
    s.add_argument("--view")
    s.add_argument("--crop")
    s = sub.add_parser("set")
    s.add_argument("id")
    s.add_argument("--status", choices=["open", "verified", "failed", "waived"])
    s.add_argument("--evidence")
    s.add_argument("--measured")
    s.add_argument("--revision")
    s = sub.add_parser("phase")
    s.add_argument("name")
    s = sub.add_parser("assume")
    s.add_argument("text")
    s.add_argument("--cls", default="C", choices=["A", "B", "C"])
    s = sub.add_parser("tx")
    s.add_argument("action", nargs="?", default="list", choices=["list", "close"])
    s.add_argument("tx", nargs="?")
    s.add_argument("--state", default="discarded", choices=["discarded", "committed", "superseded"])
    s = sub.add_parser("show")
    s.add_argument("--json", action="store_true")
    a = ap.parse_args()
    {"init": cmd_init, "add": cmd_add, "set": cmd_set, "phase": cmd_phase, "assume": cmd_assume, "tx": cmd_tx, "show": cmd_show}[a.cmd](a)


if __name__ == "__main__":
    main()
