"""Local layout choice via MiniCPM-V (or a configured vision profile) with a preliminary sight test and score fallback.

Procedure:
1. Defending champion = variant with the highest deterministic score.
2. Switch from the profile configuration (drawing-config.json, read only) or the code constant VISION_LAYOUT_DEFAULT.
3. Preliminary sight test on the built-in control pair (fixtures/sanity_clean.png vs. sanity_messy.png) in both orders at T=0.
   If the model answers wrongly or order-dependently -> fallback to score.
4. Each challenger competes pairwise against the defender, in both orders (D,C) and (C,D) at T=0.
   A challenger only takes over if it wins in BOTH orders.
5. Hard total budget <= BUDGET_S (720 s = 12 min including lock wait time, server start and calls; the deadline goes to LocalModel as `deadline`).
   RAM emergency stop: LocalModel stops its own server at < 2.5 GB free, the fallback names the emergency stop. Every error (ModelBusy, missing config/file,
   too little RAM, timeout) falls back cleanly to the score defender; a drawing run never fails because of the model.
"""
from __future__ import annotations

import io
import json
import os
import time
from pathlib import Path
from typing import Callable, Optional

import llm

# Default switch if drawing-config.json sets no switch.
# To be set to True or False after the gold acceptance measurement.
VISION_LAYOUT_DEFAULT = False

BUDGET_S = 720.0        # Hard time budget (12 min including lock wait time, server start and calls; total time per drawing up to 10 min)
CALL_TIMEOUT_S = 90.0   # maximum duration per single call
IMG_LONG_PX = 768       # uniform long edge of the comparison images (RAM-saving, sharp enough for the block layout)
VISION_CTX = 4096       # lean context for 2 comparison images + JSON answer (saves GB of KV cache on 32 GB systems)
MIN_FREE_MARGIN_GB = 5.0  # RAM reserve above model+mmproj (code constant); plus emergency stop at < llm.EMERGENCY_FREE_GB (2.5 GB) free

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"

PAIR_PROMPT = (
    "You see two layout variants of the same technical drawing "
    "(first image = A, second image = B). "
    "Which sheet is arranged more cleanly and in a more structured way? "
    "Look for clear grouping, clear alignment of the views, "
    "balanced use of the area and short distances between detail views/sections and their main views. "
    'Answer exclusively as JSON: {"choice": "A"} or {"choice": "B"}.'
)


def sanity_images() -> tuple[bytes, bytes]:
    """Loads the built-in control pair (clean, messy) as PNG bytes."""
    clean = (FIXTURES_DIR / "sanity_clean.png").read_bytes()
    messy = (FIXTURES_DIR / "sanity_messy.png").read_bytes()
    return clean, messy


def downscale_png(png: bytes, long_px: int = IMG_LONG_PX) -> bytes:
    """Scales a PNG down deterministically to at most long_px on the longer edge."""
    from PIL import Image
    im = Image.open(io.BytesIO(png)).convert("RGB")
    if max(im.size) > long_px:
        im.thumbnail((long_px, long_px), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def normalize_pair(png_a: bytes, png_b: bytes, long_px: int = IMG_LONG_PX) -> tuple[bytes, bytes]:
    """Brings two comparison images to exactly the same pixel size (fair comparison without size bias)."""
    from PIL import Image
    ia = Image.open(io.BytesIO(png_a)).convert("RGB")
    ib = Image.open(io.BytesIO(png_b)).convert("RGB")
    ia.thumbnail((long_px, long_px), Image.Resampling.LANCZOS)
    ib.thumbnail((long_px, long_px), Image.Resampling.LANCZOS)
    if ia.size != ib.size:
        target = (min(ia.size[0], ib.size[0]), min(ia.size[1], ib.size[1]))
        ia = ia.resize(target, Image.Resampling.LANCZOS)
        ib = ib.resize(target, Image.Resampling.LANCZOS)
    ba, bb = io.BytesIO(), io.BytesIO()
    ia.save(ba, "PNG", optimize=True)
    ib.save(bb, "PNG", optimize=True)
    return ba.getvalue(), bb.getvalue()


def required_ram_gb(prof: dict) -> float:
    """Minimum RAM (model + mmproj + MIN_FREE_MARGIN_GB), or explicit min_free_gb from the profile (the larger one applies)."""
    base = llm._size_gb(prof.get("model")) + llm._size_gb(prof.get("mmproj")) + MIN_FREE_MARGIN_GB
    explicit = prof.get("min_free_gb")
    return round(max(base, float(explicit)) if explicit is not None else base, 2)


def prepare_profile(prof: dict) -> dict:
    """Copy of the profile with a lean pair comparison context (<= VISION_CTX) and a safe RAM limit (+MIN_FREE_MARGIN_GB)."""
    out = dict(prof)
    out["ctx"] = min(int(out.get("ctx") or VISION_CTX), VISION_CTX)
    out["min_free_gb"] = required_ram_gb(out)
    return out


def read_config(store_root: Optional[Path], profile_name: Optional[str] = None) -> tuple[Optional[dict], bool, Optional[str]]:
    """Reads drawing-config.json (read only!). Returns: (profile_dict | None, enabled, reason_if_no_profile)."""
    env_flag = os.environ.get("LLCAD_VISION_LAYOUT")
    if profile_name is None:
        env_prof = os.environ.get("LLCAD_VISION_PROFILE")
        if env_prof and env_prof.strip():
            profile_name = env_prof.strip()
    enabled_default = VISION_LAYOUT_DEFAULT if env_flag is None else env_flag.strip().lower() in ("1", "true", "yes", "on")
    if store_root is None:
        return None, enabled_default, "configuration folder missing"
    cfg_path = Path(store_root) / "drawing-config.json"
    if not cfg_path.is_file():
        return None, enabled_default, "drawing-config.json missing"
    try:
        raw = json.loads(cfg_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return None, enabled_default, f"drawing-config.json unreadable ({e})"
    lm = raw.get("local_model")
    if not isinstance(lm, dict):
        return None, enabled_default, "local_model in drawing-config.json missing"
    cfg_enabled = lm.get("layout_choice_enabled")
    if profile_name and isinstance(lm.get("profiles"), dict):
        lm = dict(lm, active=profile_name)
    try:
        prof = llm.active_profile(lm)
    except KeyError as e:
        return None, enabled_default, str(e)
    if cfg_enabled is None and isinstance(prof.get("options"), dict):
        cfg_enabled = prof["options"].get("layout_choice_enabled")
    enabled = enabled_default if env_flag is not None else (bool(cfg_enabled) if cfg_enabled is not None else VISION_LAYOUT_DEFAULT)
    for field in ("server", "model"):
        p = prof.get(field)
        if not p or not os.path.isfile(str(p)):
            return None, enabled, f"model file missing ({field}: {p})"
    if prof.get("mmproj") and not os.path.isfile(str(prof["mmproj"])):
        return None, enabled, f"mmproj missing ({prof['mmproj']})"
    return prof, enabled, None


def _pair_messages(png_a: bytes, png_b: bytes, prompt: str = PAIR_PROMPT) -> list[dict]:
    return [{
        "role": "user",
        "content": [
            llm.image_part(png_a),
            llm.image_part(png_b),
            {"type": "text", "text": prompt},
        ],
    }]


def compare_pair(model, png_a: bytes, png_b: bytes, deadline: float, prompt: str = PAIR_PROMPT) -> dict:
    """Compares two images in both orders (A,B) and (B,A) at T=0.
    Returns: {ab, ba, winner ('A'|'B'|'tie'|None), consistent, seconds, timeout}."""
    t0 = time.time()
    pa, pb = normalize_pair(png_a, png_b)
    rem1 = deadline - time.time()
    if rem1 <= 1.0:
        return {"ab": None, "ba": None, "winner": None, "consistent": False, "seconds": 0.0, "timeout": True}
    ab = model.chat_choice(_pair_messages(pa, pb, prompt), temperature=0.0, timeout=min(CALL_TIMEOUT_S, rem1))
    rem2 = deadline - time.time()
    if ab is None or rem2 <= 1.0:
        return {"ab": ab, "ba": None, "winner": None, "consistent": False,
                "seconds": round(time.time() - t0, 2), "timeout": rem2 <= 1.0}
    ba = model.chat_choice(_pair_messages(pb, pa, prompt), temperature=0.0, timeout=min(CALL_TIMEOUT_S, rem2))
    dt = round(time.time() - t0, 2)
    if ba is None:
        return {"ab": ab, "ba": None, "winner": None, "consistent": False, "seconds": dt, "timeout": time.time() >= deadline}
    ca, cb = ab.get("choice"), ba.get("choice")
    if ca == "A" and cb == "B":
        winner, consistent = "A", True
    elif ca == "B" and cb == "A":
        winner, consistent = "B", True
    else:
        winner, consistent = "tie", False
    return {"ab": ab, "ba": ba, "winner": winner, "consistent": consistent, "seconds": dt, "timeout": False}


def _variant_summary(v: dict) -> dict:
    return {
        "id": v["id"],
        "strategy": v.get("strategy"),
        "score": v.get("score"),
        "score_parts": v.get("score_parts"),
        "blocking": v.get("blocking", 0),
        "max_shift_mm": v.get("max_shift_mm", 0.0),
    }


def select(
    variants_list: list[dict],
    *,
    store_root: Optional[Path] = None,
    work_dir: Optional[Path] = None,
    lock_path: Optional[Path] = None,
    model_factory: Optional[Callable[[], object]] = None,
    enabled: Optional[bool] = None,
    budget_s: float = BUDGET_S,
    profile_name: Optional[str] = None,
    on_wait: Callable[[str], None] = lambda m: None,
) -> dict:
    """Chooses the cleanest variant from the admissible variants (with 'id', 'score', 'png').

    Always returns a complete choice.json dict (never an exception to the outside)."""
    started = time.time()
    deadline = started + float(budget_s)
    ordered = sorted(variants_list, key=lambda v: (-float(v.get("score", 0.0)), v["id"]))
    defender = ordered[0]
    base = {
        "enabled": bool(enabled) if enabled is not None else VISION_LAYOUT_DEFAULT,
        "model_profile": profile_name,
        "variants": [_variant_summary(v) for v in ordered],
        "defender_initial": defender["id"],
        "sanity_check": None,
        "pairs": [],
        "chosen": defender["id"],
        "chosen_by": "score",
        "fallback_reason": None,
        "total_seconds": 0.0,
    }

    if len(ordered) < 2:
        base["fallback_reason"] = "only one valid variant available"
        base["total_seconds"] = round(time.time() - started, 2)
        return base

    prof = None
    if model_factory is None:
        prof, cfg_enabled, cfg_err = read_config(store_root, profile_name=profile_name)
        use_model = cfg_enabled if enabled is None else bool(enabled)
        base["enabled"] = use_model
        if prof is not None:
            base["model_profile"] = prof.get("name")
        if not use_model:
            base["fallback_reason"] = "vision choice disabled"
            base["total_seconds"] = round(time.time() - started, 2)
            return base
        if prof is None:
            base["fallback_reason"] = cfg_err or "model profile not available"
            base["total_seconds"] = round(time.time() - started, 2)
            return base
        wdir = Path(work_dir) if work_dir is not None else Path(store_root) / "local-llm"
        lpath = Path(lock_path) if lock_path is not None else Path(store_root) / "local-model.lock"
        wait_budget = max(5.0, min(60.0, budget_s * 0.25))
        safe_prof = prepare_profile(prof)
        model_factory = lambda: llm.LocalModel(safe_prof, wdir, lpath, on_wait=on_wait, wait_s=wait_budget,
                                               min_free_gb=safe_prof["min_free_gb"], deadline=deadline)
    else:
        use_model = VISION_LAYOUT_DEFAULT if enabled is None else bool(enabled)
        base["enabled"] = use_model
        if not use_model:
            base["fallback_reason"] = "vision choice disabled"
            base["total_seconds"] = round(time.time() - started, 2)
            return base

    try:
        model = model_factory()
        if getattr(model, "cfg", None) and isinstance(model.cfg, dict) and model.cfg.get("name"):
            base["model_profile"] = model.cfg["name"]
        with model:
            if time.time() >= deadline:
                base["fallback_reason"] = "time budget exceeded at model start"
                base["total_seconds"] = round(time.time() - started, 2)
                return base

            # 1. Built-in sight test (clean vs. messy in both orders)
            clean_png, messy_png = sanity_images()
            sc = compare_pair(model, clean_png, messy_png, deadline)
            passed = sc["consistent"] and sc["winner"] == "A"
            base["sanity_check"] = {
                "passed": passed,
                "clean_first": sc["ab"],
                "messy_first": sc["ba"],
                "consistent": sc["consistent"],
                "winner": "clean" if sc["winner"] == "A" else ("messy" if sc["winner"] == "B" else sc["winner"]),
                "seconds": sc["seconds"],
            }
            if sc["timeout"] or time.time() >= deadline:
                base["fallback_reason"] = "time budget exceeded in the sight test"
                base["total_seconds"] = round(time.time() - started, 2)
                return base
            if not passed:
                emergency = getattr(model, "emergency", None)
                if emergency:
                    reason = emergency
                elif sc["ab"] is None or sc["ba"] is None:
                    reason = "sight test failed (invalid answer)"
                elif not sc["consistent"]:
                    reason = "sight test failed (order-dependent)"
                else:
                    reason = "sight test failed (messy sheet preferred)"
                base["fallback_reason"] = reason
                base["total_seconds"] = round(time.time() - started, 2)
                return base

            # 2. Pair comparisons: challenger against current defender
            cur = defender
            for chal in ordered[1:]:
                if time.time() >= deadline:
                    base["chosen"] = defender["id"]
                    base["chosen_by"] = "score"
                    base["fallback_reason"] = "time budget exceeded during the pair comparisons"
                    base["total_seconds"] = round(time.time() - started, 2)
                    return base
                cmp_res = compare_pair(model, cur["png"], chal["png"], deadline)
                if cmp_res["timeout"] or (cmp_res["ab"] is None or cmp_res["ba"] is None):
                    base["pairs"].append({
                        "defender": cur["id"], "challenger": chal["id"],
                        "order_dc": cmp_res["ab"], "order_cd": cmp_res["ba"],
                        "consistent": False, "winner": cur["id"], "seconds": cmp_res["seconds"],
                        "aborted": True,
                    })
                    base["chosen"] = defender["id"]
                    base["chosen_by"] = "score"
                    base["fallback_reason"] = (
                        getattr(model, "emergency", None)
                        or ("time budget exceeded during the pair comparisons" if cmp_res["timeout"]
                            else "model answer invalid")
                    )
                    base["total_seconds"] = round(time.time() - started, 2)
                    return base
                challenger_won = cmp_res["consistent"] and cmp_res["winner"] == "B"
                winner = chal if challenger_won else cur
                base["pairs"].append({
                    "defender": cur["id"], "challenger": chal["id"],
                    "order_dc": cmp_res["ab"], "order_cd": cmp_res["ba"],
                    "consistent": cmp_res["consistent"], "winner": winner["id"],
                    "seconds": cmp_res["seconds"],
                })
                cur = winner

            base["chosen"] = cur["id"]
            base["chosen_by"] = "minicpm-v"
            base["fallback_reason"] = None
            base["total_seconds"] = round(time.time() - started, 2)
            return base
    except llm.ModelBusy as e:
        base["fallback_reason"] = str(e)
    except Exception as e:
        base["fallback_reason"] = f"model error ({type(e).__name__}: {e})"
    base["chosen"] = defender["id"]
    base["chosen_by"] = "score"
    base["total_seconds"] = round(time.time() - started, 2)
    return base
