"""Local drawing run for "Drawing Export" - without a cloud model.

Runs the same procedure as `tools/drawing_bench.py planrun` (planner -> best candidate ->
strict render with autofix), then produces up to four layout variants by pure
block displacement (N1.9, N1.7, blocking 0), optionally lets MiniCPM-V choose with a preliminary
sight test (fallback always score, budget <= 4 min) and publishes the chosen sheet.

Usage: `python -X utf8 runner.py <order.json>`; order.json names model_id,
revision, url and token_file (the token itself is never in the job file).

Output on stdout in the stream-JSON format of the agent stage (one line per event):
`assistant` events for progress, in the end exactly one `result` event. In the service the run
only counts as successful if a drawing was published (drawingOutcome).
"""
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

_LOCAL_DIR = str(Path(__file__).resolve().parent)
if _LOCAL_DIR not in sys.path:
    sys.path.insert(0, _LOCAL_DIR)

import variants
import vision_select
import plan_layout as pl      # candidate order as in the planner (variants puts packages/drawing on sys.path)

RENDER_TIMEOUT_S = 900      # planner, strict render with autofix, publishing (mandatory path; like the service limit of the autofix render)
# Hard deadline per drawing from the start of the job. Variant checking and layout choice only run as long as the remaining time suffices for the
# next render (estimate: measured render time of the strict render with autofix x RENDER_SAFETY) plus PUBLISH_RESERVE_S.
# Publishing has priority; the deadline never makes the run fail, it only shortens the variant checking.
# A cold run can take 680 s (planner 510 s). If after the strict render the remaining time suffices for no variant render, the
# variant stage is dropped entirely; optional renders (comparison, variants) never wait longer than the remaining time until the deadline minus PUBLISH_RESERVE_S (call_timeout).
DEADLINE_S = 600.0
RENDER_SAFETY = 1.15
PUBLISH_RESERVE_S = 20.0
MIN_CALL_S = 1.0


def emit(event):
    sys.stdout.write(json.dumps(event, ensure_ascii=False) + "\n")
    sys.stdout.flush()


_counter = [0]


def say(text, tool=None, model_id=None):
    _counter[0] += 1
    content = [{"type": "text", "text": text}]
    if tool:
        content.insert(0, {"type": "tool_use", "id": f"local-{_counter[0]}", "name": tool,
                           "input": {"model_id": model_id} if model_id else {}})
    emit({"type": "assistant", "message": {"id": f"local-msg-{_counter[0]}", "model": "local", "content": content}})


def result(text, error=False, started=None):
    emit({"type": "result", "subtype": "error_during_execution" if error else "success", "is_error": error,
          "result": text, "duration_ms": int((time.time() - started) * 1000) if started else None,
          "total_cost_usd": 0})


class Api:
    def __init__(self, url, token_file):
        self.base = url.rstrip("/")
        self._token = Path(token_file).read_text(encoding="utf-8").strip()

    def post(self, tool, body, timeout=RENDER_TIMEOUT_S):
        req = urllib.request.Request(self.base + "/api/tools/" + tool, data=json.dumps(body).encode("utf-8"),
                                     method="POST", headers={"Authorization": "Bearer " + self._token,
                                                             "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"{tool}: HTTP {e.code} {e.read()[:400].decode('utf-8', 'replace')}") from None

    def blob(self, artifact_id, timeout=120):
        req = urllib.request.Request(self.base + "/api/artifacts/" + str(artifact_id),
                                     method="GET", headers={"Authorization": "Bearer " + self._token})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"artifact {artifact_id}: HTTP {e.code}") from None


class Timings:
    """Phase times of a run: name, wall time and, if applicable, the seconds reported by the service."""

    def __init__(self):
        self.items = []
        self.last = 0.0
        self.last_strict = 0.0

    def post(self, api, name, tool, body, **kw):
        t = time.time()
        try:
            out = api.post(tool, body, **kw)
        finally:
            self.last = time.time() - t
        self.items.append({"phase": name, "seconds": round(self.last, 1),
                           "service_seconds": out.get("seconds") if isinstance(out, dict) else None})
        return out

    def add(self, name, t0):
        self.items.append({"phase": name, "seconds": round(time.time() - t0, 1)})


def error_text(out):
    errs = out.get("errors") if isinstance(out, dict) else None
    if errs:
        e = errs[0]
        return f"{e.get('code', 'ERROR')}: {e.get('message', '')}".strip()
    return None


def pick(candidates):
    """Best candidate by plan_layout.candidate_order: highest score, on a tie the smaller format, then the planner order."""
    return min(candidates, key=lambda c: pl.candidate_order(c.get("metrics") or {}))


def _select_layout(valid_variants, *, store_root=None, work_dir=None, on_wait=lambda m: None, budget_s=None):
    kw = {} if budget_s is None else {"budget_s": budget_s}
    return vision_select.select(valid_variants, store_root=store_root, work_dir=work_dir, on_wait=on_wait, **kw)


def time_left(started, now=None):
    """Remaining time until the hard deadline (seconds, can be negative)."""
    return started + DEADLINE_S - (time.time() if now is None else now)


def render_fits(started, est_render_s):
    """Does the remaining time suffice for a further render (estimated, with safety factor) plus publishing?"""
    return time_left(started) >= est_render_s * RENDER_SAFETY + PUBLISH_RESERVE_S


def call_timeout(started, now=None):
    """Time limit of an optional render call (comparison, variant render): at most RENDER_TIMEOUT_S and at most the remaining time until the deadline minus
    PUBLISH_RESERVE_S, so that publishing stays before the deadline even if a render takes longer than estimated; at least MIN_CALL_S. The mandatory path (planner, strict
    render, publishing) keeps RENDER_TIMEOUT_S: without it there would be nothing to publish (first run with cold caches)."""
    return max(MIN_CALL_S, min(float(RENDER_TIMEOUT_S), time_left(started, now) - PUBLISH_RESERVE_S))


def _no_variants_choice(reason, **extra):
    return {"enabled": False, "model_profile": None, "variants": [], "defender_initial": "v0", "sanity_check": None, "pairs": [], "chosen": "v0",
            "chosen_by": "score", "fallback_reason": reason, "total_seconds": 0.0, **extra}


def _choice_summary_text(choice, chosen_var):
    vid = choice.get("chosen") or chosen_var["id"]
    strat = chosen_var.get("strategy") or "original"
    if choice.get("chosen_by") == "minicpm-v":
        prof = choice.get("model_profile") or "MiniCPM-V"
        label = "MiniCPM-V" if "minicpm" in str(prof).lower() else str(prof)
        return f"Layout variant {vid} ({strat}) chosen by {label}"
    why = choice.get("fallback_reason") or "default choice"
    return f"Layout variant {vid} ({strat}) chosen by score because {why}"


def _write_timings(work_dir, tm, started):
    if work_dir is None:
        return
    try:
        work_dir.mkdir(parents=True, exist_ok=True)
        (work_dir / "timings.json").write_text(json.dumps(
            {"total_seconds": round(time.time() - started, 1), "phases": tm.items}, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def run(order):
    started = time.time()
    api = Api(order["url"], order["token_file"])
    model, revision = order["model_id"], order["revision"]
    base = {"model_id": model, "revision": revision}
    store_root = Path(order["token_file"]).resolve().parent
    work_dir = Path(order["work_dir"]) if order.get("work_dir") else None

    say("Local drawing run: planner creates sheet candidates ...", "cad_drawing_plan", model)
    tm = Timings()
    plan = tm.post(api, "plan", "cad_drawing_plan", base, timeout=RENDER_TIMEOUT_S)
    if error_text(plan):
        return result(error_text(plan), started=started)
    cands = plan.get("candidates") or []
    if not cands:
        return result("The planner returned no sheet candidate.", started=started)
    chosen = pick(cands)
    m = chosen.get("metrics") or {}
    say(f"Candidate {chosen['candidate_id']} chosen ({m.get('format')} {m.get('scale')}, "
        f"{m.get('views')} views, {m.get('dims')} dimensions); strict render with layout repair ...",
        "cad_drawing_render", model)

    try:
        rendered = tm.post(api, "render_strict_autofix", "cad_drawing_render", dict(base, spec_ref={"artifact_id": chosen["spec_artifact_id"]},
                                                       strict=True, autofix=True, images="none"), timeout=RENDER_TIMEOUT_S)
    finally:
        tm.last_strict = tm.last
    if error_text(rendered) or "sheets" not in rendered:
        return result(f"Render failed: {error_text(rendered) or 'no sheets'}", started=started)
    blocking = rendered.get("blocking") or 0
    sheets = rendered.get("sheets") or []
    if blocking:
        codes = sorted({f.get("code") for f in rendered.get("findings") or [] if f.get("severity") == "block"})
        return result(f"{blocking} blocking findings after the layout repair ({', '.join(codes)}); "
                      "not published.", started=started)

    # layout variants after autofix (block displacement only, strictly blocking 0) + optional MiniCPM-V choice
    choice = None
    chosen_var = {"id": "v0", "strategy": "original", "score": 1.0}
    if len(sheets) == 1 and not render_fits(started, tm.last_strict):
        # after the strict render the remaining time suffices for no variant render - publishing has priority, no variant work (blobs, generation, choice)
        note = "Variants skipped for lack of time"
        say(f"{note}: remaining time {max(0, int(time_left(started)))} s, render estimated {int(tm.last_strict)} s; publishing the original.")
        choice = _no_variants_choice(note, rejected=[], time_limit={"deadline_s": DEADLINE_S, "elapsed_s": round(time.time() - started, 1),
                                                                    "estimated_render_s": round(tm.last_strict, 1), "skipped": [], "variant_stage_skipped": True})
    elif len(sheets) == 1:
        try:
            fix = rendered.get("autofix") or {}
            spec_aid = fix.get("spec_artifact_id") or chosen["spec_artifact_id"]
            t0 = time.time()
            spec = json.loads(api.blob(spec_aid).decode("utf-8"))
            vframes = rendered.get("view_frames") or {}
            gen = variants.generate(spec, vframes, max_variants=variants.MAX_VARIANTS)
            orig_png = api.blob(sheets[0]["artifact_id"])
            tm.add("create_blobs_and_variants", t0)
            valid = []
            rejected = []
            # no variant may have more findings than the original for any finding code (warn counts
            # too), not checkable = rejected. The findings of the original are already in the response of the strict render with
            # autofix (or in its untruncated file last-findings-autofix-<key of the input spec>.json); only if
            # they are missing, the comparison render without autofix follows (only with sufficient remaining time).
            render_s = tm.last_strict
            skipped = []
            orig_counts = None
            if any(v["strategy"] != "original" for v in gen):
                try:
                    t0 = time.time()
                    plan_spec = json.loads(api.blob(chosen["spec_artifact_id"]).decode("utf-8"))
                    bf, bok = variants.findings_of(store_root, plan_spec, rendered, action="autofix")
                    tm.add("original_findings_from_autofix", t0)
                    orig_counts = variants.finding_counts(bf) if bok else None
                except Exception:
                    orig_counts = None
                if orig_counts is None and render_fits(started, render_s):
                    try:
                        br = tm.post(api, "render_original_comparison", "cad_drawing_render", dict(base, spec=spec, strict=True, autofix=False, images="none"),
                                     timeout=call_timeout(started))
                        if not error_text(br) and "sheets" in br:
                            t0 = time.time()
                            bf, bok = variants.findings_of(store_root, spec, br)
                            tm.add("findings_file_original", t0)
                            orig_counts = variants.finding_counts(bf) if bok else None
                    except Exception:
                        orig_counts = None
            # best prospect first (score descending), so that with scarce time the most promising variant is checked
            order_ = sorted(gen, key=lambda v: (0 if v["strategy"] == "original" else 1, -float(v.get("score", 0.0)), v["id"]))
            for var in order_:
                if var["strategy"] == "original":
                    valid.append({**var, "blocking": 0, "png": orig_png, "_rendered": rendered})
                    continue
                if orig_counts is None:
                    rejected.append({"id": var["id"], "strategy": var["strategy"], "reason": "findings of the original not checkable"})
                    continue
                if not render_fits(started, render_s):
                    skipped.append({"id": var["id"], "strategy": var["strategy"]})
                    continue
                try:
                    vr = tm.post(api, "render_" + var["id"], "cad_drawing_render", dict(base, spec=var["spec"], strict=True, autofix=False, images="none"),
                                 timeout=call_timeout(started))
                    render_s = max(render_s, tm.last)
                except Exception as e:      # a render aborted at the remaining time is also listed in rejected and raises the estimate
                    render_s = max(render_s, tm.last)
                    rejected.append({"id": var["id"], "strategy": var["strategy"], "reason": f"Render aborted ({type(e).__name__})"})
                    continue
                if error_text(vr) or "sheets" not in vr or (vr.get("blocking") or 0) > 0:
                    continue
                t0 = time.time()
                vf, vok = variants.findings_of(store_root, var["spec"], vr)
                tm.add("findings_file_" + var["id"], t0)
                if not vok:
                    rejected.append({"id": var["id"], "strategy": var["strategy"], "reason": "findings of the variant not checkable"})
                    continue
                worse = variants.worse_codes(orig_counts, variants.finding_counts(vf))
                if worse:
                    rejected.append({"id": var["id"], "strategy": var["strategy"], "reason": "more findings than the original", "worse_codes": worse})
                    continue
                try:
                    vpng = api.blob(vr["sheets"][0]["artifact_id"])
                except Exception:
                    continue
                valid.append({**var, "blocking": 0, "png": vpng, "_rendered": vr})
            if skipped:
                say("Variants skipped for lack of time: " + ", ".join(x["id"] for x in skipped)
                    + f" (remaining time {max(0, int(time_left(started)))} s, render estimated {int(render_s)} s).")
            if valid:
                # Sort by score (defending champion = position 0)
                valid.sort(key=lambda v: (-float(v.get("score", 0.0)), 0 if v["strategy"] == "original" else 1, v["id"]))
                say(f"{len(valid)} valid layout variants (blocking 0) checked; starting layout choice ...")
                t0 = time.time()
                # choice budget of the vision model: at most the remaining time minus publishing
                vbudget = max(0.0, min(vision_select.BUDGET_S, time_left(started) - PUBLISH_RESERVE_S))
                choice = _select_layout(valid, store_root=store_root, work_dir=(work_dir / "local_llm") if work_dir else None,
                                        on_wait=lambda msg: say(msg), budget_s=vbudget)
                tm.add("layout_choice", t0)
                choice["rejected"] = rejected
                if skipped:
                    note = "Variants skipped for lack of time"
                    choice["time_limit"] = {"deadline_s": DEADLINE_S, "elapsed_s": round(time.time() - started, 1),
                                            "estimated_render_s": round(render_s, 1), "skipped": skipped}
                    choice["fallback_reason"] = f"{choice['fallback_reason']}; {note}" if choice.get("fallback_reason") else note
                chosen_var = next((v for v in valid if v["id"] == choice.get("chosen")), valid[0])
                rendered = chosen_var["_rendered"]
                sheets = rendered.get("sheets") or sheets
        except Exception as e:
            choice = _no_variants_choice(f"Variant choice skipped ({type(e).__name__}: {e})")
    else:
        choice = _no_variants_choice("Multi-sheet drawing (layout variants for single sheets only)")

    pub_t0 = time.time()
    if work_dir is not None and choice is not None:
        try:
            work_dir.mkdir(parents=True, exist_ok=True)
            (work_dir / "choice.json").write_text(json.dumps(choice, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass

    choice_text = _choice_summary_text(choice, chosen_var) if choice else "default layout (score)"
    cov = rendered.get("coverage") or {}
    cov_text = (f"{cov.get('dimensioned')}/{cov.get('required')} required features dimensioned"
                if cov.get("required") else "coverage not reported")
    warn = rendered.get("warnings_count") or 0
    if choice and choice.get("chosen_by") == "minicpm-v":
        statement = (f"Local run: candidate {chosen['candidate_id']} from cad_drawing_plan, "
                     f"strict render with autofix; {choice_text}; all {len(sheets)} sheets checked automatically - "
                     f"0 blocking findings, {warn} warnings, {cov_text}.")
    else:
        statement = (f"Local run without a language model: candidate {chosen['candidate_id']} from cad_drawing_plan, "
                     f"strict render with autofix; {choice_text}; all {len(sheets)} sheets checked automatically - "
                     f"0 blocking findings, {warn} warnings, {cov_text}. No visual inspection by a model.")
    say(f"0 blocking findings ({choice_text}); publishing ...", "cad_drawing_publish", model)
    pub = tm.post(api, "publish", "cad_drawing_publish", dict(base, render_id=rendered["render_id"],
                                               review={"sheets_viewed": len(sheets), "statement": statement}), timeout=RENDER_TIMEOUT_S)
    if error_text(pub) or not pub.get("published"):
        return result(f"Publication rejected: {error_text(pub) or 'unknown'}", started=started)

    _write_timings(work_dir, tm, started)
    fmt = ", ".join(f"{s.get('format')} {s.get('orientation') or ''}".strip() for s in sheets)
    result("\n".join([
        f"**Drawing:** {len(sheets)} sheet ({fmt}), scale {m.get('scale')}, {m.get('views')} views - created locally.",
        f"**Layout choice:** {choice_text}.",
        f"**Dimensioned:** {cov_text}; {warn} warnings without blocking.",
        "**Assumptions:** title block from the project defaults (title_defaults).",
        "**Open:** visual inspection by a person.",
    ]), started=started)


def main():
    if len(sys.argv) != 2:
        print("Usage: runner.py <order.json>", file=sys.stderr)
        return 2
    order_path = Path(sys.argv[1]).resolve()
    order = json.loads(order_path.read_text(encoding="utf-8"))
    order.setdefault("work_dir", str(order_path.parent))
    started = time.time()
    try:
        run(order)
    except Exception as e:  # every error ends with a result event
        result(f"Local drawing run aborted: {e}", error=True, started=started)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
