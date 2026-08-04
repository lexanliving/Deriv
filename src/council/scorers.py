"""Hard / Soft / Informational rule tiers. De-duplicated, volatility-adjusted."""
from __future__ import annotations
from . import indicators as ind

# --- HARD RULES (safety only). All volatility-adjusted so normal pullbacks pass.
def hard_rules(snap: dict) -> list:
    out = []
    d = snap["direction"]
    if not snap["closes"] or snap["closes"][-1] <= 0:
        out.append(("data_integrity", "non-positive price"))
    if snap["vol_long"] > 0 and snap["vol_short"] > 3.0 * snap["vol_long"] and snap["vol_short"] > 0.004:
        out.append(("extreme_whipsaw", f"vol_short={snap['vol_short']:.5f} >> vol_long={snap['vol_long']:.5f}"))
    adverse = (-snap["slope_fast"]) if d == "BUY" else snap["slope_fast"]
    vol_adj = max(snap["vol_short"], 1e-6)
    if adverse > 4.0 * vol_adj and adverse > 0.003:
        out.append(("violent_counter_trend", f"adverse slope={snap['slope_fast']:.5f} vs vol={vol_adj:.5f}"))
    return out

# --- SOFT RULES (confidence only). Each weakness counted once.
def s_trend_structure(s):
    d = s["direction"]
    aligned = (s["e_fast"][-1] > s["e_slow"][-1]) if d == "BUY" else (s["e_fast"][-1] < s["e_slow"][-1])
    slope_ok = (s["slope_slow"] > 0) if d == "BUY" else (s["slope_slow"] < 0)
    return ind.clamp(0.45 * aligned + 0.25 * slope_ok + 0.30 * s["structure"]), \
        f"emas_aligned={aligned} slope_ok={slope_ok} structure={s['structure']:.2f}"

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
    return ind.clamp(1 - (0.6 * noisy + 0.4 * s["noise"])), \
        f"vol_regime={s['vol_ratio_regime']:.2f} noise={s['noise']:.2f}"

def s_candle(s):
    return ind.clamp(s["candle_quality"]), f"candle={s['candle_quality']:.2f}"

def s_sr(s):
    return ind.clamp(s["sr_dist"] / 2.0), f"sr_dist={s['sr_dist']:.2f} ATR"

SOFT = [(0.28, "trend_structure", s_trend_structure), (0.18, "momentum", s_momentum),
        (0.18, "pullback", s_pullback), (0.20, "volatility_noise", s_volatility_noise),
        (0.10, "candle", s_candle), (0.10, "sr", s_sr)]

# --- INFORMATIONAL (record only, never reject).
def informational(s):
    return [("session", f"{s['session']:.2f}"), ("volume_activity", f"{s['vol_ratio']:.2f}"),
            ("rsi", f"{s['rsi']:.1f}"), ("tick_noise", f"{s['tick_noise']:.5f}" if s["tick_noise"] else "n/a")]
