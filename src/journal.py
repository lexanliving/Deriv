"""
src/journal.py — decision + execution log with a crash-proof archive.

  logs/trade_journal.csv   — live working file: one row per 15m review, with the
                             trade outcome back-filled in place. Shown in the
                             Terminal preview and offered as the CSV download.
  logs/journal_archive.csv — append-only mirror: every review (kind=EVAL) and
                             every outcome (kind=OUTCOME, with the reason in
                             `note`) is appended and never rewritten or cleared.
                             The Performance Scope reads THIS file, so a cleared
                             live log never erases a past day from the scope.

Schema migration: an existing file whose header lacks newer columns (e.g. note)
is migrated in place by column name so rows never misalign. Every operation is
defensive — a journal failure can never interrupt trading.
"""
import csv
import os
import threading
from typing import Any, Dict, List, Optional

from config import LOG_DIR
from src.logger import get_logger

logger = get_logger("journal")

JOURNAL_FILE = os.path.join(LOG_DIR, "trade_journal.csv")
ARCHIVE_FILE = os.path.join(LOG_DIR, "journal_archive.csv")

COLUMNS = [
    "signal_id", "timestamp_utc", "symbol",
    "direction", "trend", "taken", "executed", "rejection_reason", "note",
    "score", "threshold",
    "s_trend", "s_trigger", "s_momentum", "s_volatility", "s_alignment",
    "s_adx", "s_macd", "s_rsi_zone", "s_pattern", "s_structure",
    "entry_adx", "entry_rsi", "entry_macd_hist", "atr", "close",
    "outcome", "pnl", "stake", "martingale_step", "contract_id", "execution_mode",
]
ARCHIVE_COLUMNS = ["kind"] + COLUMNS


class TradeJournal:
    def __init__(self, live_path: str = JOURNAL_FILE, archive_path: str = ARCHIVE_FILE):
        self._live = live_path
        self._archive = archive_path
        self._lock = threading.Lock()
        self._ensure_csv(self._live, COLUMNS)
        self._ensure_csv(self._archive, ARCHIVE_COLUMNS)

    def _ensure_csv(self, path: str, header: List[str]) -> None:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            if not os.path.exists(path):
                with open(path, "w", newline="", encoding="utf-8") as f:
                    csv.writer(f).writerow(header)
                return
            with open(path, "r", newline="", encoding="utf-8") as f:
                first = next(csv.reader(f), None)
            if first == header:
                return
            with open(path, "r", newline="", encoding="utf-8") as f:
                old_rows = list(csv.DictReader(f))
            with open(path, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(header)
                for r in old_rows:
                    w.writerow([r.get(c, "") for c in header])
            logger.info("Migrated journal header for %s.", os.path.basename(path))
        except Exception as exc:
            logger.warning("Journal header init/migrate failed for %s: %s", path, exc)

    def record_evaluation(self, record: Dict[str, Any]) -> None:
        live_row = [record.get(c, "") for c in COLUMNS]
        try:
            with self._lock:
                self._ensure_csv(self._live, COLUMNS)
                with open(self._live, "a", newline="", encoding="utf-8") as f:
                    csv.writer(f).writerow(live_row)
        except Exception as exc:
            logger.warning("Journal evaluation write failed: %s", exc)
        try:
            with self._lock:
                self._ensure_csv(self._archive, ARCHIVE_COLUMNS)
                with open(self._archive, "a", newline="", encoding="utf-8") as f:
                    csv.writer(f).writerow(["EVAL"] + live_row)
        except Exception as exc:
            logger.warning("Archive evaluation write failed: %s", exc)

    def record_outcome(self, signal_id, outcome, pnl, stake, contract_id,
                       execution_mode, martingale_step="", note="") -> None:
        if not signal_id:
            return
        try:
            with self._lock:
                self._ensure_csv(self._live, COLUMNS)
                if os.path.exists(self._live):
                    with open(self._live, "r", newline="", encoding="utf-8") as f:
                        rows = list(csv.reader(f))
                    if rows:
                        idx = {name: i for i, name in enumerate(rows[0])}
                        sid_i = idx.get("signal_id")
                        if sid_i is not None:
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
                                    put("note", note)
                                    changed = True
                            if changed:
                                with open(self._live, "w", newline="", encoding="utf-8") as f:
                                    csv.writer(f).writerows(rows)
        except Exception as exc:
            logger.warning("Journal outcome write failed: %s", exc)
        odict = {
            "signal_id": signal_id, "outcome": outcome, "pnl": f"{pnl:.2f}",
            "stake": f"{stake:.2f}", "martingale_step": str(martingale_step),
            "contract_id": str(contract_id) if contract_id else "",
            "execution_mode": execution_mode,
            "executed": "TRUE" if contract_id else "FALSE", "note": note,
        }
        try:
            with self._lock:
                self._ensure_csv(self._archive, ARCHIVE_COLUMNS)
                with open(self._archive, "a", newline="", encoding="utf-8") as f:
                    csv.writer(f).writerow(["OUTCOME"] + [odict.get(c, "") for c in COLUMNS])
        except Exception as exc:
            logger.warning("Archive outcome write failed: %s", exc)

    def read_rows(self) -> List[Dict[str, str]]:
        try:
            with self._lock:
                if not os.path.exists(self._live):
                    return []
                with open(self._live, "r", newline="", encoding="utf-8") as f:
                    return list(csv.DictReader(f))
        except Exception as exc:
            logger.warning("Journal read failed: %s", exc)
            return []

    def to_csv_bytes(self) -> bytes:
        try:
            with self._lock:
                if not os.path.exists(self._live):
                    return b""
                with open(self._live, "rb") as f:
                    return f.read()
        except Exception:
            return b""

    def read_archive_merged(self) -> List[Dict[str, str]]:
        try:
            with self._lock:
                if os.path.exists(self._archive):
                    with open(self._archive, "r", newline="", encoding="utf-8") as f:
                        arows = list(csv.DictReader(f))
                    if arows:
                        evals: Dict[str, Dict[str, str]] = {}
                        order: List[str] = []
                        for r in arows:
                            kind = r.get("kind", "")
                            d = {c: r.get(c, "") for c in COLUMNS}
                            sid = d.get("signal_id", "")
                            if kind == "EVAL":
                                if sid and sid not in evals:
                                    evals[sid] = d
                                    order.append(sid)
                                elif not sid:
                                    key = f"__noid_{len(order)}"
                                    evals[key] = d
                                    order.append(key)
                            elif kind == "OUTCOME":
                                target = evals.get(sid) if sid else None
                                if target is None:
                                    target = d
                                    if sid:
                                        evals[sid] = target
                                        order.append(sid)
                                    else:
                                        key = f"__noid_{len(order)}"
                                        evals[key] = target
                                        order.append(key)
                                for fld in ("outcome", "pnl", "stake", "martingale_step",
                                            "contract_id", "execution_mode", "executed", "note"):
                                    v = r.get(fld, "")
                                    if v not in (None, ""):
                                        target[fld] = v
                        return [evals[k] for k in order]
                if os.path.exists(self._live):
                    with open(self._live, "r", newline="", encoding="utf-8") as f:
                        return list(csv.DictReader(f))
                return []
        except Exception as exc:
            logger.warning("Archive read/merge failed: %s", exc)
            return []


_journal_singleton: Optional[TradeJournal] = None


def get_journal() -> TradeJournal:
    global _journal_singleton
    if _journal_singleton is None:
        _journal_singleton = TradeJournal()
    return _journal_singleton