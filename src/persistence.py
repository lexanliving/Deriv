"""Small persistence layer for the digit terminal.

The live strategy only needs lossless journal export, merged-view export, and
idempotent restore. Candle gate sweeps, AI learning bundles, and post-mortem
analytics were removed from the lightweight build because they do not affect
execution of the digit strategy.
"""

from __future__ import annotations

import csv
import io
import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Tuple

from config import LOG_DIR
from src.journal import ARCHIVE_COLUMNS, COLUMNS


def _clean_record(row: Any) -> Dict[str, str]:
    row = row if isinstance(row, dict) else {}
    clean: Dict[str, str] = {}
    for key, value in row.items():
        if key is None:
            continue
        name = str(key).strip()
        text = "" if value is None else str(value).strip()
        clean[name] = text
    for column in COLUMNS:
        clean.setdefault(column, "")
    return clean


def export_archive_csv_bytes(journal: Any) -> bytes:
    """Return the append-only archive, falling back to the live journal."""
    try:
        path = getattr(journal, "_archive", "")
        if path and os.path.exists(path):
            with open(path, "rb") as handle:
                return handle.read()
    except Exception:
        pass
    try:
        return journal.to_csv_bytes() or b""
    except Exception:
        return b""


def export_merged_json_bytes(journal: Any) -> bytes:
    try:
        rows = journal.read_archive_merged() or []
    except Exception:
        rows = []
    payload = {
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "rows": [_clean_record(row) for row in rows],
    }
    return json.dumps(payload, indent=2, default=str).encode("utf-8")


def _fingerprint(row: Dict[str, Any]) -> Tuple[str, ...]:
    keys = ("timestamp_utc", "symbol", "direction", "strategy_mode", "barrier", "duration_unit", "entry_digit", "lower_confirmation_digit")
    return tuple(str(row.get(key, "") or "").strip().lower() for key in keys)


def _known_records(journal: Any) -> Tuple[set, set, set]:
    evaluation_ids: set = set()
    outcome_ids: set = set()
    evaluation_hashes: set = set()
    try:
        rows = journal.read_archive_merged() or []
    except Exception:
        rows = []
    for raw in rows:
        row = _clean_record(raw)
        sid = row.get("signal_id", "")
        if sid:
            evaluation_ids.add(sid)
            if row.get("outcome", ""):
                outcome_ids.add(sid)
        evaluation_hashes.add(_fingerprint(row))
    return evaluation_ids, outcome_ids, evaluation_hashes


def _load_records(data: Any, filename: str) -> List[Tuple[str, Dict[str, Any]]]:
    if isinstance(data, (bytes, bytearray)):
        text = data.decode("utf-8-sig")
    else:
        text = str(data)
    is_json = str(filename or "").lower().endswith(".json") or text.lstrip().startswith(("{", "["))
    records: List[Tuple[str, Dict[str, Any]]] = []
    if is_json:
        loaded = json.loads(text)
        if isinstance(loaded, dict):
            loaded = loaded.get("rows") or loaded.get("journal") or loaded.get("data") or [loaded]
        if not isinstance(loaded, list):
            raise ValueError("JSON backup must contain a list of rows.")
        for row in loaded:
            if isinstance(row, dict):
                records.append(("MERGED", row))
        return records

    reader = csv.DictReader(io.StringIO(text))
    fields = [str(field or "").strip() for field in (reader.fieldnames or [])]
    has_kind = "kind" in fields
    for raw in reader:
        if not isinstance(raw, dict):
            continue
        kind = str(raw.get("kind", "EVAL") or "EVAL").strip().upper() if has_kind else "EVAL"
        raw.pop("kind", None)
        records.append((kind, raw))
    return records


def import_journal(journal: Any, data: Any, filename: str = "import.csv") -> Dict[str, int]:
    """Restore evaluation/outcome rows without adding duplicates."""
    counts = {"eval_added": 0, "outcome_added": 0, "skipped": 0, "errors": 0}
    try:
        records = _load_records(data, filename)
        eval_ids, outcome_ids, eval_hashes = _known_records(journal)
        for kind, raw in records:
            row = _clean_record(raw)
            sid = row.get("signal_id", "")
            if kind == "OUTCOME":
                if not sid or sid in outcome_ids:
                    counts["skipped"] += 1
                    continue
                journal.record_outcome(
                    sid,
                    row.get("outcome", "UNKNOWN"),
                    float(row.get("pnl") or 0.0),
                    float(row.get("stake") or 0.0),
                    int(row["contract_id"]) if str(row.get("contract_id", "")).isdigit() else None,
                    row.get("execution_mode", ""),
                    row.get("martingale_step", ""),
                    row.get("note", ""),
                    row.get("mae", ""),
                    row.get("mfe", ""),
                )
                outcome_ids.add(sid)
                counts["outcome_added"] += 1
                continue

            if sid and sid in eval_ids:
                counts["skipped"] += 1
                continue
            fingerprint = _fingerprint(row)
            if not sid and fingerprint in eval_hashes:
                counts["skipped"] += 1
                continue
            journal.record_evaluation(row)
            if sid:
                eval_ids.add(sid)
            eval_hashes.add(fingerprint)
            counts["eval_added"] += 1

            outcome = row.get("outcome", "")
            if sid and outcome and sid not in outcome_ids:
                journal.record_outcome(
                    sid,
                    outcome,
                    float(row.get("pnl") or 0.0),
                    float(row.get("stake") or 0.0),
                    int(row["contract_id"]) if str(row.get("contract_id", "")).isdigit() else None,
                    row.get("execution_mode", ""),
                    row.get("martingale_step", ""),
                    row.get("note", ""),
                    row.get("mae", ""),
                    row.get("mfe", ""),
                )
                outcome_ids.add(sid)
                counts["outcome_added"] += 1
        return counts
    except Exception:
        counts["errors"] += 1
        return counts
