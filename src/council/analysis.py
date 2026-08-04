"""Cached MarketSnapshot. One O(n) pass per candle; reused across scorers."""

from __future__ import annotations

from typing import Dict, Sequence

from . import indicators as ind

_SNAP_CACHE: Dict[tuple, dict] = {}
_CACHE_MAX = 8


def build_snapshot(
    candles: Sequence[dict],
    ticks: Sequence[float],
    hour_utc: int,
    direction: str,
) -> dict:
    key = (candles[-1].get("epoch") if candles else None, len(candles), direction)

    if key in _SNAP_CACHE:
        return _SNAP_CACHE[key]

    closes = [float(c.get("close") or 0) for c in candles]
    highs = [float(c.get("high") or 0) for c in candles]
    lows = [float(c.get("low") or 0) for c in candles]
    vols = [float(c.get("volume") or 0) for c in candles]

    e_fast = ind.ema(closes, 9)
    e_slow = ind.ema(closes, 21)

    slope_fast = ind.slope_pct(e_fast, 5)
    slope_slow = ind.slope_pct(e_slow, 10)

    rets = [
        (closes[i] - closes[i - 1]) / closes[i - 1]
        for i in range(1, len(closes))
        if closes[i - 1]
    ]

    vol_short = ind.stdev(rets[-10:])
    vol_long = ind.stdev(rets[-60:]) if len(rets) >= 61 else vol_short
    vol_ratio_regime = (vol_short / vol_long) if vol_long > 0 else 1.0

    trs = ind.true_ranges(candles)
    atr_abs = ind.mean(trs[-14:])
    atr = atr_abs / closes[-1] if closes and closes[-1] else 0.0

    rsi = ind.rsi_last(closes, 14)
    chop = ind.choppiness(candles, 14)

    wicks = []

    for c in candles[-14:]:
        h = float(c.get("high") or 0)
        l = float(c.get("low") or 0)
        o = float(c.get("open") or 0)
        cl = float(c.get("close") or 0)

        rng = max(h - l, 1e-9)
        wicks.append((rng - abs(cl - o)) / rng)

    noise = ind.mean(wicks)

    n = 14
    start = max(0, len(closes) - n)

    path = sum((highs[i] - lows[i]) for i in range(start, len(closes)))
    net = abs(closes[-1] - closes[start]) if len(closes) > start else 0.0
    efficiency = (net / path) if path > 0 else 0.0

    o = float(candles[-1].get("open") or 0)
    cl = closes[-1]

    rng = max(highs[-1] - lows[-1], 1e-9)
    body = abs(cl - o) / rng
    aligned = (cl > o) if direction == "BUY" else (cl < o)
    candle_quality = body if aligned else body * 0.3

    body_ratio = body
    close_position = (cl - lows[-1]) / rng if rng > 0 else 0.5
    close_vs_fast = (cl - e_fast[-1]) if e_fast else 0.0
    recent_ret_3 = (
        (closes[-1] - closes[-4]) / closes[-4]
        if len(closes) >= 4 and closes[-4]
        else 0.0
    )
    slope_slow_norm = slope_slow / max(vol_short, 1e-9)

    swing_high = max(highs[-n:])
    swing_low = min(lows[-n:])

    sr_dist = (
        ((swing_high - cl) / atr_abs)
        if (direction == "BUY" and atr_abs)
        else ((cl - swing_low) / atr_abs)
        if atr_abs
        else 0.0
    )

    pull_dist = abs(cl - e_slow[-1]) / atr_abs if atr_abs else 0.0

    half = n

    if len(highs) >= 2 * half:
        prev_hh = max(highs[-2 * half:-half])
        cur_hh = max(highs[-half:])
        prev_ll = min(lows[-2 * half:-half])
        cur_ll = min(lows[-half:])

        if direction == "BUY":
            structure = 1.0 if (cur_hh >= prev_hh and cur_ll >= prev_ll) else (0.5 if cur_ll >= prev_ll else 0.0)
        else:
            structure = 1.0 if (cur_ll <= prev_ll and cur_hh <= prev_hh) else (0.5 if cur_hh <= prev_hh else 0.0)
    else:
        structure = 0.5

    vol_sma = ind.sma(vols, 20)
    vol_ratio = (vols[-1] / vol_sma[-1]) if vol_sma and vol_sma[-1] else 1.0

    tick_noise = (
        ind.stdev([ticks[i] - ticks[i - 1] for i in range(1, len(ticks))])
        if len(ticks) > 2
        else None
    )

    session = 1.0 if 7 <= hour_utc <= 16 else 0.6

    snap = {
        "closes": closes,
        "e_fast": e_fast,
        "e_slow": e_slow,
        "slope_fast": slope_fast,
        "slope_slow": slope_slow,
        "vol_short": vol_short,
        "vol_long": vol_long,
        "vol_ratio_regime": vol_ratio_regime,
        "atr": atr,
        "atr_abs": atr_abs,
        "rsi": rsi,
        "chop": chop,
        "noise": noise,
        "efficiency": efficiency,
        "candle_quality": candle_quality,
        "body_ratio": body_ratio,
        "close_position": close_position,
        "close_vs_fast": close_vs_fast,
        "recent_ret_3": recent_ret_3,
        "slope_slow_norm": slope_slow_norm,
        "sr_dist": sr_dist,
        "pull_dist": pull_dist,
        "structure": structure,
        "vol_ratio": vol_ratio,
        "tick_noise": tick_noise,
        "session": session,
        "direction": direction,
    }

    if len(_SNAP_CACHE) > _CACHE_MAX:
        _SNAP_CACHE.clear()

    _SNAP_CACHE[key] = snap

    return snap
