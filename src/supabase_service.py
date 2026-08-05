"""src/supabase_service.py — Supabase trades-table writer (official client over HTTPS).

The connection is exactly as before: the official `supabase` client with the
service-role key — the most reliable method from Streamlit Cloud. All AI /
research uploads were removed together with the AI layer. This service now has
ONE job: mirror settled trades into the Personal OS `trades` table.

Robustness:
  * insert-or-update by trade_id — does NOT rely on an ON CONFLICT / unique
    constraint existing on the table (the most common silent failure),
  * falls back to smaller payloads if the table has fewer columns,
  * retries with exponential backoff,
  * a transient failure never disables mirroring permanently (5 consecutive
    failures do, with a loud log),
  * never raises into the trading engine.

We never write personal notes or anything beyond the trade row itself.
"""

from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from src.logger import get_logger

logger = get_logger("supabase")


def _secret(name: str, default: str = "") -> str:
    value = (os.getenv(name) or "").strip()
    if value:
        return value
    try:
        import streamlit as st
        value = st.secrets.get(name, "")
        return str(value).strip() if value else default
    except Exception:
        return default


def _iso(value: Any) -> Optional[str]:
    """Normalise timestamps to ISO-8601 UTC so both text and timestamptz columns accept them."""
    if value in (None, ""):
        return None
    text = str(value).strip()
    cleaned = text.replace("UTC", "").replace("Z", "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(cleaned, fmt)
            return parsed.replace(tzinfo=timezone.utc).isoformat()
        except ValueError:
            continue
    return text


class SupabaseService:
    MAX_CONSECUTIVE_FAILURES = 5

    def __init__(self):
        self._lock = threading.Lock()
        self._client = None
        self._client_failed = False
        self._consecutive_failures = 0
        self.url = _secret("SUPABASE_URL")
        self.key = _secret("SUPABASE_SECRET_KEY") or _secret("SUPABASE_PUBLISHABLE_KEY")
        self.os_table = (_secret("SUPABASE_OS_TRADES_TABLE") or "trades").lower()
        self.os_id_col = (_secret("SUPABASE_OS_TRADE_ID_COLUMN") or "trade_id").lower()
        self.write_os_row = _secret("SUPABASE_WRITE_OS_TRADE_ROW", "true").lower() in ("1", "true", "yes")

    # ------------------------------------------------------------- connection
    @property
    def enabled(self) -> bool:
        return bool(self.url and self.key)

    def client(self):
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

    # ------------------------------------------------------------------ schema
    def ensure_schema(self) -> bool:
        """REST cannot create tables; verify the trades table exists and log a clear hint if not."""
        def op():
            c = self.client()
            if c is None:
                return False
            c.table(self.os_table).select(self.os_id_col).limit(1).execute()
            return True

        ok = self._with_retry(op, attempts=2)
        if not ok:
            logger.error("Supabase '%s' table is missing or unreachable — trades will not be mirrored. "
                         "Check that the table exists (and SUPABASE_OS_TRADES_TABLE if it is named differently).",
                         self.os_table)
        return bool(ok)

    # ------------------------------------------------------------- trade rows
    def link_trade(self, trade_id, meta) -> bool:
        """Insert-or-update one settled trade row. Never raises."""
        if not (self.enabled and self.write_os_row):
            return False
        trade_id = str(trade_id)
        full_payload = {
            self.os_id_col: trade_id,
            "source": "deriv-bot",
            "symbol": meta.get("symbol"),
            "direction": meta.get("direction"),
            "stake": meta.get("stake"),
            "opened_at": _iso(meta.get("opened_at")),
            "closed_at": _iso(meta.get("closed_at")),
            "outcome": meta.get("outcome"),
            "pnl": meta.get("pnl"),
            "contract_id": meta.get("contract_id"),
        }
        base_payload = {k: v for k, v in full_payload.items() if k not in ("pnl", "contract_id")}
        minimal_payload = {k: v for k, v in base_payload.items() if v is not None or k == self.os_id_col}
        candidates = [full_payload, base_payload, minimal_payload]

        def op():
            c = self.client()
            if c is None:
                return None
            table = c.table(self.os_table)
            exists = bool(table.select(self.os_id_col).eq(self.os_id_col, trade_id).execute().data)
            last_exc: Optional[Exception] = None
            for payload in candidates:
                try:
                    if exists:
                        updates = {k: v for k, v in payload.items() if k != self.os_id_col}
                        table.update(updates).eq(self.os_id_col, trade_id).execute()
                    else:
                        table.insert(payload).execute()
                    return True
                except Exception as exc:
                    last_exc = exc
                    logger.warning("Supabase trade write variant failed (%s); trying smaller payload.", exc)
            raise last_exc if last_exc else RuntimeError("Supabase trade write failed on all payload variants")

        try:
            ok = self._with_retry(op) is not None
        except Exception as exc:  # defensive; _with_retry already swallows
            ok = False
            logger.error("Supabase link_trade error: %s", exc)

        if ok:
            self._consecutive_failures = 0
            logger.info("Trade %s mirrored to Supabase '%s'.", trade_id, self.os_table)
        else:
            self._consecutive_failures += 1
            logger.warning("Supabase trade write failed for %s (%d consecutive).", trade_id, self._consecutive_failures)
            if self._consecutive_failures >= self.MAX_CONSECUTIVE_FAILURES:
                self.write_os_row = False
                logger.error("Supabase trade mirroring disabled after %d consecutive failures. "
                             "Restart the bot to re-enable once Supabase is fixed.", self._consecutive_failures)
        return ok


_singleton: Optional[SupabaseService] = None


def get_supabase() -> SupabaseService:
    global _singleton
    if _singleton is None:
        _singleton = SupabaseService()
    return _singleton
