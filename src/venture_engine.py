"""src/venture_engine.py — AI venture advisor that ACTS on market conditions.

Now market-driven and deterministic (works even if the AI is down):
  * risk_multiplier comes from multi-timeframe agreement + micro-bias conflict,
    so it always scales stake (does work) instead of sitting at 1.0.
  * verdict POOR (hard stop) only on a clear timeframe conflict (agreement 0 or
    strong micro conflict) — never because trade history is missing.
  * The AI (brain_llm) may only make it MORE conservative (min), never more
    aggressive; when history is thin the AI is advisory-only (cannot override).
On/off via set_venture_enabled (dashboard toggle).
"""
from __future__ import annotations
import json, os, threading, time
from datetime import datetime, timedelta, timezone
from config import LOG_DIR
from src import analytics
from src.journal import get_journal
from src.logger import get_logger
from src.supabase_service import get_supabase
logger = get_logger("venture")
ADVICE_FILE = os.path.join(LOG_DIR, "venture_advice.json")
VENTURE_INTERVAL_SECONDS = 300
WINDOWS_DAYS = (7, 30, 90)
MIN_HISTORY_TRADES = 5
DEFAULT_ADVICE = {"verdict": "NEUTRAL", "risk_multiplier": 1.0, "max_risk_pct": None, "discussion": {},
                  "reasoning": "No advice yet.", "confidence": 0, "period_days": 30, "created_at": ""}
_enabled = True
def set_venture_enabled(on: bool) -> None:
    global _enabled
    _enabled = bool(on)
def is_venture_enabled() -> bool:
    return _enabled
class VentureEngine:
    def __init__(self):
        self._state = None; self._stop = threading.Event(); self._started = False
        self._lock = threading.Lock(); self._advice = self._load_local()
    def attach(self, state):
        with self._lock:
            self._state = state
            if self._started:
                return
            self._started = True
        threading.Thread(target=self._run, daemon=True, name="venture-engine").start()
        logger.info("Venture engine started.")
    def _load_local(self):
        try:
            if os.path.exists(ADVICE_FILE):
                with open(ADVICE_FILE, "r", encoding="utf-8") as f:
                    return {**DEFAULT_ADVICE, **json.load(f)}
        except Exception:
            pass
        return dict(DEFAULT_ADVICE)
    def current_advice(self):
        if not is_venture_enabled():
            return dict(DEFAULT_ADVICE)
        with self._lock:
            return dict(self._advice)
    def _run(self):
        time.sleep(10)
        while not self._stop.is_set():
            try:
                if is_venture_enabled():
                    self._discuss()
            except Exception as exc:
                logger.error("Venture discussion failed: %s", exc)
            self._stop.wait(VENTURE_INTERVAL_SECONDS)
    def _market_state(self):
        st = self._state
        if st is None:
            return {}
        try:
            return st.get_strategy_state()
        except Exception:
            return {}
    def _market_multiplier(self, s: dict) -> float:
        """Deterministic market-based risk multiplier (0..1). Works without any
        trade history and without the AI, so the venture always 'does work'."""
        trend = s.get("trend_direction")
        if trend is None:
            return 1.0  # strategy won't trade anyway
        agree = int(s.get("mtf_agreement") or 0)
        base = {0: 0.0, 1: 0.5, 2: 0.7, 3: 0.9, 4: 1.0}.get(agree, 1.0)
        micro = s.get("micro_bias")
        if micro and trend and micro != trend:
            base = max(0.0, base - 0.25)
        return base
    def _context(self):
        rows = get_journal().read_archive_merged(); now = datetime.now(timezone.utc); windows = {}; total_closed = 0
        for d in WINDOWS_DAYS:
            cutoff = (now - timedelta(days=d)).strftime("%Y-%m-%d %H:%M:%S")
            sub = [r for r in rows if (r.get("timestamp_utc") or "") >= cutoff]
            closed = [r for r in sub if r.get("outcome") in ("WON", "LOST")]
            total_closed = max(total_closed, len(closed))
            wins = [r for r in closed if r.get("outcome") == "WON"]; losses = [r for r in closed if r.get("outcome") == "LOST"]
            pnls = [analytics._f(r, "pnl") or 0.0 for r in closed]
            windows[d] = {"reviews": len(sub), "closed": len(closed), "wins": len(wins), "losses": len(losses),
                          "win_rate": round(len(wins) / len(closed) * 100, 1) if closed else 0.0,
                          "net_pnl": round(sum(pnls), 2)}
        return {"windows": windows, "total_closed": total_closed,
                "overall": analytics.aggregate_stats(rows), "market": self._market_state()}
    def _discuss(self):
        ctx = self._context(); sb = get_supabase()
        mkt = ctx.get("market", {})
        deterministic = self._market_multiplier(mkt)
        thin_history = int(ctx.get("total_closed", 0)) < MIN_HISTORY_TRADES
        # AI may only be MORE conservative; when history is thin it is advisory-only.
        llm_mult = None; discussion = {}; reasoning = ""
        try:
            from src import brain_llm
            prior = sb.fetch_recent_research(6) if sb.enabled else []
            prompt = ("You are a panel of three quantitative discussers (BULL, BEAR, RISK) advising risk for a Deriv "
                      "binary-options trend bot. Judge using CURRENT MARKET CONDITIONS and general trading knowledge, NOT "
                      "trade history. Return a risk_multiplier in 0..1 (lower = more conservative) and a short discussion. "
                      f"MARKET: {mkt} STATS: {ctx.get('overall')} PRIOR RESEARCH: {prior}. "
                      "Answer ONLY JSON: {\"risk_multiplier\":0..1,\"discussion\":{\"bull\":str,\"bear\":str,\"risk\":str},\"reasoning\":str}")
            text = brain_llm.chat_with_chain([{"role": "user", "content": prompt}], max_tokens=500)
            import re as _re
            m = _re.search(r"\{.*\}", text or "", _re.DOTALL)
            if m:
                obj = json.loads(m.group(0))
                if isinstance(obj, dict):
                    try:
                        llm_mult = max(0.0, min(1.0, float(obj.get("risk_multiplier", 1.0))))
                    except Exception:
                        llm_mult = None
                    discussion = obj.get("discussion", {}) or {}
                    reasoning = str(obj.get("reasoning", ""))
        except Exception as exc:
            logger.warning("Venture AI unavailable; using market-only control: %s", exc)
        if llm_mult is None or thin_history:
            final = deterministic  # AI advisory-only when thin or down
        else:
            final = min(deterministic, llm_mult)  # AI can only be more conservative
        verdict = "POOR" if final <= 0.0 else ("CAUTION" if final < 0.75 else "GOOD")
        advice = dict(DEFAULT_ADVICE)
        advice.update({"verdict": verdict, "risk_multiplier": round(final, 2),
                       "discussion": discussion, "confidence": int(final * 100), "period_days": 30})
        advice["reasoning"] = (f"Market-based control: MTF agreement={mkt.get('mtf_agreement')} "
                               f"micro={mkt.get('micro_bias')} -> multiplier {final:.2f}. " + (reasoning or ""))
        advice["created_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        with self._lock:
            self._advice = advice
        try:
            with open(ADVICE_FILE, "w", encoding="utf-8") as f:
                json.dump(advice, f, ensure_ascii=False)
        except Exception:
            pass
        if sb.enabled:
            sb.upsert_venture_advice(advice)
        logger.info("Venture advice: %s (mult %.2f) [market-driven]", advice["verdict"], advice["risk_multiplier"])
_singleton = None
def get_venture_engine():
    global _singleton
    if _singleton is None:
        _singleton = VentureEngine()
    return _singleton
def get_venture_advice():
    if not is_venture_enabled():
        return dict(DEFAULT_ADVICE)
    if _singleton is not None:
        return _singleton.current_advice()
    try:
        if os.path.exists(ADVICE_FILE):
            with open(ADVICE_FILE, "r", encoding="utf-8") as f:
                return {**DEFAULT_ADVICE, **json.load(f)}
    except Exception:
        pass
    return dict(DEFAULT_ADVICE)
