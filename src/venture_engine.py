"""src/venture_engine.py — per-trade entry council (real-time only).

Judges ONLY real-time market conditions + the live setup essentials. No past
trades, no history, no knowledge base, no intuition. For each signal it returns
{"approved": bool, "reason": str, "thinking_seconds": float}.
  * Deterministic real-time check: short-term slope aligned with direction and
    volatility not extreme.
  * Optional AI council (brain_llm) may also decline, but must base its answer
    ONLY on the provided real-time essentials; if the AI is slow/down it is
    skipped (deterministic check decides) so entries are never stuck.
On/off via set_venture_enabled (dashboard toggle).
"""
from __future__ import annotations
import json, statistics, time
from src.logger import get_logger
logger = get_logger("venture")

_enabled = True
_singleton = None

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
        aligned = (slope > 0) if direction == "BUY" else (slope < 0)
        vol_ok = vol <= 0.004
        return {"slope": slope, "vol": vol, "aligned": aligned, "vol_ok": vol_ok}
    def review(self, setup):
        if not _enabled:
            return {"approved": True, "reason": "council disabled", "thinking_seconds": 0.0}
        direction = setup.get("direction")
        rt = self._realtime(direction)
        if rt is None:
            det_ok, det_reason = True, "insufficient candles; default approve"
        else:
            det_ok = bool(rt["aligned"] and rt["vol_ok"])
            det_reason = (f"realtime slope={rt['slope']:.5f} vol={rt['vol']:.5f} "
                          f"aligned={rt['aligned']} vol_ok={rt['vol_ok']}")
        llm_ok = True; llm_reason = ""; think = 0.0
        try:
            from src import brain_llm
            t0 = time.monotonic()
            prompt = ("You are a 3-member trade-entry council (BULL, BEAR, RISK) for a Deriv binary-options "
                      "trend bot. Decide ONLY from the REAL-TIME essentials below (no history, no intuition). "
                      f"SETUP: {setup}. REALTIME: {rt}. "
                      "Approve only if current market conditions support this entry. "
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
