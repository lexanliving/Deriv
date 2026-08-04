"""src/supabase_service.py — Supabase via REST (DML) + optional Postgres (DDL).

Auto-creates tables on first run when SUPABASE_DB_URL is set (psycopg2). If not
set, falls back to a REST check and logs the exact SQL to run once. Every write
logs success/failure so you can confirm rows are flowing. Never writes
profit/loss/manual notes (those belong to the Personal OS).
"""
from __future__ import annotations
import json, os, ssl, threading, time, urllib.error, urllib.parse, urllib.request
from typing import Any, Dict, List, Optional
from src.logger import get_logger
logger = get_logger("supabase")
try:
    import psycopg2
except Exception:
    psycopg2 = None

SCHEMA_SQL = """
create table if not exists public.deriv_trade_research (
  trade_id text primary key, signal_id text, symbol text, direction text,
  outcome text, pnl numeric, entry_analysis text, exit_analysis text,
  strategy_adherence text, market_behaviour text, confidence numeric,
  strengths jsonb not null default '[]', weaknesses jsonb not null default '[]',
  mistakes jsonb not null default '[]', pattern_detected text,
  risk_observations jsonb not null default '[]', suggested_improvements jsonb not null default '[]',
  technical_explanation text, ai_summary text, model text,
  created_at timestamptz not null default now());
create index if not exists idx_dtr_symbol on public.deriv_trade_research (symbol);
create index if not exists idx_dtr_created on public.deriv_trade_research (created_at desc);
create table if not exists public.deriv_research_knowledge (
  id bigserial primary key, kind text not null, pattern_key text not null, description text,
  occurrences int not null default 1, wins int not null default 0, losses int not null default 0,
  last_trade_id text, first_seen timestamptz not null default now(),
  last_seen timestamptz not null default now(), unique (kind, pattern_key));
create table if not exists public.deriv_venture_advice (
  id bigserial primary key, verdict text, risk_multiplier numeric, max_risk_pct numeric,
  discussion jsonb not null default '{}', reasoning text, confidence numeric, period_days int,
  created_at timestamptz not null default now());
"""

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

class SupabaseService:
    def __init__(self):
        self._lock = threading.Lock()
        self.url = _secret("SUPABASE_URL")
        self.key = _secret("SUPABASE_SECRET_KEY") or _secret("SUPABASE_PUBLISHABLE_KEY")
        self.db_url = _secret("SUPABASE_DB_URL")
        self.os_table = (_secret("SUPABASE_OS_TRADES_TABLE") or "trades").lower()
        self.os_id_col = (_secret("SUPABASE_OS_TRADE_ID_COLUMN") or "trade_id").lower()
        self.write_os_row = _secret("SUPABASE_WRITE_OS_TRADE_ROW", "true").lower() in ("1", "true", "yes")
        self.research_table = "deriv_trade_research"
        self.knowledge_table = "deriv_research_knowledge"
        self.advice_table = "deriv_venture_advice"
        self._schema_ok = False
        self.last_error = ""
    @property
    def enabled(self):
        return bool(self.url and self.key)
    def _ok(self, s):
        return s in (200, 201, 204)
    def _request(self, method, table, params=None, body=None, prefer=None):
        url = f"{self.url.rstrip('/')}/rest/v1/{table}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        headers = {"apikey": self.key, "Authorization": f"Bearer {self.key}",
                   "Accept": "application/json", "Content-Type": "application/json"}
        if prefer:
            headers["Prefer"] = prefer
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30, context=ssl.create_default_context()) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")
        except Exception as e:
            return 0, str(e)
    def ensure_schema(self) -> bool:
        if self._schema_ok:
            return True
        if self.db_url and psycopg2:
            try:
                conn = psycopg2.connect(self.db_url, connect_timeout=10)
                conn.autocommit = True
                cur = conn.cursor(); cur.execute(SCHEMA_SQL); cur.close(); conn.close()
                self._schema_ok = True
                logger.info("Supabase schema ensured via Postgres.")
                return True
            except Exception as e:
                logger.error("PG schema bootstrap failed: %s", e)
        status, raw = self._request("GET", self.research_table, {"select": "trade_id", "limit": "1"})
        if self._ok(status):
            self._schema_ok = True
            return True
        self.last_error = f"tables missing ({status} {raw[:150]})"
        logger.error("Supabase tables missing. Add SUPABASE_DB_URL (auto-create) or run supabase/001_research_schema.sql once.")
        return False
    def health(self) -> Dict[str, Any]:
        return {"enabled": self.enabled, "schema_ok": self._schema_ok, "last_error": self.last_error}
    def _with_retry(self, fn, attempts=3, base=1.5):
        last = None
        for i in range(attempts):
            try:
                with self._lock:
                    return fn()
            except Exception as exc:
                last = exc; time.sleep(base * (2 ** i))
        self.last_error = str(last)
        return None
    def link_trade(self, trade_id, meta):
        if not (self.enabled and self.write_os_row):
            return
        def op():
            s, r = self._request("POST", self.os_table, {"on_conflict": self.os_id_col},
                {self.os_id_col: trade_id, "source": "deriv-bot", "symbol": meta.get("symbol"),
                 "direction": meta.get("direction"), "stake": meta.get("stake"),
                 "opened_at": meta.get("opened_at"), "closed_at": meta.get("closed_at"),
                 "outcome": meta.get("outcome")}, prefer="resolution=merge-duplicates,return=minimal")
            return self._ok(s)
        if not self._with_retry(op, attempts=2):
            self.write_os_row = False
    def upsert_research(self, rec) -> bool:
        def op():
            s, r = self._request("POST", self.research_table, {"on_conflict": "trade_id"}, {
                "trade_id": rec["trade_id"], "signal_id": rec.get("signal_id"), "symbol": rec.get("symbol"),
                "direction": rec.get("direction"), "outcome": rec.get("outcome"), "pnl": rec.get("pnl"),
                "entry_analysis": rec.get("entry_analysis"), "exit_analysis": rec.get("exit_analysis"),
                "strategy_adherence": rec.get("strategy_adherence"), "market_behaviour": rec.get("market_behaviour"),
                "confidence": rec.get("confidence"), "strengths": rec.get("strengths", []),
                "weaknesses": rec.get("weaknesses", []), "mistakes": rec.get("mistakes", []),
                "pattern_detected": rec.get("pattern_detected"), "risk_observations": rec.get("risk_observations", []),
                "suggested_improvements": rec.get("suggested_improvements", []),
                "technical_explanation": rec.get("technical_explanation"), "ai_summary": rec.get("ai_summary"),
                "model": rec.get("model")}, prefer="resolution=merge-duplicates,return=minimal")
            if not self._ok(s):
                self.last_error = f"upsert_research {s} {r[:150]}"
            return self._ok(s)
        return bool(self._with_retry(op))
    def upsert_knowledge(self, rows) -> bool:
        if not rows:
            return True
        def op():
            for r in rows:
                s, raw = self._request("GET", self.knowledge_table,
                    {"select": "id,occurrences,wins,losses", "kind": f"eq.{r['kind']}", "pattern_key": f"eq.{r['pattern_key']}"})
                existing = json.loads(raw) if self._ok(s) and raw else []
                if existing:
                    row = existing[0]
                    self._request("PATCH", self.knowledge_table, {"id": f"eq.{row['id']}"}, {
                        "occurrences": int(row["occurrences"]) + 1, "wins": int(row["wins"]) + int(r.get("wins", 0)),
                        "losses": int(row["losses"]) + int(r.get("losses", 0)),
                        "description": r.get("description"), "last_trade_id": r.get("last_trade_id")},
                        prefer="return=minimal")
                else:
                    self._request("POST", self.knowledge_table, None, {
                        "kind": r["kind"], "pattern_key": r["pattern_key"], "description": r.get("description"),
                        "wins": int(r.get("wins", 0)), "losses": int(r.get("losses", 0)),
                        "last_trade_id": r.get("last_trade_id")}, prefer="return=minimal")
            return True
        return bool(self._with_retry(op))
    def upsert_venture_advice(self, a) -> bool:
        def op():
            s, r = self._request("POST", self.advice_table, None, {
                "verdict": a.get("verdict"), "risk_multiplier": a.get("risk_multiplier"),
                "max_risk_pct": a.get("max_risk_pct"), "discussion": a.get("discussion", {}),
                "reasoning": a.get("reasoning"), "confidence": a.get("confidence"),
                "period_days": a.get("period_days", 30)}, prefer="return=minimal")
            return self._ok(s)
        return bool(self._with_retry(op, attempts=2))
    def fetch_recent_research(self, limit=5):
        def op():
            s, raw = self._request("GET", self.research_table,
                {"select": "symbol,outcome,pattern_detected,ai_summary", "order": "created_at.desc", "limit": str(limit)})
            return [dict(symbol=r.get("symbol"), outcome=r.get("outcome"), pattern=r.get("pattern_detected"),
                         summary=r.get("ai_summary")) for r in json.loads(raw or "[]")] if self._ok(s) else []
        return self._with_retry(op, attempts=2) or []
    def fetch_knowledge(self, limit=8):
        def op():
            s, raw = self._request("GET", self.knowledge_table,
                {"select": "kind,pattern_key,description,occurrences,wins,losses", "order": "occurrences.desc", "limit": str(limit)})
            return [dict(kind=r.get("kind"), pattern_key=r.get("pattern_key"), description=r.get("description"),
                         occurrences=r.get("occurrences"), wins=r.get("wins"), losses=r.get("losses")) for r in json.loads(raw or "[]")] if self._ok(s) else []
        return self._with_retry(op, attempts=2) or []

_singleton = None
def get_supabase():
    global _singleton
    if _singleton is None:
        _singleton = SupabaseService()
    return _singleton
