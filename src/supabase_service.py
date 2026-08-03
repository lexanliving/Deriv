"""src/supabase_service.py — reusable Supabase/Postgres service (research + advice)."""
from __future__ import annotations
import json, os, re, threading, time
from typing import Any, Dict, List, Optional
from src.logger import get_logger
logger = get_logger("supabase")
_IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")

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

def _ident(value, default):
    v = (value or "").lower().strip()
    return v if _IDENT.match(v) else default

class SupabaseService:
    def __init__(self):
        self._lock = threading.Lock(); self._conn = None
        self._schema_attempted = False; self._os_write_disabled = False
        self.url = _secret("SUPABASE_DB_URL")
        self.schema = _ident(_secret("SUPABASE_SCHEMA"), "public")
        self.os_table = _ident(_secret("SUPABASE_OS_TRADES_TABLE"), "trades")
        self.os_id_col = _ident(_secret("SUPABASE_OS_TRADE_ID_COLUMN"), "trade_id")
        self.write_os_row = _secret("SUPABASE_WRITE_OS_TRADE_ROW", "true").lower() in ("1", "true", "yes")
        self.research_table = f"{self.schema}.deriv_trade_research"
        self.knowledge_table = f"{self.schema}.deriv_research_knowledge"
        self.advice_table = f"{self.schema}.deriv_venture_advice"
    @property
    def enabled(self):
        return bool(self.url)
    def _connect(self):
        if self._conn is not None and not self._conn.closed:
            return self._conn
        import psycopg2
        self._conn = psycopg2.connect(self.url, connect_timeout=10, options=f"-c search_path={self.schema},public")
        return self._conn
    def _with_retry(self, fn, attempts=3, base=1.5):
        last = None
        for i in range(attempts):
            try:
                with self._lock:
                    conn = self._connect()
                    with conn.cursor() as cur:
                        out = fn(cur)
                    conn.commit(); return out
            except Exception as exc:
                last = exc; logger.warning("Supabase attempt %d/%d failed: %s", i + 1, attempts, exc)
                try:
                    if self._conn is not None:
                        self._conn.close()
                except Exception:
                    pass
                self._conn = None; time.sleep(base * (2 ** i))
        logger.error("Supabase op failed after %d attempts: %s", attempts, last); return None
    def ensure_schema(self):
        if self._schema_attempted:
            return True
        self._schema_attempted = True
        def ddl(cur):
            cur.execute(f"""
              create table if not exists {self.research_table}(trade_id text primary key,signal_id text,symbol text,direction text,outcome text,pnl numeric,entry_analysis text,exit_analysis text,strategy_adherence text,market_behaviour text,confidence numeric,strengths jsonb not null default '[]',weaknesses jsonb not null default '[]',mistakes jsonb not null default '[]',pattern_detected text,risk_observations jsonb not null default '[]',suggested_improvements jsonb not null default '[]',technical_explanation text,ai_summary text,model text,created_at timestamptz not null default now());
              create index if not exists idx_dtr_symbol on {self.research_table}(symbol);
              create table if not exists {self.knowledge_table}(id bigserial primary key,kind text not null,pattern_key text not null,description text,occurrences int not null default 1,wins int not null default 0,losses int not null default 0,last_trade_id text,first_seen timestamptz not null default now(),last_seen timestamptz not null default now(),unique(kind,pattern_key));
              create table if not exists {self.advice_table}(id bigserial primary key,verdict text,risk_multiplier numeric,max_risk_pct numeric,discussion jsonb not null default '{{}}',reasoning text,confidence numeric,period_days int,created_at timestamptz not null default now());""")
            cur.execute(f"""do $$ begin
              if exists(select 1 from information_schema.tables where table_schema=%s and table_name=%s)
              and exists(select 1 from information_schema.columns where table_schema=%s and table_name=%s and column_name=%s)
              and not exists(select 1 from information_schema.table_constraints where constraint_name='fk_dtr_os_trade') then
              execute format('alter table {self.research_table} add constraint fk_dtr_os_trade foreign key(trade_id) references %I.%I(%I) on delete cascade',%s,%s,%s); end if; end $$;""",
              (self.schema, self.os_table, self.schema, self.os_table, self.os_id_col, self.schema, self.os_table, self.os_id_col))
        self._with_retry(ddl, attempts=2); return True
    def link_trade(self, trade_id, meta):
        if not (self.enabled and self.write_os_row) or self._os_write_disabled:
            return
        def op(cur):
            cur.execute(f"insert into {self.schema}.{self.os_table}({self.os_id_col},source,symbol,direction,stake,opened_at,closed_at,outcome) values(%s,'deriv-bot',%s,%s,%s,%s,%s,%s) on conflict({self.os_id_col}) do nothing",
              (trade_id, meta.get("symbol"), meta.get("direction"), meta.get("stake"), meta.get("opened_at"), meta.get("closed_at"), meta.get("outcome")))
        if self._with_retry(op, attempts=2) is None:
            self._os_write_disabled = True; logger.warning("OS trade-row write disabled (schema differs?).")
    def upsert_research(self, rec):
        def op(cur):
            cur.execute(f"""insert into {self.research_table}(trade_id,signal_id,symbol,direction,outcome,pnl,entry_analysis,exit_analysis,strategy_adherence,market_behaviour,confidence,strengths,weaknesses,mistakes,pattern_detected,risk_observations,suggested_improvements,technical_explanation,ai_summary,model) values(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
              on conflict(trade_id) do update set entry_analysis=excluded.entry_analysis,exit_analysis=excluded.exit_analysis,strategy_adherence=excluded.strategy_adherence,market_behaviour=excluded.market_behaviour,confidence=excluded.confidence,strengths=excluded.strengths,weaknesses=excluded.weaknesses,mistakes=excluded.mistakes,pattern_detected=excluded.pattern_detected,risk_observations=excluded.risk_observations,suggested_improvements=excluded.suggested_improvements,technical_explanation=excluded.technical_explanation,ai_summary=excluded.ai_summary,model=excluded.model""",
              (rec["trade_id"], rec.get("signal_id"), rec.get("symbol"), rec.get("direction"), rec.get("outcome"), rec.get("pnl"), rec.get("entry_analysis"), rec.get("exit_analysis"), rec.get("strategy_adherence"), rec.get("market_behaviour"), rec.get("confidence"), json.dumps(rec.get("strengths", [])), json.dumps(rec.get("weaknesses", [])), json.dumps(rec.get("mistakes", [])), rec.get("pattern_detected"), json.dumps(rec.get("risk_observations", [])), json.dumps(rec.get("suggested_improvements", [])), rec.get("technical_explanation"), rec.get("ai_summary"), rec.get("model")))
        return self._with_retry(op) is not None
    def upsert_knowledge(self, rows):
        if not rows:
            return True
        def op(cur):
            for r in rows:
                cur.execute(f"insert into {self.knowledge_table}(kind,pattern_key,description,wins,losses,last_trade_id) values(%s,%s,%s,%s,%s,%s) on conflict(kind,pattern_key) do update set occurrences={self.knowledge_table}.occurrences+1,wins={self.knowledge_table}.wins+excluded.wins,losses={self.knowledge_table}.losses+excluded.losses,description=excluded.description,last_trade_id=excluded.last_trade_id,last_seen=now()",
                  (r["kind"], r["pattern_key"], r.get("description"), r.get("wins", 0), r.get("losses", 0), r.get("last_trade_id")))
        return self._with_retry(op) is not None
    def upsert_venture_advice(self, a):
        def op(cur):
            cur.execute(f"insert into {self.advice_table}(verdict,risk_multiplier,max_risk_pct,discussion,reasoning,confidence,period_days) values(%s,%s,%s,%s,%s,%s,%s)",
              (a.get("verdict"), a.get("risk_multiplier"), a.get("max_risk_pct"), json.dumps(a.get("discussion", {})), a.get("reasoning"), a.get("confidence"), a.get("period_days", 30)))
        return self._with_retry(op, attempts=2) is not None
    def fetch_recent_research(self, limit=5):
        if not self.enabled:
            return []
        def op(cur):
            cur.execute(f"select symbol,outcome,pattern_detected,ai_summary from {self.research_table} order by created_at desc limit %s", (limit,))
            return [dict(symbol=r[0], outcome=r[1], pattern=r[2], summary=r[3]) for r in cur.fetchall()]
        return self._with_retry(op, attempts=2) or []
    def fetch_knowledge(self, limit=8):
        if not self.enabled:
            return []
        def op(cur):
            cur.execute(f"select kind,pattern_key,description,occurrences,wins,losses from {self.knowledge_table} order by occurrences desc,last_seen desc limit %s", (limit,))
            return [dict(kind=r[0], pattern_key=r[1], description=r[2], occurrences=r[3], wins=r[4], losses=r[5]) for r in cur.fetchall()]
        return self._with_retry(op, attempts=2) or []

_singleton = None
def get_supabase():
    global _singleton
    if _singleton is None:
        _singleton = SupabaseService()
    return _singleton
