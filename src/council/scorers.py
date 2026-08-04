"""Council scorers: strict on obvious wrong-from-start entries, but not trade-choking."""

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
    COUNCIL_PROJECTION_MIN = 0.35
    COUNCIL_WRONG_START_MIN_BODY = 0.20
    COUNCIL_WRONG_START_MIN_CLOSE_POSITION = 0.35

EXTREME_VOL = 0.008
COUNTER_SLOPE = 0.002
CHOP_REJECT = 68


def clamp_dur(x):
    return max(1, min(60, int(round(x))))


def _close_price(s):
    closes = s.get("closes") or []
    return float(closes[-1]) if closes else 0.0


def _expected_move(s, duration):
    """
    Expected absolute price move over the selected duration.
    """
    close = _close_price(s)

    if close <= 0:
        return 0.0

    vol_short = float(s.get("vol_short", 0.0) or 0.0)

    return close * vol_short * math.sqrt(max(duration, 1) / 5.0)


def _atr_abs(s):
    return float(s.get("atr_abs", 0.0) or 0.0)


def duration_advice(s, duration):
    em = _expected_move(s, duration)
    needed = 0.15 * _atr_abs(s)

    if em < needed and float(s.get("vol_short", 0.0) or 0.0) > 1e-9:
        rec = clamp_dur(5 * (needed / max(float(s.get("vol_short", 1e-9)), 1e-9)) ** 2)
        return False, f"too slow for {duration}m (exp={em:.5f} < need={needed:.5f}); rec≈{rec}m", rec

    return True, f"duration fit ok (exp={em:.5f} >= need={needed:.5f})", int(duration)


def projection_gate(s, duration):
    """
    Duration/strength projection.
    This is now in absolute price units and deliberately not over-strict.
    """
    close = _close_price(s)
    atr_abs = _atr_abs(s)

    if close <= 0 or atr_abs <= 1e-12:
        return True, "projection skipped: no price/atr evidence", 0.5, 0.0

    em = _expected_move(s, duration)
    need = 0.15 * atr_abs

    vol_short = max(float(s.get("vol_short", 0.0) or 0.0), 1e-9)

    trend_power = abs(float(s.get("slope_slow", 0.0) or 0.0)) / vol_short
    trend_power_norm = ind.clamp(trend_power / 2.0)

    chop = float(s.get("chop", 50.0) or 50.0)
    chop_penalty = ind.clamp((chop - 45.0) / 30.0)

    session_raw = float(s.get("session", 1.0) or 1.0)
    session_score = 1.0 if session_raw >= 0.9 else 0.65

    structure = float(s.get("structure", 0.5) or 0.5)
    efficiency = float(s.get("efficiency", 0.0) or 0.0)

    persistence = ind.clamp(
        0.32 * trend_power_norm
        + 0.28 * structure
        + 0.25 * efficiency
        + 0.15 * session_score
        - 0.20 * chop_penalty
        + 0.05
    )

    move_ok = em >= need or (em >= 0.80 * need and persistence >= COUNCIL_PROJECTION_MIN + 0.10)
    ok = move_ok and persistence >= COUNCIL_PROJECTION_MIN

    reason = (
        f"projection {duration}m exp={em:.5f} need={need:.5f} "
        f"persistence={persistence:.2f} min={COUNCIL_PROJECTION_MIN:.2f}"
    )

    if ok:
        strength_margin = (
            min(1.0, max(0.0, (em / max(need, 1e-12) - 1.0) * 1.5))
            + min(1.0, max(0.0, (persistence - COUNCIL_PROJECTION_MIN) / 0.18))
        )

        wait = max(0.0, 6.0 - strength_margin * 2.0)
    else:
        wait = 0.0

    return ok, reason, persistence, wait


def hard_rules(snap):
    out = []
    d = snap.get("direction")
    closes = snap.get("closes", [])

    if not closes or closes[-1] <= 0:
        out.append(("data_integrity", "non-positive price"))
        return out

    vol_long = float(snap.get("vol_long", 0.0) or 0.0)
    vol_short = float(snap.get("vol_short", 0.0) or 0.0)

    if vol_long > 0 and vol_short > 3.5 * vol_long and vol_short > 0.006:
        out.append((
            "extreme_whipsaw",
            f"vol_short={vol_short:.5f} >> vol_long={vol_long:.5f}",
        ))

    adverse = (-snap.get("slope_fast", 0.0)) if d == "BUY" else snap.get("slope_fast", 0.0)
    vol_adj = max(vol_short, 1e-6)

    if adverse > 5.0 * vol_adj and adverse > 0.004:
        out.append((
            "violent_counter_trend",
            f"adverse slope={snap.get('slope_fast', 0.0):.5f}",
        ))

    body = float(snap.get("body_ratio", 0.0) or 0.0)
    cp = float(snap.get("close_position", 0.5) or 0.5)
    e_fast = snap.get("e_fast") or []
    last_close = float(closes[-1])
    atr_abs = _atr_abs(snap)
    rsi = float(snap.get("rsi", 50.0) or 50.0)

    min_cp = COUNCIL_WRONG_START_MIN_CLOSE_POSITION

    if d == "BUY":
        if cp < min_cp:
            out.append((
                "weak_close_for_buy",
                f"close_position={cp:.2f} < {min_cp:.2f}",
            ))

        if e_fast and atr_abs > 0 and last_close < e_fast[-1] - 0.05 * atr_abs:
            out.append((
                "close_not_above_fast_ema",
                f"close={last_close:.5f} below fast EMA by more than 0.05 ATR",
            ))

        if rsi > 92.0:
            out.append((
                "overextended_buy",
                f"rsi={rsi:.1f} > 92",
            ))

        recent = float(snap.get("recent_ret_3", 0.0) or 0.0)
        if recent < -0.003 and adverse > 3.0 * vol_adj:
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

        if e_fast and atr_abs > 0 and last_close > e_fast[-1] + 0.05 * atr_abs:
            out.append((
                "close_not_below_fast_ema",
                f"close={last_close:.5f} above fast EMA by more than 0.05 ATR",
            ))

        if rsi < 8.0:
            out.append((
                "overextended_sell",
                f"rsi={rsi:.1f} < 8",
            ))

        recent = float(snap.get("recent_ret_3", 0.0) or 0.0)
        if recent > 0.003 and adverse > 3.0 * vol_adj:
            out.append((
                "counter_momentum_sell",
                f"recent_ret_3={recent:.5f} while slope is adverse",
            ))

    if body < COUNCIL_WRONG_START_MIN_BODY:
        out.append((
            "weak_trigger_body",
            f"body_ratio={body:.2f} < {COUNCIL_WRONG_START_MIN_BODY:.2f}",
        ))

    return out


def core_floors(snap):
    """
    Light core floors. Enough to block junk, not enough to choke valid trades.
    """
    out = []

    d = snap.get("direction")

    ts, _ = s_trend_structure(snap)
    mo, _ = s_momentum(snap)

    if ts < 0.30:
        out.append(("core_trend_floor", f"trend_structure={ts:.2f} < 0.30 (weak core)"))

    aligned = (
        snap["e_fast"][-1] > snap["e_slow"][-1]
        if d == "BUY"
        else snap["e_fast"][-1] < snap["e_slow"][-1]
    )

    if aligned and mo < 0.25 and ts < 0.45:
        out.append((
            "coherence",
            f"trend aligned but momentum={mo:.2f} and structure weak (incoherent)",
        ))

    ok, reason, rec = duration_advice(snap, snap.get("duration", 30))

    if not ok:
        out.append(("duration_fit", reason))

    return out, rec


def s_trend_structure(s):
    d = s.get("direction")

    aligned = (
        s["e_fast"][-1] > s["e_slow"][-1]
        if d == "BUY"
        else s["e_fast"][-1] < s["e_slow"][-1]
    )

    slope_ok = (s.get("slope_slow", 0.0) > 0) if d == "BUY" else (s.get("slope_slow", 0.0) < 0)

    score = ind.clamp(
        0.45 * aligned
        + 0.25 * slope_ok
        + 0.30 * float(s.get("structure", 0.5) or 0.5)
    )

    return score, f"aligned={aligned} slope_ok={slope_ok} structure={s.get('structure', 0.0):.2f}"


def s_momentum(s):
    d = s.get("direction")

    rsi = float(s.get("rsi", 50.0) or 50.0)
    target = 60 if d == "BUY" else 40

    rsi_score = ind.clamp(1 - abs(rsi - target) / 30)
    slope_ok = (s.get("slope_fast", 0.0) > 0) if d == "BUY" else (s.get("slope_fast", 0.0) < 0)

    score = ind.clamp(
        0.4 * rsi_score
        + 0.3 * slope_ok
        + 0.3 * ind.clamp(float(s.get("efficiency", 0.0) or 0.0) * 1.5)
    )

    return score, f"rsi={rsi:.1f} slope_ok={slope_ok} eff={s.get('efficiency', 0.0):.2f}"


def s_pullback(s):
    return ind.clamp(1 - float(s.get("pull_dist", 0.0) or 0.0) / 3.0), f"pull_dist={s.get('pull_dist', 0.0):.2f} ATR"


def s_volatility_noise(s):
    noisy = ind.clamp((float(s.get("vol_ratio_regime", 1.0) or 1.0) - 0.7) / 1.3)
    noise = float(s.get("noise", 0.0) or 0.0)

    return ind.clamp(1 - (0.6 * noisy + 0.4 * noise)), f"vol_regime={s.get('vol_ratio_regime', 0.0):.2f}"


def s_candle(s):
    return ind.clamp(float(s.get("candle_quality", 0.0) or 0.0)), f"candle={s.get('candle_quality', 0.0):.2f}"


def s_sr(s):
    return ind.clamp(float(s.get("sr_dist", 0.0) or 0.0) / 2.0), f"sr_dist={s.get('sr_dist', 0.0):.2f} ATR"


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
        ("session", f"{s.get('session', 0.0):.2f}"),
        ("volume_activity", f"{s.get('vol_ratio', 0.0):.2f}"),
        ("rsi", f"{s.get('rsi', 0.0):.1f}"),
        ("tick_noise", f"{s['tick_noise']:.5f}" if s.get("tick_noise") else "n/a"),
    ]
