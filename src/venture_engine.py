"""src/venture_engine.py — per-trade entry council (risk-taker sniper).

Permissive by design: APPROVES by default and works in tight/low-vol conditions.
It DECLINES ONLY clearly-disastrous real-time setups:
  * strong counter-trend (short-term slope hard against the trade direction), or
  * extreme volatility (price whipping, no tradable structure).
Insufficient candles => default APPROVE (never blocks on missing data).
The AI council is a second opinion instructed to be risk-tolerant; it may only
add a decline when real-time data is present and conditions are clearly bad.
If the AI is slow/down, the deterministic read decides (still permissive).
On/off via set_venture_enabled (dashboard toggle).
"""
from __future__ import annotations
import json, statistics, time
from src.logger import get_logger
logger = get_logger("venture")

_enabled = True
_singleton = None

# Sniper thresholds: only these two conditions are "completely bad".
COUNTER_SLOPE = 0.002   # ~0.2% adverse 5m slope over the last candles = strong counter-trend
EXTREME_VOL = 0.008     # ~0.8% stdev per 5m candle = extreme whipsaw

def set_venture_enabled(on: bool) -> None:
    global _enabled
    _enabled = bool(on)

def is_venture_enabled() -> bool:
    return _enabled

class VentureEngine:
    def __init__(self):
        self._state = None
    def attach(self, state):
        self._state = state
    @staticmethod
    def _ema(series, period):
        k = 2 / (period + 1); e = series[0]; out = [e]
        for x in series[1:]:
            e = x * k + e * (1 - k); out.append(e)
        return out
    def _realtime(self, direction):
        st = self._state
        if st is None:
            return None
        candles = st.get_candles_5m() or []
        closes = [float(c.get("close")) for c in candles if c.get("close") is not None]
        if len(closes) < 30:
            return None
        e9 = self._ema(closes, 9); e21 = self._ema(closes, 21)
        slope = (e9[-1] - e9[-6]) / e9[-6] if e9[-6] else 0.0
        rets = [(closes[i] - closes[i - 1]) / closes[i - 1] for i in range(1, len(closes)) if closes[i - 1]]
        vol = statistics.pstdev(rets) if len(rets) > 1 else 0.0
        counter_trend = (direction == "BUY" and slope < -COUNTER_SLOPE) or \
                        (direction == "SELL" and slope > COUNTER_SLOPE)
        extreme_vol = vol > EXTREME_VOL
        bad = bool(counter_trend or extreme_vol)
        return {"slope": slope, "vol": vol, "counter_trend": counter_trend,
                "extreme_vol": extreme_vol, "bad": bad}
    def review(self, setup):
        if not _enabled:
            return {"approved": True, "reason": "council disabled", "thinking_seconds": 0.0}
        direction = setup.get("direction")
        rt = self._realtime(direction)
        if rt is None:
            det_ok, det_reason = True, "insufficient candles; risk-taker default approve"
        else:
            det_ok = not rt["bad"]
            det_reason = (f"realtime slope={rt['slope']:.5f} vol={rt['vol']:.5f} "
                          f"counter_trend={rt['counter_trend']} extreme_vol={rt['extreme_vol']}")
        llm_ok = True; llm_reason = ""; think = 0.0
        # Only consult the AI when we have real-time data; never let it decline on
        # missing data. It is instructed to be a risk-taker and decline only clearly
        # bad conditions.
        if rt is not None:
            try:
                from src import brain_llm
                t0 = time.monotonic()
                prompt = ("You are a 3-member risk-TOLERANT trade-entry council (BULL, BEAR, RISK) for a Deriv "
                          "binary-options trend bot. You are a sniper: you take good-enough setups and decline ONLY "
                          "clearly-disastrous ones (strong counter-trend or extreme whipsaw volatility). DEFAULT TO "
                          "APPROVE. Base the decision ONLY on the REAL-TIME essentials below (no history, no intuition). "
                          f"SETUP: {setup}. REALTIME: {rt}. "
                          "Answer ONLY JSON: {\"approve\":true|false,\"reason\":str}")
                text = brain_llm.chat_with_chain([{"role": "user", "content": prompt}], temperature=0.0, max_tokens=200)
                think = time.monotonic() - t0
                import re as _re
                m = _re.search(r"\{.*\}", text or "", _re.DOTALL)
                if m:
                    obj = json.loads(m.group(0))
                    if isinstance(obj, dict):
                        llm_ok = bool(obj.get("approve", True))
                        llm_reason = str(obj.get("reason", ""))
            except Exception as exc:
                logger.warning("Council AI unavailable/slow; deterministic decides: %s", exc)
        approved = bool(det_ok and llm_ok)
        reason = det_reason + (f" | council: {llm_reason}" if llm_reason else "")
        logger.info("Council: %s (%s) [think=%.1fs]", "APPROVE" if approved else "DECLINE", reason, think)
        return {"approved": approved, "reason": reason, "thinking_seconds": round(think, 2)}

def get_venture_engine():
    global _singleton
    if _singleton is None:
        _singleton = VentureEngine()
    return _singleton

def review_entry(setup):
    return get_venture_engine().review(setup)
