"""Re-valued scorers + core-quality floors + coherence + duration-fit vetoes.

New valuation: trend_structure and momentum carry the weight (they drive edge);
volatility/pullback/candle/sr are confirmatory. MACD/RSI live inside momentum
only (no double counting). Hard rules now include core floors, coherence, and
duration-fit so "25 points with stupid alignment" or "too slow for this
duration" are vetoed even if the total score is high.
"""
from __future__ import annotations
import math
from . import indicators as ind

EXTREME_VOL = 0.008
COUNTER_SLOPE = 0.002
CHOP_REJECT = 68

def _expected_move(s, duration):
    """Approx price move expected over `duration` minutes from short-term vol."""
    vol_short = s["vol_short"]
    return vol_short * math.sqrt(max(duration, 1) / 5.0)

def _atr_abs(s):
    return s["atr"] * (s["closes"][-1] if s["closes"] else 1.0)

def duration_advice(s, duration):
    """Return (ok, reason, recommended_duration). Veto if market too slow."""
    em = _expected_move(s, duration)
    needed = 0.6 * _atr_abs(s)
    if em < needed and s["vol_short"] > 0:
        rec = int(clamp_dur(5 * (needed / s["vol_short"]) ** 2))
        return False, f"too slow for {duration}m (exp={em:.5f} < need={needed:.5f}); rec≈{rec}m", rec
    return True, f"duration fit ok (exp={em:.5f} >= need={needed:.5f})", int(duration)

def clamp_dur(x):
    return max(1, min(60, int(round(x))))

def hard_rules(snap):
    out = []
    d = snap["direction"]
    if not snap["closes"] or snap["closes"][-1] <= 0:
        out.append(("data_integrity", "non-positive price"))
    if snap["vol_long"] > 0 and snap["vol_short"] > 3.0 * snap["vol_long"] and snap["vol_short"] > 0.004:
        out.append(("extreme_whipsaw", f"vol_short={snap['vol_short']:.5f} >> vol_long={snap['vol_long']:.5f}"))
    adverse = (-snap["slope_fast"]) if d == "BUY" else snap["slope_fast"]
    vol_adj = max(snap["vol_short"], 1e-6)
    if adverse > 4.0 * vol_adj and adverse > 0.003:
        out.append(("violent_counter_trend", f"adverse slope={snap['slope_fast']:.5f}"))
    return out

def core_floors(snap):
    """Core-quality floors + coherence + duration-fit. These veto weak alignment."""
    out = []
    d = snap["direction"]
    ts, _ = s_trend_structure(snap)
    mo, _ = s_momentum(snap)
    st, _ = s_sr(snap)
    if ts < 0.45:
        out.append(("core_trend_floor", f"trend_structure={ts:.2f} < 0.45 (weak core)"))
    aligned = (snap["e_fast"][-1] > snap["e_slow"][-1]) if d == "BUY" else (snap["e_fast"][-1] < snap["e_slow"][-1])
    if aligned and mo < 0.35 and ts < 0.6:
        out.append(("coherence", f"trend aligned but momentum={mo:.2f} & structure weak (incoherent)"))
    ok, reason, rec = duration_advice(snap, snap.get("duration", 30))
    if not ok:
        out.append(("duration_fit", reason))
    return out, rec

# --- SOFT (confidence only), re-valued ---
def s_trend_structure(s):
    d = s["direction"]
    aligned = (s["e_fast"][-1] > s["e_slow"][-1]) if d == "BUY" else (s["e_fast"][-1] < s["e_slow"][-1])
    slope_ok = (s["slope_slow"] > 0) if d == "BUY" else (s["slope_slow"] < 0)
    return ind.clamp(0.45 * aligned + 0.25 * slope_ok + 0.30 * s["structure"]), \
        f"aligned={aligned} slope_ok={slope_ok} structure={s['structure']:.2f}"

def s_momentum(s):
    d = s["direction"]
    target = 60 if d == "BUY" else 40
    rsi_score = ind.clamp(1 - abs(s["rsi"] - target) / 30)
    slope_ok = (s["slope_fast"] > 0) if d == "BUY" else (s["slope_fast"] < 0)
    return ind.clamp(0.4 * rsi_score + 0.3 * slope_ok + 0.3 * ind.clamp(s["efficiency"] * 1.5)), \
        f"rsi={s['rsi']:.1f} slope_ok={slope_ok} eff={s['efficiency']:.2f}"

def s_pullback(s):
    return ind.clamp(1 - s["pull_dist"] / 3.0), f"pull_dist={s['pull_dist']:.2f} ATR"

def s_volatility_noise(s):
    noisy = ind.clamp((s["vol_ratio_regime"] - 0.7) / 1.3)
    return ind.clamp(1 - (0.6 * noisy + 0.4 * s["noise"])), f"vol_regime={s['vol_ratio_regime']:.2f}"

def s_candle(s):
    return ind.clamp(s["candle_quality"]), f"candle={s['candle_quality']:.2f}"

def s_sr(s):
    return ind.clamp(s["sr_dist"] / 2.0), f"sr_dist={s['sr_dist']:.2f} ATR"

SOFT = [(0.30, "trend_structure", s_trend_structure), (0.20, "momentum", s_momentum),
        (0.15, "pullback", s_pullback), (0.15, "volatility_noise", s_volatility_noise),
        (0.10, "candle", s_candle), (0.10, "sr", s_sr)]

def informational(s):
    return [("session", f"{s['session']:.2f}"), ("volume_activity", f"{s['vol_ratio']:.2f}"),
            ("rsi", f"{s['rsi']:.1f}"), ("tick_noise", f"{s['tick_noise']:.5f}" if s["tick_noise"] else "n/a")]
