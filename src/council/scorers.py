"""Re-valued scorers + core-quality floors + coherence + duration-fit vetoes.

This version adds:
- wrong-from-start protection
- projection gate for duration/strength fit
- stricter trigger-quality checks
"""

from __future__ import annotations

import math

from . import indicators as ind

try:
    from config import (
        COUNCIL_PROJECTION_MIN,
        COUNCIL_WRONG_START_MIN_BODY,
        COUNCIL_WRONG_START_MIN_CLOSE_POSITION,
    )
except Exception:
    COUNCIL_PROJECTION_MIN = 0.52
    COUNCIL_WRONG_START_MIN_BODY = 0.30
    COUNCIL_WRONG_START_MIN_CLOSE_POSITION = 0.45

EXTREME_VOL = 0.008
COUNTER_SLOPE = 0.002
CHOP_REJECT = 68


def clamp_dur(x):
    return max(1, min(60, int(round(x))))


def _expected_move(s, duration):
    """Approx price move expected over `duration` minutes from short-term vol."""
    vol_short = s["vol_short"]
    return vol_short * math.sqrt(max(duration, 1) / 5.0)


def _atr_abs(s):
    return s.get("atr_abs", 0.0) or (s["atr"] * (s["closes"][-1] if s["closes"] else 1.0))


def duration_advice(s, duration):
    """Return (ok, reason, recommended_duration). Veto if market too slow."""
    em = _expected_move(s, duration)
    needed = 0.40 * _atr_abs(s)

    if em < needed and s["vol_short"] > 0:
        rec = clamp_dur(5 * (needed / s["vol_short"]) ** 2)
        return False, f"too slow for {duration}m (exp={em:.5f} < need={needed:.5f}); rec≈{rec}m", rec

    return True, f"duration fit ok (exp={em:.5f} >= need={needed:.5f})", int(duration)


def projection_gate(s, duration):
    """
    Project whether the current move has enough strength and trend persistence
    to remain favourable through the selected duration.

    This is deliberately conservative. It is not a promise. It is a veto against
    weak, late, choppy, or incoherent entries.
    """
    em = _expected_move(s, duration)
    need = 0.35 * _atr_abs(s)

    vol_short = max(s.get("vol_short", 0.0), 1e-9)

    trend_power = abs(s.get("slope_slow", 0.0)) / vol_short
    trend_power_norm = ind.clamp(trend_power / 2.0)

    chop_penalty = ind.clamp((s.get("chop", 50.0) - 45.0) / 30.0)
    session_score = 1.0 if s.get("session", 1.0) >= 0.9 else 0.6

    persistence = ind.clamp(
        0.34 * trend_power_norm
        + 0.26 * s.get("structure", 0.5)
        + 0.24 * s.get("efficiency", 0.0)
        + 0.16 * session_score
        - 0.22 * chop_penalty
    )

    ok = em >= need and persistence >= COUNCIL_PROJECTION_MIN

    reason = (
        f"projection {duration}m exp={em:.5f} need={need:.5f} "
        f"persistence={persistence:.2f} min={COUNCIL_PROJECTION_MIN:.2f}"
    )

    if ok:
        strength_margin = (
            min(1.0, max(0.0, (em / need - 1.0) * 1.5))
            + min(1.0, max(0.0, (persistence - COUNCIL_PROJECTION_MIN) / 0.18))
        )

        # Stronger projections wait less. Marginal projections wait more.
        wait = max(0.0, 6.5 - strength_margin * 2.5)
    else:
        wait = 0.0

    return ok, reason, persistence, wait


def hard_rules(snap):
    out = []
    d = snap["direction"]
    closes = snap.get("closes", [])

    if not closes or closes[-1] <= 0:
        out.append(("data_integrity", "non-positive price"))
        return out

    vol_long = snap.get("vol_long", 0.0)
    vol_short = snap.get("vol_short", 0.0)

    if vol_long > 0 and vol_short > 3.0 * vol_long and vol_short > 0.004:
        out.append((
            "extreme_whipsaw",
            f"vol_short={vol_short:.5f} >> vol_long={vol_long:.5f}",
        ))

    adverse = (-snap.get("slope_fast", 0.0)) if d == "BUY" else snap.get("slope_fast", 0.0)
    vol_adj = max(vol_short, 1e-6)

    if adverse > 4.0 * vol_adj and adverse > 0.003:
        out.append(("violent_counter_trend", f"adverse slope={snap.get('slope_fast', 0.0):.5f}"))

    body = snap.get("body_ratio", 0.0)
    cp = snap.get("close_position", 0.5)
    e_fast = snap.get("e_fast") or []
    last_close = closes[-1]

    min_cp = COUNCIL_WRONG_START_MIN_CLOSE_POSITION

    if d == "BUY":
        if cp < min_cp:
            out.append((
                "weak_close_for_buy",
                f"close_position={cp:.2f} < {min_cp:.2f}",
            ))

        if e_fast and last_close <= e_fast[-1]:
            out.append((
                "close_not_above_fast_ema",
                f"close={last_close:.5f} <= ema_fast={e_fast[-1]:.5f}",
            ))

        if snap.get("rsi", 50.0) > 88.0:
            out.append((
                "overextended_buy",
                f"rsi={snap.get('rsi', 0.0):.1f} > 88",
            ))

        recent = snap.get("recent_ret_3", 0.0)
        if recent < -0.0012 and adverse > 2.0 * vol_adj:
            out.append((
                "counter_momentum_buy",
                f"recent_ret_3={recent:.5f} while slope is adverse",
            ))
    else:
        if cp > 1.0 - min_cp:
            out.append((
                "weak_close_for_sell",
                f"close_position={cp:.2f} > {1.0 - min_cp:.2f}",
            ))

        if e_fast and last_close >= e_fast[-1]:
            out.append((
                "close_not_below_fast_ema",
                f"close={last_close:.5f} >= ema_fast={e_fast[-1]:.5f}",
            ))

        if snap.get("rsi", 50.0) < 12.0:
            out.append((
                "overextended_sell",
                f"rsi={snap.get('rsi', 0.0):.1f} < 12",
            ))

        recent = snap.get("recent_ret_3", 0.0)
        if recent > 0.0012 and adverse > 2.0 * vol_adj:
            out.append((
                "counter_momentum_sell",
                f"recent_ret_3={recent:.5f} while slope is adverse",
            ))

    if body < COUNCIL_WRONG_START_MIN_BODY:
        out.append((
            "weak_trigger_body",
            f"body_ratio={body:.2f} < {COUNCIL_WRONG_START_MIN_BODY:.2f}",
        ))

    if snap.get("candle_quality", 0.0) < 0.18:
        out.append((
            "poor_candle_quality",
            f"candle_quality={snap.get('candle_quality', 0.0):.2f} < 0.18",
        ))

    if snap.get("efficiency", 0.0) < 0.10 and snap.get("chop", 50.0) > 62.0:
        out.append((
            "inefficient_chop",
            f"efficiency={snap.get('efficiency', 0.0):.2f} with chop={snap.get('chop', 0.0):.1f}",
        ))

    return out


def core_floors(snap):
    """Core-quality floors + coherence + duration-fit. These veto weak alignment."""
    out = []

    d = snap["direction"]

    ts, _ = s_trend_structure(snap)
    mo, _ = s_momentum(snap)

    if ts < 0.45:
        out.append(("core_trend_floor", f"trend_structure={ts:.2f} < 0.45 (weak core)"))

    aligned = (snap["e_fast"][-1] > snap["e_slow"][-1]) if d == "BUY" else (snap["e_fast"][-1] < snap["e_slow"][-1])

    if aligned and mo < 0.35 and ts < 0.6:
        out.append((
            "coherence",
            f"trend aligned but momentum={mo:.2f} & structure weak (incoherent)",
        ))

    ok, reason, rec = duration_advice(snap, snap.get("duration", 30))

    if not ok:
        out.append(("duration_fit", reason))

    return out, rec


# --- SOFT (confidence only), re-valued ---

def s_trend_structure(s):
    d = s["direction"]

    aligned = (s["e_fast"][-1] > s["e_slow"][-1]) if d == "BUY" else (s["e_fast"][-1] < s["e_slow"][-1])
    slope_ok = (s["slope_slow"] > 0) if d == "BUY" else (s["slope_slow"] < 0)

    score = ind.clamp(0.45 * aligned + 0.25 * slope_ok + 0.30 * s["structure"])

    return score, f"aligned={aligned} slope_ok={slope_ok} structure={s['structure']:.2f}"


def s_momentum(s):
    d = s["direction"]

    target = 60 if d == "BUY" else 40
    rsi_score = ind.clamp(1 - abs(s["rsi"] - target) / 30)
    slope_ok = (s["slope_fast"] > 0) if d == "BUY" else (s["slope_fast"] < 0)

    score = ind.clamp(0.4 * rsi_score + 0.3 * slope_ok + 0.3 * ind.clamp(s["efficiency"] * 1.5))

    return score, f"rsi={s['rsi']:.1f} slope_ok={slope_ok} eff={s['efficiency']:.2f}"


def s_pullback(s):
    return ind.clamp(1 - s["pull_dist"] / 3.0), f"pull_dist={s['pull_dist']:.2f} ATR"


def s_volatility_noise(s):
    noisy = ind.clamp((s["vol_ratio_regime"] - 0.7) / 1.3)
    return ind.clamp(1 - (0.6 * noisy + 0.4 * s["noise"])), f"vol_regime={s['vol_ratio_regime']:.2f}"


def s_candle(s):
    return ind.clamp(s["candle_quality"]), f"candle={s['candle_quality']:.2f}"


def s_sr(s):
    return ind.clamp(s["sr_dist"] / 2.0), f"sr_dist={s['sr_dist']:.2f} ATR"


SOFT = [
    (0.30, "trend_structure", s_trend_structure),
    (0.20, "momentum", s_momentum),
    (0.15, "pullback", s_pullback),
    (0.15, "volatility_noise", s_volatility_noise),
    (0.10, "candle", s_candle),
    (0.10, "sr", s_sr),
]


def informational(s):
    return [
        ("session", f"{s['session']:.2f}"),
        ("volume_activity", f"{s['vol_ratio']:.2f}"),
        ("rsi", f"{s['rsi']:.1f}"),
        ("tick_noise", f"{s['tick_noise']:.5f}" if s["tick_noise"] else "n/a"),
    ]
