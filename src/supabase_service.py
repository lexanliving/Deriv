"""src/supabase_service.py — Supabase service via the official client.

Uses the official `supabase` client over HTTPS (no raw sockets), which is the
most reliable method from Streamlit Cloud. Tables are created once via the SQL
Editor (see supabase/001_research_schema.sql); this service only reads/writes.

We NEVER write profit/loss/manual notes (those belong to the Personal OS).
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from src.logger import get_logger

logger = get_logger("supabase")


def _secret(name: str, default: str = "") -> str:
    val = (os.getenv(name) or "").strip()
    if val:
        return val
    try:
        import streamlit as st
        v = st.secrets.get(name, "")
        return str(v).strip() if v else default
    except Exception:
        return default


def _utc_now() -> str:
    """A real UTC timestamp string that PostgREST accepts for timestamptz."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


class SupabaseService:
    def __init__(self):
        self._lock = threading.Lock()
        self._client = None
        self._client_failed = False
        self.url = _secret("SUPABASE_URL")
        self.key = _secret("SUPABASE_SECRET_KEY") or _secret("SUPABASE_PUBLISHABLE_KEY")
        self.os_table = (_secret("SUPABASE_OS_TRADES_TABLE") or "trades").lower()
        self.os_id_col = (_secret("SUPABASE_OS_TRADE_ID_COLUMN") or "trade_id").lower()
        self.write_os_row = _secret("SUPABASE_WRITE_OS_TRADE_ROW", "true").lower() in ("1", "true", "yes")
        self.research_table = "deriv_trade_research"
        self.knowledge_table = "deriv_research_knowledge"
        self.advice_table = "deriv_venture_advice"

    @property
    def enabled(self):
        return bool(self.url and self.key)

    def _client_instance(self):
        if self._client is not None:
            return self._client
        if self._client_failed or not self.enabled:
            return None
        try:
            from supabase import create_client
            self._client = create_client(self.url, self.key)
            return self._client
        except Exception as exc:
            self._client_failed = True
            logger.error("Supabase client init failed: %s", exc)
            return None

    def _with_retry(self, fn, attempts=3, base=1.5):
        last = None
        for i in range(attempts):
            try:
                with self._lock:
                    return fn()
            except Exception as exc:
                last = exc
                logger.warning("Supabase attempt %d/%d failed: %s", i + 1, attempts, exc)
                time.sleep(base * (2 ** i))
        logger.error("Supabase op failed after %d attempts: %s", attempts, last)
        return None

    def ensure_schema(self):
        def op():
            c = self._client_instance()
            if c is None:
                return False
            c.table(self.research_table).select("trade_id").limit(1).execute()
            return True
        ok = self._with_retry(op, attempts=2)
        if not ok:
            logger.error("Tables missing or unreachable — run supabase/001_research_schema.sql once in the SQL Editor.")
        return bool(ok)

    def link_trade(self, trade_id, meta):
        if not (self.enabled and self.write_os_row):
            return

        def op():
            c = self._client_instance()
            if c is None:
                return None
            c.table(self.os_table).upsert(
                {self.os_id_col: trade_id, "source": "deriv-bot", "symbol": meta.get("symbol"),
                 "direction": meta.get("direction"), "stake": meta.get("stake"),
                 "opened_at": meta.get("opened_at"), "closed_at": meta.get("closed_at"),
                 "outcome": meta.get("outcome")},
                on_conflict=self.os_id_col).execute()
            return True
        if self._with_retry(op, attempts=2) is None:
            logger.warning("OS trade-row write disabled (schema differs?).")
            self.write_os_row = False

    def upsert_research(self, rec):
        def op():
            c = self._client_instance()
            if c is None:
                return None
            c.table(self.research_table).upsert({
                "trade_id": rec["trade_id"], "signal_id": rec.get("signal_id"), "symbol": rec.get("symbol"),
                "direction": rec.get("direction"), "outcome": rec.get("outcome"), "pnl": rec.get("pnl"),
                "entry_analysis": rec.get("entry_analysis"), "exit_analysis": rec.get("exit_analysis"),
                "strategy_adherence": rec.get("strategy_adherence"), "market_behaviour": rec.get("market_behaviour"),
                "confidence": rec.get("confidence"), "strengths": rec.get("strengths", []),
                "weaknesses": rec.get("weaknesses", []), "mistakes": rec.get("mistakes", []),
                "pattern_detected": rec.get("pattern_detected"), "risk_observations": rec.get("risk_observations", []),
                "suggested_improvements": rec.get("suggested_improvements", []),
                "technical_explanation": rec.get("technical_explanation"), "ai_summary": rec.get("ai_summary"),
                "model": rec.get("model")}, on_conflict="trade_id").execute()
            return True
        return self._with_retry(op) is not None

    def upsert_knowledge(self, rows):
        if not rows:
            return True

        def op():
            c = self._client_instance()
            if c is None:
                return None
            for r in rows:
                existing = c.table(self.knowledge_table).select("id,occurrences,wins,losses").eq(
                    "kind", r["kind"]).eq("pattern_key", r["pattern_key"]).execute()
                if existing.data:
                    row = existing.data[0]
                    c.table(self.knowledge_table).update({
                        "occurrences": int(row["occurrences"]) + 1,
                        "wins": int(row["wins"]) + int(r.get("wins", 0)),
                        "losses": int(row["losses"]) + int(r.get("losses", 0)),
                        "description": r.get("description"), "last_trade_id": r.get("last_trade_id"),
                        "last_seen": _utc_now()}).eq("id", row["id"]).execute()
                else:
                    c.table(self.knowledge_table).insert({
                        "kind": r["kind"], "pattern_key": r["pattern_key"], "description": r.get("description"),
                        "wins": int(r.get("wins", 0)), "losses": int(r.get("losses", 0)),
                        "last_trade_id": r.get("last_trade_id")}).execute()
            return True
        return self._with_retry(op) is not None

    def upsert_venture_advice(self, a):
        def op():
            c = self._client_instance()
            if c is None:
                return None
            c.table(self.advice_table).insert({
                "verdict": a.get("verdict"), "risk_multiplier": a.get("risk_multiplier"),
                "max_risk_pct": a.get("max_risk_pct"), "discussion": a.get("discussion", {}),
                "reasoning": a.get("reasoning"), "confidence": a.get("confidence"),
                "period_days": a.get("period_days", 30)}).execute()
            return True
        return self._with_retry(op, attempts=2) is not None

    def fetch_recent_research(self, limit=5):
        def op():
            c = self._client_instance()
            if c is None:
                return []
            res = c.table(self.research_table).select("symbol,outcome,pattern_detected,ai_summary").order(
                "created_at", desc=True).limit(limit).execute()
            return [dict(symbol=r.get("symbol"), outcome=r.get("outcome"), pattern=r.get("pattern_detected"),
                         summary=r.get("ai_summary")) for r in res.data]
        return self._with_retry(op, attempts=2) or []

    def fetch_knowledge(self, limit=8):
        def op():
            c = self._client_instance()
            if c is None:
                return []
            res = c.table(self.knowledge_table).select("kind,pattern_key,description,occurrences,wins,losses").order(
                "occurrences", desc=True).limit(limit).execute()
            return [dict(kind=r.get("kind"), pattern_key=r.get("pattern_key"), description=r.get("description"),
                         occurrences=r.get("occurrences"), wins=r.get("wins"), losses=r.get("losses")) for r in res.data]
        return self._with_retry(op, attempts=2) or []


_singleton = None


def get_supabase():
    global _singleton
    if _singleton is None:
        _singleton = SupabaseService()
    return _singleton
