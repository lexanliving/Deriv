"""Recalibrated Council: hard/soft/informational tiers, volatility-adaptive
thresholds, calibration logging. Deterministic, millisecond-scale, no I/O."""
from __future__ import annotations
import json, os, threading, time
from collections import Counter
from datetime import datetime, timezone
from config import LOG_DIR
from . import analysis, scorers
from .indicators import clamp

CAL_FILE = os.path.join(LOG_DIR, "council_calibration.jsonl")
_cal_lock = threading.Lock()
_CAL = {"reviews": 0, "approved": 0, "caution": 0, "rejected": 0,
        "hard": Counter(), "low_conf": 0, "factor_sum": Counter(), "factor_n": Counter()}

def _record_cal(entry: dict) -> None:
    with _cal_lock:
        _CAL["reviews"] += 1
        oc = entry.get("outcome")
        if oc == "APPROVE":
            _CAL["approved"] += 1
        elif oc == "CAUTION":
            _CAL["caution"] += 1
        else:
            _CAL["rejected"] += 1
        for hr in entry.get("hard", []):
            _CAL["hard"][hr] += 1
        if entry.get("low_conf"):
            _CAL["low_conf"] += 1
        for name, sc in entry.get("factors", []):
            _CAL["factor_sum"][name] += sc
            _CAL["factor_n"][name] += 1
    try:
        with _cal_lock:
            with open(CAL_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
    except Exception:
        pass

def get_calibration() -> dict:
    with _cal_lock:
        rev = _CAL["reviews"] or 1
        return {"reviews": _CAL["reviews"], "approved": _CAL["approved"],
                "caution": _CAL["caution"], "rejected": _CAL["rejected"],
                "approval_rate": round((_CAL["approved"] + _CAL["caution"]) / rev * 100, 1),
                "hard_rejects": dict(_CAL["hard"]), "low_confidence": _CAL["low_conf"],
                "factor_avg": {k: round(_CAL["factor_sum"][k] / max(1, _CAL["factor_n"][k]), 2)
                               for k in _CAL["factor_sum"]}}

def review(setup: dict, state) -> dict:
    t0 = time.perf_counter()
    direction = setup.get("direction")
    candles = state.get_candles_5m() if state else []
    ticks = state.get_recent_ticks() if state else []

    if len(candles) < 30:
        out = _finish(True, "APPROVE", 60, ["insufficient candles; risk-taker default approve"], [], 55, 40, [], t0)
        _record_cal({**out, "outcome": "APPROVE", "hard": [], "low_conf": False, "factors": []})
        return out

    snap = analysis.build_snapshot(candles, ticks, datetime.now(timezone.utc).hour, direction)

    # HARD rules: only true safety vetoes.
    hard = scorers.hard_rules(snap)
    if hard:
        names = [h[0] for h in hard]
        out = _finish(False, "REJECT", 5, [f"HARD {n}: {r}" for n, r in hard], [], 55, 40, [], t0)
        _record_cal({**out, "outcome": "REJECT", "hard": names, "low_conf": False, "factors": []})
        return out

    # SOFT rules: weighted confidence; no single factor can reject.
    reasons = []; acc = 0.0; factors = []
    for w, name, fn in scorers.SOFT:
        sc, why = fn(snap)
        acc += w * sc
        reasons.append(f"{name}={sc:.2f} ({why})")
        factors.append((name, round(sc, 2)))
    confidence = int(round(clamp(acc) * 100))
    info = scorers.informational(snap)
    reasons += [f"[info] {n}={v}" for n, v in info]

    # Dynamic thresholds from market state.
    clean = snap["chop"] < 45 and snap["efficiency"] > 0.35
    noisy = snap["chop"] > 60 or snap["vol_ratio_regime"] > 1.5
    approve_thr = int(clamp(55 - (5 if clean else 0) + (5 if noisy else 0), 45, 65))
    reject_thr = approve_thr - 15

    if confidence >= approve_thr:
        outcome = "APPROVE"
    elif confidence >= reject_thr:
        outcome = "CAUTION"
    else:
        outcome = "REJECT"
    approved = outcome != "REJECT"
    weakest = sorted(factors, key=lambda kv: kv[1])[:2]
    out = _finish(approved, outcome, confidence, reasons, info, approve_thr, reject_thr, weakest, t0)
    _record_cal({**out, "outcome": outcome, "hard": [], "low_conf": outcome == "REJECT", "factors": factors})
    return out

def _finish(approved, outcome, confidence, reasons, info, approve_thr, reject_thr, weakest, t0) -> dict:
    return {"approved": approved, "outcome": outcome, "confidence": confidence,
            "reasons": reasons, "informational": info, "reasoning": " | ".join(reasons),
            "approve_thr": approve_thr, "reject_thr": reject_thr, "weakest": weakest,
            "thinking_ms": round((time.perf_counter() - t0) * 1000.0, 2)}
