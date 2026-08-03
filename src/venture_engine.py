"""src/venture_engine.py — periodic AI venture advisor (the 'discusser' gate).

Advisory-first: it NEVER blocks or shrinks a trade while trade history is thin
(below MIN_HISTORY_TRADES closed trades) — it is forced to NEUTRAL / 1.0 then.
It judges from market conditions + general trading knowledge, not from missing
history. A runtime on/off switch (set_venture_enabled) lets the user turn the
governor fully off (purely advisory).
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
MIN_HISTORY_TRADES = 5  # below this closed-trade count the governor is advisory-only

DEFAULT_ADVICE = {"verdict": "NEUTRAL", "risk_multiplier": 1.0, "max_risk_pct": None, "discussion": {},
                  "reasoning": "No advice yet.", "confidence": 0, "period_days": 30, "created_at": ""}

_enabled = True  # runtime on/off (dashboard toggle)

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
            return dict(DEFAULT_ADVICE)  # off => purely advisory, never blocks
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
    def _market_context(self):
        st = self._state
        if st is None:
            return {}
        try:
            s = st.get_strategy_state()
            return {"trend": s.get("trend_direction"), "mtf_bias": s.get("mtf_bias"),
                    "mtf_agreement": s.get("mtf_agreement"), "mtf_tf_biases": s.get("mtf_tf_biases", {}),
                    "micro_bias": s.get("micro_bias")}
        except Exception:
            return {}
    def _context(self):
        rows = get_journal().read_archive_merged(); now = datetime.now(timezone.utc); windows = {}
        total_closed = 0
        for d in WINDOWS_DAYS:
            cutoff = (now - timedelta(days=d)).strftime("%Y-%m-%d %H:%M:%S")
            sub = [r for r in rows if (r.get("timestamp_utc") or "") >= cutoff]
            closed = [r for r in sub if r.get("outcome") in ("WON", "LOST")]
            total_closed = max(total_closed, len(closed))
            wins = [r for r in closed if r.get("outcome") == "WON"]; losses = [r for r in closed if r.get("outcome") == "LOST"]
            pnls = [analytics._f(r, "pnl") or 0.0 for r in closed]; avoid = fragile = 0
            for r in losses:
                mae, mfe = analytics._f(r, "mae"), analytics._f(r, "mfe")
                if mfe is not None and mfe > 0 and (mae is None or mfe > mae * 0.5):
                    avoid += 1
            for r in wins:
                mae, mfe = analytics._f(r, "mae"), analytics._f(r, "mfe")
                if mae is not None and mae > 0 and (mfe is None or mae > mfe * 0.5):
                    fragile += 1
            windows[d] = {"reviews": len(sub), "closed": len(closed), "wins": len(wins), "losses": len(losses),
                          "win_rate": round(len(wins) / len(closed) * 100, 1) if closed else 0.0,
                          "net_pnl": round(sum(pnls), 2),
                          "avoidable_loss_frac": round(avoid / len(losses) * 100, 1) if losses else 0.0,
                          "fragile_win_frac": round(fragile / len(wins) * 100, 1) if wins else 0.0}
        return {"windows": windows, "total_closed": total_closed,
                "overall": analytics.aggregate_stats(rows), "market": self._market_context()}
    def _discuss(self):
        ctx = self._context(); sb = get_supabase()
        prior = sb.fetch_recent_research(6) if sb.enabled else []
        prompt = ("You are a panel of three quantitative discussers (BULL, BEAR, RISK) advising whether a Deriv "
                  "binary-options trend bot should trade now and how much to risk. Judge using the CURRENT MARKET "
                  "CONDITIONS provided and your general trading knowledge. Do NOT output POOR or a tiny risk multiplier "
                  "simply because there is little or no trade history; if history is thin, base the verdict on the market "
                  f"conditions and default to NEUTRAL or CAUTION. DATA: {ctx} PRIOR RESEARCH: {prior}. "
                  "Answer ONLY JSON: {\"verdict\":\"GOOD|CAUTION|POOR\",\"risk_multiplier\":0..1,\"max_risk_pct\":number|null,"
                  "\"discussion\":{\"bull\":str,\"bear\":str,\"risk\":str},\"reasoning\":str,\"confidence\":0-100,\"period_days\":30}")
        advice = dict(DEFAULT_ADVICE)
        try:
            from src import brain_llm
            text = brain_llm.chat_with_chain([{"role": "user", "content": prompt}], max_tokens=900)
            import re as _re
            m = _re.search(r"\{.*\}", text or "", _re.DOTALL)
            if m:
                obj = json.loads(m.group(0))
                if isinstance(obj, dict):
                    advice.update({k: obj.get(k, advice.get(k)) for k in DEFAULT_ADVICE})
        except Exception as exc:
            logger.warning("Venture AI unavailable; neutral advice: %s", exc)
        try:
            advice["risk_multiplier"] = max(0.0, min(1.0, float(advice.get("risk_multiplier", 1.0))))
        except Exception:
            advice["risk_multiplier"] = 1.0
        if advice.get("verdict") not in ("GOOD", "CAUTION", "POOR", "NEUTRAL"):
            advice["verdict"] = "NEUTRAL"
        # NO-HISTORY GUARD: advisory-only until enough closed trades exist.
        if int(ctx.get("total_closed", 0)) < MIN_HISTORY_TRADES:
            if advice.get("verdict") == "POOR":
                advice["verdict"] = "CAUTION"
            advice["risk_multiplier"] = max(advice.get("risk_multiplier", 1.0), 1.0)
            advice["reasoning"] = ("Insufficient trade history — advisory only, not controlling. " + str(advice.get("reasoning", "")))
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
        logger.info("Venture advice: %s (mult %.2f)", advice.get("verdict"), advice.get("risk_multiplier", 1.0))

_singleton = None
def get_venture_engine():
    global _singleton
    if _singleton is None:
        _singleton = VentureEngine()
    return _singleton

def get_venture_advice():
    """Synchronous, never-blocking read used by the trade gate."""
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
