"""src/venture_engine.py — periodic AI venture advisor (the 'discusser' gate).

Runs fully in the background. It reads ONLY our own stored data (local journal +
our Supabase research), asks the configured AI chain to run a multi-voice
discussion (bull / bear / risk) over recent windows, and emits a cached advisory:
  {verdict: GOOD|CAUTION|POOR, risk_multiplier: 0..1, max_risk_pct, discussion,
   reasoning, confidence, period_days, created_at}
The trade gate reads current_advice() synchronously (cached, never blocking).
Offline / AI-down => DEFAULT_ADVICE (multiplier 1.0, never blocks).
We SEND research to Supabase; we only READ our own data here.
"""
from __future__ import annotations

import json
import os
import threading
import time
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

DEFAULT_ADVICE = {
    "verdict": "NEUTRAL", "risk_multiplier": 1.0, "max_risk_pct": None,
    "discussion": {}, "reasoning": "No advice yet.", "confidence": 0,
    "period_days": 30, "created_at": "",
}


class VentureEngine:
    def __init__(self) -> None:
        self._state = None
        self._stop = threading.Event()
        self._started = False
        self._lock = threading.Lock()
        self._advice = self._load_local()

    def attach(self, state) -> None:
        with self._lock:
            self._state = state
            if self._started:
                return
            self._started = True
        threading.Thread(target=self._run, daemon=True, name="venture-engine").start()
        logger.info("Venture engine started.")

    def _load_local(self) -> dict:
        try:
            if os.path.exists(ADVICE_FILE):
                with open(ADVICE_FILE, "r", encoding="utf-8") as f:
                    return {**DEFAULT_ADVICE, **json.load(f)}
        except Exception:
            pass
        return dict(DEFAULT_ADVICE)

    def current_advice(self) -> dict:
        with self._lock:
            return dict(self._advice)

    def _run(self) -> None:
        time.sleep(10)
        while not self._stop.is_set():
            try:
                self._discuss()
            except Exception as exc:
                logger.error("Venture discussion failed: %s", exc)
            self._stop.wait(VENTURE_INTERVAL_SECONDS)

    # -- context over 7/30/90-day windows from our own stored data ----------
    def _context(self) -> dict:
        rows = get_journal().read_archive_merged()
        now = datetime.now(timezone.utc)
        windows = {}
        for d in WINDOWS_DAYS:
            cutoff = (now - timedelta(days=d)).strftime("%Y-%m-%d %H:%M:%S")
            sub = [r for r in rows if (r.get("timestamp_utc") or "") >= cutoff]
            closed = [r for r in sub if r.get("outcome") in ("WON", "LOST")]
            wins = [r for r in closed if r.get("outcome") == "WON"]
            losses = [r for r in closed if r.get("outcome") == "LOST"]
            pnls = [analytics._f(r, "pnl") or 0.0 for r in closed]
            avoid = 0
            fragile = 0
            for r in losses:
                mae, mfe = analytics._f(r, "mae"), analytics._f(r, "mfe")
                if mfe is not None and mfe > 0 and (mae is None or mfe > mae * 0.5):
                    avoid += 1
            for r in wins:
                mae, mfe = analytics._f(r, "mae"), analytics._f(r, "mfe")
                if mae is not None and mae > 0 and (mfe is None or mae > mfe * 0.5):
                    fragile += 1
            windows[d] = {
                "reviews": len(sub), "closed": len(closed), "wins": len(wins), "losses": len(losses),
                "win_rate": round(len(wins) / len(closed) * 100, 1) if closed else 0.0,
                "net_pnl": round(sum(pnls), 2),
                "avoidable_loss_frac": round(avoid / len(losses) * 100, 1) if losses else 0.0,
                "fragile_win_frac": round(fragile / len(wins) * 100, 1) if wins else 0.0,
            }
        return {"windows": windows, "overall": analytics.aggregate_stats(rows)}

    def _discuss(self) -> None:
        ctx = self._context()
        sb = get_supabase()
        prior = sb.fetch_recent_research(6) if sb.enabled else []
        prompt = (
            "You are a panel of three quantitative discussers deciding whether a Deriv "
            "binary-options trend bot is a GOOD venture right now and how much to risk. "
            "BULL argues for, BEAR argues against, RISK sets sizing. Base every claim ONLY on "
            f"the DATA below (our own stored trade research). DATA: {ctx} PRIOR RESEARCH: {prior}. "
            "Answer with ONLY JSON: {\"verdict\":\"GOOD|CAUTION|POOR\",\"risk_multiplier\":0..1,"
            "\"max_risk_pct\":number|null,\"discussion\":{\"bull\":str,\"bear\":str,\"risk\":str},"
            "\"reasoning\":str,\"confidence\":0-100,\"period_days\":30}")
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
            logger.warning("Venture AI unavailable; using neutral advice: %s", exc)
        # clamp safety: never exceed 1.0, never negative
        try:
            advice["risk_multiplier"] = max(0.0, min(1.0, float(advice.get("risk_multiplier", 1.0))))
        except Exception:
            advice["risk_multiplier"] = 1.0
        if advice.get("verdict") not in ("GOOD", "CAUTION", "POOR", "NEUTRAL"):
            advice["verdict"] = "NEUTRAL"
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


def get_venture_engine() -> VentureEngine:
    global _singleton
    if _singleton is None:
        _singleton = VentureEngine()
    return _singleton


def get_venture_advice() -> dict:
    """Synchronous, never-blocking read used by the trade gate."""
    if _singleton is not None:
        return _singleton.current_advice()
    try:
        if os.path.exists(ADVICE_FILE):
            with open(ADVICE_FILE, "r", encoding="utf-8") as f:
                return {**DEFAULT_ADVICE, **json.load(f)}
    except Exception:
        pass
    return dict(DEFAULT_ADVICE)
