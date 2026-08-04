"""src/research_engine.py — background research pipeline (never touches execution)."""
from __future__ import annotations
import json, os, queue, threading, time
from typing import Any, Dict, Optional, Set
from config import LOG_DIR
from src import ai_engine, analytics
from src.journal import get_journal
from src.logger import get_logger
from src.supabase_service import get_supabase
logger = get_logger("research")
DONE_FILE = os.path.join(LOG_DIR, "research_done.jsonl")
OUTBOX_FILE = os.path.join(LOG_DIR, "research_outbox.jsonl")
LOCAL_FILE = os.path.join(LOG_DIR, "research_local.jsonl")
FINAL = ("WON", "LOST", "UNKNOWN")
class ResearchEngine:
    def __init__(self):
        self._queue = queue.Queue(maxsize=200); self._stop = threading.Event()
        self._started = False; self._lock = threading.Lock(); self._state = None
        self._done = set(self._load_done())
    def attach(self, state):
        with self._lock:
            self._state = state
            if self._started:
                return
            self._started = True
        threading.Thread(target=self._run, daemon=True, name="research-worker").start()
        try:
            from src.venture_engine import get_venture_engine; get_venture_engine().attach(state)
        except Exception as exc:
            logger.warning("Venture engine not started: %s", exc)
        logger.info("Research engine attached and started.")
    def _load_done(self):
        ids = set()
        if os.path.exists(DONE_FILE):
            try:
                with open(DONE_FILE, "r", encoding="utf-8") as fh:
                    for line in fh:
                        try:
                            ids.add(json.loads(line).get("trade_id"))
                        except Exception:
                            continue
            except Exception:
                pass
        return ids
    def _append(self, path, obj):
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(obj, ensure_ascii=False) + "\n")
        except Exception as exc:
            logger.error("Local research write failed: %s", exc)
    def _mark_done(self, tid):
        self._done.add(tid); self._append(DONE_FILE, {"trade_id": tid, "ts": time.time()})
    def _run(self):
        sb = get_supabase()
        if sb.enabled:
            sb.ensure_schema()
        last_poll = last_flush = 0.0
        while not self._stop.is_set():
            now = time.time()
            if now - last_poll > 3.0:
                last_poll = now; self._poll_state()
            try:
                tid = self._queue.get(timeout=1.0)
            except queue.Empty:
                tid = None
            if tid:
                self._process(tid)
            if now - last_flush > 60.0:
                last_flush = now; self._flush_outbox()
    def _poll_state(self):
        st = self._state
        if st is None:
            return
        for t in st.get_trade_history():
            if t.status in FINAL and t.trade_id not in self._done:
                try:
                    self._queue.put_nowait(t.trade_id)
                except queue.Full:
                    logger.warning("Research queue full; dropping %s", t.trade_id)
    def _journal_row_for(self, signal_id):
        if not signal_id:
            return None
        for r in get_journal().read_archive_merged():
            if r.get("signal_id") == signal_id:
                return r
        return None
    def _process(self, tid):
        st = self._state; trade = st.get_trade(tid) if st else None
        if trade is None:
            return
        jrow = self._journal_row_for(getattr(trade, "signal_id", ""))
        ctx = {"symbol": trade.symbol, "direction": trade.direction, "outcome": trade.status, "pnl": trade.pnl,
               "score": None, "threshold": None, "regime": jrow.get("regime") if jrow else None,
               "tf_biases": ({k: jrow.get(k) for k in ("tf_5m", "tf_15m", "tf_30m", "tf_1h")} if jrow else {}),
               "mae": analytics._f(jrow, "mae") if jrow else None, "mfe": analytics._f(jrow, "mfe") if jrow else None,
               "martingale_step": trade.martingale_step, "rejection": jrow.get("rejection_reason") if jrow else None,
               "factor_profile": analytics.factor_profile(jrow),
               "stats": analytics.aggregate_stats(get_journal().read_archive_merged())}
        if jrow:
            ctx["score"] = analytics._f(jrow, "score"); ctx["threshold"] = analytics._f(jrow, "threshold")
        sb = get_supabase()
        prior_k = sb.fetch_knowledge(8) if sb.enabled else []
        prior_r = sb.fetch_recent_research(5) if sb.enabled else []
        record, model = ai_engine.generate_research(ctx, prior_k, prior_r)
        record.update({"trade_id": tid, "signal_id": getattr(trade, "signal_id", ""), "symbol": trade.symbol,
                       "direction": trade.direction, "outcome": trade.status, "pnl": trade.pnl, "model": model})
        krows = analytics.knowledge_rows_from(record)
        if sb.enabled:
            sb.link_trade(tid, {"symbol": trade.symbol, "direction": trade.direction, "stake": trade.stake,
                                "opened_at": trade.timestamp, "closed_at": time.strftime("%Y-%m-%d %H:%M:%S"), "outcome": trade.status})
            ok_r = sb.upsert_research(record); ok_k = sb.upsert_knowledge(krows)
            if ok_r and ok_k:
                self._mark_done(tid)
            else:
                self._append(OUTBOX_FILE, {"research": record, "knowledge": krows}); logger.error("Research upload failed for %s; queued.", tid)
        else:
            self._append(LOCAL_FILE, record); self._mark_done(tid)
    def _flush_outbox(self):
        sb = get_supabase()
        if not sb.enabled or not os.path.exists(OUTBOX_FILE):
            return
        try:
            with open(OUTBOX_FILE, "r", encoding="utf-8") as fh:
                entries = [json.loads(l) for l in fh if l.strip()]
        except Exception:
            return
        remaining = []
        for e in entries:
            ok_r = sb.upsert_research(e.get("research", {})); ok_k = sb.upsert_knowledge(e.get("knowledge", []))
            if ok_r and ok_k:
                self._mark_done(e["research"].get("trade_id", ""))
            else:
                remaining.append(e)
        try:
            with open(OUTBOX_FILE, "w", encoding="utf-8") as fh:
                for e in remaining:
                    fh.write(json.dumps(e, ensure_ascii=False) + "\n")
        except Exception:
            pass
        if entries:
            logger.info("Outbox flush: %d uploaded, %d remaining.", len(entries) - len(remaining), len(remaining))
_singleton = None
def get_research_engine():
    global _singleton
    if _singleton is None:
        _singleton = ResearchEngine()
    return _singleton
