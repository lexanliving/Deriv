"""
src/journal.py
Append-only CSV decision journal for strategy improvement.
Records every 15m signal evaluation — taken or rejected — with the full
confluence score breakdown and the rejection reason, then back-fills the
outcome when the resulting trade settles. Opens directly in Excel / Google
Sheets. All operations are defensive: a journal failure can never interrupt
trading.
"""
import csv
import os
import threading
from typing import Any, Dict, List, Optional

from config import LOG_DIR
from src.logger import get_logger

logger = get_logger("journal")

JOURNAL_FILE = os.path.join(LOG_DIR, "trade_journal.csv")

COLUMNS = [
    "signal_id", "timestamp_utc", "symbol",
    "direction", "trend", "taken", "executed", "rejection_reason",
    "score", "threshold",
    "s_trend", "s_trigger", "s_momentum", "s_volatility", "s_alignment",
    "s_adx", "s_macd", "s_rsi_zone", "s_pattern", "s_structure",
    "entry_adx", "entry_rsi", "entry_macd_hist", "atr", "close",
    "outcome", "pnl", "stake", "martingale_step", "contract_id", "execution_mode",
]


class TradeJournal:
    def __init__(self, path: str = JOURNAL_FILE):
        self._path = path
        self._lock = threading.Lock()
        self._ensure_header()

    def _ensure_header(self) -> None:
        try:
            os.makedirs(os.path.dirname(self._path), exist_ok=True)
            if not os.path.exists(self._path):
                with open(self._path, "w", newline="", encoding="utf-8") as f:
                    csv.writer(f).writerow(COLUMNS)
        except Exception as exc:
            logger.warning("Journal header init failed (journaling disabled): %s", exc)

    def record_evaluation(self, record: Dict[str, Any]) -> None:
        try:
            with self._lock:
                self._ensure_header()
                row = [record.get(col, "") for col in COLUMNS]
                with open(self._path, "a", newline="", encoding="utf-8") as f:
                    csv.writer(f).writerow(row)
        except Exception as exc:
            logger.warning("Journal evaluation write failed: %s", exc)

    def record_outcome(self, signal_id, outcome, pnl, stake, contract_id,
                       execution_mode, martingale_step="") -> None:
        if not signal_id:
            return
        try:
            with self._lock:
                if not os.path.exists(self._path):
                    return
                with open(self._path, "r", newline="", encoding="utf-8") as f:
                    rows = list(csv.reader(f))
                if not rows:
                    return
                idx = {name: i for i, name in enumerate(rows[0])}
                sid_i = idx.get("signal_id")
                if sid_i is None:
                    return
                changed = False
                for row in rows[1:]:
                    if len(row) > sid_i and row[sid_i] == signal_id:
                        def put(col, val):
                            if col in idx and idx[col] < len(row):
                                row[idx[col]] = val
                        put("outcome", outcome)
                        put("pnl", f"{pnl:.2f}")
                        put("stake", f"{stake:.2f}")
                        put("martingale_step", str(martingale_step))
                        put("contract_id", str(contract_id) if contract_id else "")
                        put("execution_mode", execution_mode)
                        put("executed", "TRUE" if contract_id else "FALSE")
                        changed = True
                if changed:
                    with open(self._path, "w", newline="", encoding="utf-8") as f:
                        csv.writer(f).writerows(rows)
        except Exception as exc:
            logger.warning("Journal outcome write failed: %s", exc)

    def read_rows(self) -> List[Dict[str, str]]:
        try:
            with self._lock:
                if not os.path.exists(self._path):
                    return []
                with open(self._path, "r", newline="", encoding="utf-8") as f:
                    return list(csv.DictReader(f))
        except Exception as exc:
            logger.warning("Journal read failed: %s", exc)
            return []

    def to_csv_bytes(self) -> bytes:
        try:
            with self._lock:
                if not os.path.exists(self._path):
                    return b""
                with open(self._path, "rb") as f:
                    return f.read()
        except Exception:
            return b""


_journal_singleton: Optional[TradeJournal] = None


def get_journal() -> TradeJournal:
    global _journal_singleton
    if _journal_singleton is None:
        _journal_singleton = TradeJournal()
    return _journal_singleton