"""src/supabase_service.py — Supabase via plain HTTPS (PostgREST), stdlib only.

No external client / no psycopg2, so it works on Streamlit Cloud regardless of
package installs. Uses SUPABASE_URL + SUPABASE_SECRET_KEY (service role).
We NEVER write profit/loss/manual notes (those belong to the Personal OS).
Tables must exist (run supabase/001_research_schema.sql once in the SQL Editor).
"""
from __future__ import annotations
import json, os, ssl, threading, time, urllib.error, urllib.parse, urllib.request
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

class SupabaseService:
    def __init__(self):
        self._lock = threading.Lock()
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
    def _ok(self, status):
        return status in (200, 201, 204)
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
            status, raw = self._request("GET", self.research_table, {"select": "trade_id", "limit": "1"})
            if self._ok(status):
                return True
            logger.error("Tables missing/unreachable (%s %s) — run supabase/001_research_schema.sql once in the SQL Editor.", status, raw[:200])
            return False
        return bool(self._with_retry(op, attempts=2))
    def link_trade(self, trade_id, meta):
        if not (self.enabled and self.write_os_row):
            return
        def op():
            status, raw = self._request("POST", self.os_table, {"on_conflict": self.os_id_col},
                {self.os_id_col: trade_id, "source": "deriv-bot", "symbol": meta.get("symbol"),
                 "direction": meta.get("direction"), "stake": meta.get("stake"),
                 "opened_at": meta.get("opened_at"), "closed_at": meta.get("closed_at"),
                 "outcome": meta.get("outcome")}, prefer="resolution=merge-duplicates,return=minimal")
            return self._ok(status)
        if not self._with_retry(op, attempts=2):
            logger.warning("OS trade-row write disabled (schema differs?).")
            self.write_os_row = False
    def upsert_research(self, rec):
        def op():
            status, raw = self._request("POST", self.research_table, {"on_conflict": "trade_id"}, {
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
            return self._ok(status)
        return bool(self._with_retry(op))
    def upsert_knowledge(self, rows):
        if not rows:
            return True
        def op():
            for r in rows:
                status, raw = self._request("GET", self.knowledge_table,
                    {"select": "id,occurrences,wins,losses", "kind": f"eq.{r['kind']}", "pattern_key": f"eq.{r['pattern_key']}"})
                existing = json.loads(raw) if self._ok(status) and raw else []
                if existing:
                    row = existing[0]
                    self._request("PATCH", self.knowledge_table, {"id": f"eq.{row['id']}"}, {
                        "occurrences": int(row["occurrences"]) + 1, "wins": int(row["wins"]) + int(r.get("wins", 0)),
                        "losses": int(row["losses"]) + int(r.get("losses", 0)),
                        "description": r.get("description"), "last_trade_id": r.get("last_trade_id"),
                        "last_seen": "now()"}, prefer="return=minimal")
                else:
                    self._request("POST", self.knowledge_table, None, {
                        "kind": r["kind"], "pattern_key": r["pattern_key"], "description": r.get("description"),
                        "wins": int(r.get("wins", 0)), "losses": int(r.get("losses", 0)),
                        "last_trade_id": r.get("last_trade_id")}, prefer="return=minimal")
            return True
        return bool(self._with_retry(op))
    def upsert_venture_advice(self, a):
        def op():
            status, raw = self._request("POST", self.advice_table, None, {
                "verdict": a.get("verdict"), "risk_multiplier": a.get("risk_multiplier"),
                "max_risk_pct": a.get("max_risk_pct"), "discussion": a.get("discussion", {}),
                "reasoning": a.get("reasoning"), "confidence": a.get("confidence"),
                "period_days": a.get("period_days", 30)}, prefer="return=minimal")
            return self._ok(status)
        return bool(self._with_retry(op, attempts=2))
    def fetch_recent_research(self, limit=5):
        def op():
            status, raw = self._request("GET", self.research_table,
                {"select": "symbol,outcome,pattern_detected,ai_summary", "order": "created_at.desc", "limit": str(limit)})
            if not self._ok(status):
                return []
            return [dict(symbol=r.get("symbol"), outcome=r.get("outcome"), pattern=r.get("pattern_detected"),
                         summary=r.get("ai_summary")) for r in json.loads(raw or "[]")]
        return self._with_retry(op, attempts=2) or []
    def fetch_knowledge(self, limit=8):
        def op():
            status, raw = self._request("GET", self.knowledge_table,
                {"select": "kind,pattern_key,description,occurrences,wins,losses", "order": "occurrences.desc", "limit": str(limit)})
            if not self._ok(status):
                return []
            return [dict(kind=r.get("kind"), pattern_key=r.get("pattern_key"), description=r.get("description"),
                         occurrences=r.get("occurrences"), wins=r.get("wins"), losses=r.get("losses")) for r in json.loads(raw or "[]")]
        return self._with_retry(op, attempts=2) or []

_singleton = None
def get_supabase():
    global _singleton
    if _singleton is None:
        _singleton = SupabaseService()
    return _singleton
