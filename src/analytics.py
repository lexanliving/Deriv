"""src/analytics.py — deterministic stats for AI research prompts."""

from __future__ import annotations

import re
from typing import Any, Dict, List

FACTORS = [
    ("s_trend", "trend", 5),
    ("s_trigger", "trigger", 3),
    ("s_momentum", "momentum", 3),
    ("s_volatility", "volatility", 2),
    ("s_alignment", "alignment", 1),
    ("s_adx", "adx", 3),
    ("s_macd", "macd", 2),
    ("s_rsi_zone", "rsi_zone", 2),
    ("s_pattern", "pattern", 2),
    ("s_structure", "structure", 2),
]


def _f(d, k):
    if not d:
        return None

    v = d.get(k)

    if v is None or str(v).strip() == "":
        return None

    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def factor_profile(jrow):
    if not jrow:
        return {}

    return {
        name: {"score": _f(jrow, col), "max": mx}
        for col, name, mx in FACTORS
        if _f(jrow, col) is not None
    }


def strongest_weakest(prof):
    if not prof:
        return [], []

    ranked = sorted(prof.items(), key=lambda kv: kv[1]["score"] / kv[1]["max"], reverse=True)

    return (
        [n for n, v in ranked if v["score"] / v["max"] >= 0.66],
        [n for n, v in reversed(ranked) if v["score"] / v["max"] <= 0.34],
    )


def aggregate_stats(rows):
    closed = [r for r in rows if (r.get("outcome") or "") in ("WON", "LOST")]
    wins = [r for r in closed if r.get("outcome") == "WON"]
    losses = [r for r in closed if r.get("outcome") == "LOST"]

    pnls = [_f(r, "pnl") or 0.0 for r in closed]

    by = {}

    for r in closed:
        by.setdefault(r.get("regime") or "?", []).append(_f(r, "pnl") or 0.0)

    return {
        "closed": len(closed),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(closed) * 100, 1) if closed else 0.0,
        "net_pnl": round(sum(pnls), 2),
        "by_regime": {
            k: {"n": len(v), "pnl": round(sum(v), 2)}
            for k, v in by.items()
        },
    }


def _slug(s):
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")[:80] or "generic"


def knowledge_rows_from(rec):
    rows = []
    won = rec.get("outcome") == "WON"
    tid = rec.get("trade_id")

    if rec.get("pattern_detected"):
        rows.append(
            {
                "kind": "win_pattern" if won else "losing_pattern",
                "pattern_key": _slug(rec["pattern_detected"]),
                "description": rec["pattern_detected"],
                "wins": 1 if won else 0,
                "losses": 0 if won else 1,
                "last_trade_id": tid,
            }
        )

    for m in rec.get("mistakes", []):
        rows.append(
            {
                "kind": "mistake",
                "pattern_key": _slug(str(m)),
                "description": str(m),
                "wins": 0,
                "losses": 1,
                "last_trade_id": tid,
            }
        )

    for s in rec.get("strengths", []):
        rows.append(
            {
                "kind": "success",
                "pattern_key": _slug(str(s)),
                "description": str(s),
                "wins": 1,
                "losses": 0,
                "last_trade_id": tid,
            }
        )

    if rec.get("market_behaviour"):
        rows.append(
            {
                "kind": "market_behaviour",
                "pattern_key": _slug(rec["market_behaviour"]),
                "description": rec["market_behaviour"],
                "wins": 1 if won else 0,
                "losses": 0 if won else 1,
                "last_trade_id": tid,
            }
        )

    for im in rec.get("suggested_improvements", []):
        rows.append(
            {
                "kind": "improvement",
                "pattern_key": _slug(str(im)),
                "description": str(im),
                "wins": 0,
                "losses": 0,
                "last_trade_id": tid,
            }
        )

    return rows
