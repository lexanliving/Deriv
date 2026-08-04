"""Lightweight, deterministic, O(n) indicators. Pure math, no I/O."""
from __future__ import annotations
import math
from collections import deque
from typing import List, Sequence

def clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))

def mean(values: Sequence[float]) -> float:
    n = len(values)
    return (sum(values) / n) if n else 0.0

def ema(values: Sequence[float], period: int) -> List[float]:
    if not values:
        return []
    k = 2.0 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out

def sma(values: Sequence[float], period: int) -> List[float]:
    if not values:
        return []
    out: List[float] = []; dq: deque = deque(); s = 0.0
    for v in values:
        dq.append(v); s += v
        if len(dq) > period:
            s -= dq.popleft()
        out.append(s / len(dq))
    return out

def stdev(values: Sequence[float]) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    m = sum(values) / n
    return (sum((x - m) ** 2 for x in values) / n) ** 0.5

def slope_pct(series: Sequence[float], lookback: int) -> float:
    if len(series) < lookback + 1:
        return 0.0
    base = series[-lookback - 1]
    return (series[-1] - base) / abs(base) if base else 0.0

def true_ranges(candles: Sequence[dict]) -> List[float]:
    trs: List[float] = []; prev_close = None
    for c in candles:
        h = float(c.get("high") or 0); l = float(c.get("low") or 0)
        if prev_close is None:
            trs.append(max(h - l, 1e-9))
        else:
            trs.append(max(h - l, abs(h - prev_close), abs(l - prev_close)))
        prev_close = float(c.get("close") or 0)
    return trs

def rsi_last(closes: Sequence[float], period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    gains = 0.0; losses = 0.0
    for i in range(len(closes) - period, len(closes)):
        d = closes[i] - closes[i - 1]
        if d > 0:
            gains += d
        else:
            losses -= d
    if losses == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + gains / losses)

def choppiness(candles: Sequence[dict], n: int = 14) -> float:
    if len(candles) < n + 1:
        return 50.0
    trs = true_ranges(candles)[-n:]
    sum_atr = sum(trs)
    hh = max(float(c.get("high") or 0) for c in candles[-n:])
    ll = min(float(c.get("low") or 0) for c in candles[-n:])
    rng = hh - ll
    if rng <= 0 or sum_atr <= 0:
        return 50.0
    return 100.0 * math.log10(sum_atr / rng) / math.log10(n)
