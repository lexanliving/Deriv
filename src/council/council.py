"""Council with wrong-from-start protection, projection gating, and deliberation time."""

from __future__ import annotations

import json
import os
import threading
import time
from collections import Counter
from datetime import datetime, timezone

try:
    from config import (
        COUNCIL_MAX_THINK_SECONDS,
        COUNCIL_MIN_THINK_SECONDS,
        COUNCIL_PROJECTION_MIN,
        LOG_DIR,
    )
except Exception:
    COUNCIL_MIN_THINK_SECONDS = 3.0
    COUNCIL_MAX_THINK_SECONDS = 8.0
    COUNCIL_PROJECTION_MIN = 0.35
    LOG_DIR = os.path.join(os.getcwd(), "logs")

from . import analysis, scorers
from .indicators import clamp

CAL_FILE = os.path.join(LOG_DIR, "council_calibration.jsonl")

_cal_lock = threading.Lock()

_CAL = {
    "reviews": 0,
    "approved": 0,
    "caution": 0,
    "rejected": 0,
    "hard": Counter(),
    "core": Counter(),
    "projection": 0,
    "low_conf": 0,
    "factor_sum": Counter(),
    "factor_n": Counter(),
}


def _record_cal(entry):
    with _cal_lock:
        _CAL["reviews"] += 1

        oc = entry.get("outcome")
        _CAL[{"APPROVE": "approved", "CAUTION": "caution"}.get(oc, "rejected")] += 1

        for hr in entry.get("hard", []):
            _CAL["hard"][hr] += 1

        for cr in entry.get("core", []):
            _CAL["core"][cr] += 1

        if entry.get("projection_reject"):
            _CAL["projection"] += 1

        if entry.get("low_conf"):
            _CAL["low_conf"] += 1

        for name, sc in entry.get("factors", []):
            _CAL["factor_sum"][name] += sc
            _CAL["factor_n"][name] += 1

    try:
        with open(CAL_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass


def get_calibration():
    with _cal_lock:
        rev = _CAL["reviews"] or 1

        return {
            "reviews": _CAL["reviews"],
            "approved": _CAL["approved"],
            "caution": _CAL["caution"],
            "rejected": _CAL["rejected"],
            "approval_rate": round((_CAL["approved"] + _CAL["caution"]) / rev * 100, 1),
            "hard_rejects": dict(_CAL["hard"]),
            "core_rejects": dict(_CAL["core"]),
            "projection_rejects": _CAL["projection"],
            "low_confidence": _CAL["low_conf"],
            "factor_avg": {
                k: round(_CAL["factor_sum"][k] / max(1, _CAL["factor_n"][k]), 2)
                for k in _CAL["factor_sum"]
            },
        }


def review(setup, state):
    t0 = time.perf_counter()

    direction = setup.get("direction")
    symbol = setup.get("symbol", "")
    duration = int(setup.get("duration", 30))

    candles = state.get_candles_5m() if state else []
    ticks = state.get_recent_ticks() if state else []

    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    if len(candles) < 30:
        out = _finish(
            True,
            "APPROVE",
            60,
            ["insufficient candles; risk-taker default"],
            [],
            55,
            40,
            [],
            duration,
            t0,
            0.0,
        )

        out.update({"symbol": symbol, "direction": direction, "ts": now_iso})

        _record_cal({
            **out,
            "hard": [],
            "core": [],
            "projection_reject": False,
            "low_conf": False,
            "factors": [],
        })

        return out

    snap = analysis.build_snapshot(
        candles,
        ticks,
        datetime.now(timezone.utc).hour,
        direction,
    )

    snap["duration"] = duration

    hard = scorers.hard_rules(snap)

    if hard:
        out = _finish(
            False,
            "REJECT",
            5,
            [f"HARD {n}: {r}" for n, r in hard],
            [],
            55,
            40,
            [],
            duration,
            t0,
            0.0,
        )

        out.update({"symbol": symbol, "direction": direction, "ts": now_iso})

        _record_cal({
            **out,
            "hard": [h[0] for h in hard],
            "core": [],
            "projection_reject": False,
            "low_conf": False,
            "factors": [],
        })

        return out

    core, rec_dur = scorers.core_floors(snap)

    if core:
        out = _finish(
            False,
            "REJECT",
            10,
            [f"CORE {n}: {r}" for n, r in core],
            [],
            55,
            40,
            [],
            rec_dur,
            t0,
            0.0,
        )

        out.update({"symbol": symbol, "direction": direction, "ts": now_iso})

        _record_cal({
            **out,
            "hard": [],
            "core": [c[0] for c in core],
            "projection_reject": False,
            "low_conf": False,
            "factors": [],
        })

        return out

    try:
        proj_ok, proj_reason, persistence, proj_wait = scorers.projection_gate(snap, duration)
    except Exception as exc:
        proj_ok = True
        proj_reason = f"projection error fallback: {exc}"
        persistence = 0.5
        proj_wait = 0.0

    if not proj_ok:
        out = _finish(
            False,
            "REJECT",
            12,
            [f"PROJECTION {proj_reason}"],
            [],
            55,
            40,
            [],
            rec_dur,
            t0,
            0.0,
        )

        out.update({"symbol": symbol, "direction": direction, "ts": now_iso})

        _record_cal({
            **out,
            "hard": [],
            "core": [],
            "projection_reject": True,
            "low_conf": False,
            "factors": [],
        })

        return out

    reasons = []
    acc = 0.0
    factors = []

    for w, name, fn in scorers.SOFT:
        sc, why = fn(snap)

        acc += w * sc
        reasons.append(f"{name}={sc:.2f} ({why})")
        factors.append((name, round(sc, 2)))

    confidence = int(round(clamp(acc) * 100))

    if persistence < COUNCIL_PROJECTION_MIN + 0.08:
        confidence = max(0, confidence - 4)
    elif persistence > 0.72:
        confidence = min(100, confidence + 3)

    info = scorers.informational(snap) + [
        ("recommended_duration", f"{rec_dur}m"),
        ("projection_persistence", f"{persistence:.2f}"),
        ("projection", proj_reason),
    ]

    clean = snap.get("chop", 50.0) < 45 and snap.get("efficiency", 0.0) > 0.35
    noisy = snap.get("chop", 50.0) > 60 or snap.get("vol_ratio_regime", 1.0) > 1.5

    approve_thr = int(clamp(55 - (4 if clean else 0) + (4 if noisy else 0), 45, 65))

    if persistence < COUNCIL_PROJECTION_MIN + 0.06:
        approve_thr = int(clamp(approve_thr + 3, 45, 68))

    reject_thr = approve_thr - 15

    outcome = (
        "APPROVE"
        if confidence >= approve_thr
        else ("CAUTION" if confidence >= reject_thr else "REJECT")
    )

    approved = outcome != "REJECT"
    weakest = sorted(factors, key=lambda kv: kv[1])[:2]

    wait_seconds = 0.0

    if approved:
        wait_seconds = max(COUNCIL_MIN_THINK_SECONDS, float(proj_wait or 0.0))

        if outcome == "CAUTION":
            wait_seconds += 1.5

        wait_seconds = clamp(
            wait_seconds,
            COUNCIL_MIN_THINK_SECONDS,
            COUNCIL_MAX_THINK_SECONDS,
        )

    info.append(("wait_seconds", f"{wait_seconds:.1f}"))
    reasons += [f"[info] {n}={v}" for n, v in info]

    out = _finish(
        approved,
        outcome,
        confidence,
        reasons,
        info,
        approve_thr,
        reject_thr,
        weakest,
        rec_dur,
        t0,
        wait_seconds,
    )

    out.update({"symbol": symbol, "direction": direction, "ts": now_iso})

    _record_cal({
        **out,
        "hard": [],
        "core": [],
        "projection_reject": False,
        "low_conf": outcome == "REJECT",
        "factors": factors,
    })

    return out


def _finish(
    approved,
    outcome,
    confidence,
    reasons,
    info,
    approve_thr,
    reject_thr,
    weakest,
    rec_dur,
    t0,
    wait_seconds=0.0,
):
    return {
        "approved": approved,
        "outcome": outcome,
        "confidence": confidence,
        "reasons": reasons,
        "informational": info,
        "reasoning": " | ".join(reasons),
        "approve_thr": approve_thr,
        "reject_thr": reject_thr,
        "weakest": weakest,
        "recommended_duration": rec_dur,
        "thinking_ms": round((time.perf_counter() - t0) * 1000.0, 2),
        "wait_seconds": float(wait_seconds or 0.0),
    }
