"""src/persistence.py — Offline learning loop for MomentumMaster TF.

This module is additive and isolated. It reads the journal and optional
snapshot file only. It never imports the trading engine and never mutates
strategy behaviour.

Provides:
  (A) Backup / restore helpers
  (B) Post-mortem analytics
  (C) Offline gate backtest
  (D) Learning bundle export
"""

import csv
import io
import json
import os
import zipfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

try:
    from config import LOG_DIR, SCORE_MAX
except Exception:
    LOG_DIR = os.path.join(os.getcwd(), "logs")
    SCORE_MAX = 25

try:
    from src.journal import COLUMNS as JOURNAL_COLUMNS, ARCHIVE_COLUMNS as JOURNAL_ARCHIVE_COLUMNS
except Exception:
    JOURNAL_COLUMNS = [
        "signal_id",
        "timestamp_utc",
        "symbol",
        "direction",
        "trend",
        "taken",
        "executed",
        "rejection_reason",
        "note",
        "score",
        "threshold",
        "s_trend",
        "s_trigger",
        "s_momentum",
        "s_volatility",
        "s_alignment",
        "s_adx",
        "s_macd",
        "s_rsi_zone",
        "s_pattern",
        "s_structure",
        "entry_adx",
        "entry_rsi",
        "entry_macd_hist",
        "atr",
        "close",
        "outcome",
        "pnl",
        "stake",
        "martingale_step",
        "contract_id",
        "execution_mode",
        "regime",
        "duration_min",
        "mae",
        "mfe",
        "tf_5m",
        "tf_15m",
        "tf_30m",
        "tf_1h",
        "mtf_agreement",
    ]
    JOURNAL_ARCHIVE_COLUMNS = ["kind"] + JOURNAL_COLUMNS


SNAPSHOT_FILE = os.path.join(LOG_DIR, "trade_snapshots.jsonl")

FACTOR_KEYS = [
    "s_trend",
    "s_trigger",
    "s_momentum",
    "s_volatility",
    "s_alignment",
    "s_adx",
    "s_macd",
    "s_rsi_zone",
    "s_pattern",
    "s_structure",
]

BASE_FACTOR_WEIGHTS = {
    "trend": 5,
    "trigger": 3,
    "momentum": 3,
    "volatility": 2,
    "alignment": 1,
    "adx": 3,
    "macd": 2,
    "rsi_zone": 2,
    "pattern": 2,
    "structure": 2,
}

FACTOR_TO_BASE = {f"s_{name}": name for name in BASE_FACTOR_WEIGHTS}

SWEEP_THRESHOLDS = [13, 16, 20, 23]


def _make_variant(overrides: Optional[Dict[str, float]] = None) -> Dict[str, float]:
    variant = {name: 1.0 for name in BASE_FACTOR_WEIGHTS}
    if overrides:
        variant.update(overrides)
    return variant


WEIGHT_VARIANTS: Dict[str, Dict[str, float]] = {
    "current": _make_variant(),
    "trend_heavy": _make_variant(
        {
            "trend": 1.25,
            "alignment": 1.20,
            "adx": 1.15,
            "structure": 1.10,
            "trigger": 0.95,
            "momentum": 0.95,
            "macd": 0.95,
            "rsi_zone": 0.90,
            "volatility": 0.85,
            "pattern": 0.85,
        }
    ),
    "execution_heavy": _make_variant(
        {
            "trigger": 1.25,
            "momentum": 1.20,
            "macd": 1.15,
            "pattern": 1.10,
            "adx": 1.05,
            "structure": 1.00,
            "alignment": 1.00,
            "trend": 0.90,
            "rsi_zone": 0.95,
            "volatility": 0.90,
        }
    ),
    "pullback_patient": _make_variant(
        {
            "rsi_zone": 1.25,
            "structure": 1.20,
            "volatility": 1.15,
            "alignment": 1.10,
            "pattern": 1.05,
            "trend": 1.00,
            "adx": 1.00,
            "macd": 0.95,
            "trigger": 0.90,
            "momentum": 0.90,
        }
    ),
}


# ---------------------------------------------------------------------------
# Basic helpers
# ---------------------------------------------------------------------------

def _to_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    if value is None:
        return default
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return default
    try:
        return float(text)
    except (TypeError, ValueError):
        return default


def _to_bool(value: Any) -> bool:
    return str(value or "").strip().upper() in {"TRUE", "T", "1", "YES", "Y"}


def _parse_timestamp(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)

    text = str(value).strip()
    if not text:
        return None

    formats = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S UTC",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%SZ",
    ]
    for fmt in formats:
        try:
            parsed = datetime.strptime(text, fmt)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except Exception:
            pass

    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _clean_record(row: Optional[Dict[Any, Any]]) -> Dict[str, str]:
    """Normalise a journal row.

    The uploaded files can contain harmless formatting damage, trailing spaces,
    or mixed key forms. This keeps imports robust without touching live code.
    """
    out: Dict[str, str] = {}
    row = row or {}

    for key, value in row.items():
        if key is None:
            continue
        raw_key = str(key)
        clean_key = raw_key.strip()

        if isinstance(value, str):
            clean_value = value.strip()
        elif value is None:
            clean_value = ""
        else:
            clean_value = str(value).strip()

        out[raw_key] = clean_value
        out[clean_key] = clean_value

    # Ensure both stripped and exact journal column forms exist.
    for column in JOURNAL_COLUMNS:
        column_s = str(column).strip()
        value = out.get(column_s, out.get(str(column), ""))
        out[str(column)] = value
        out[column_s] = value

    return out


def _eval_fingerprint(row: Dict[str, Any]) -> Tuple[str, ...]:
    keys = [
        "timestamp_utc",
        "symbol",
        "direction",
        "trend",
        "score",
        "threshold",
        "regime",
        "duration_min",
        "entry_adx",
        "entry_rsi",
        "entry_macd_hist",
        "atr",
        "close",
        "tf_5m",
        "tf_15m",
        "tf_30m",
        "tf_1h",
        "mtf_agreement",
    ] + FACTOR_KEYS
    return tuple(str(row.get(k, "") or "").strip().lower() for k in keys)


def _outcome_fingerprint(row: Dict[str, Any]) -> Tuple[str, ...]:
    keys = [
        "signal_id",
        "outcome",
        "pnl",
        "stake",
        "martingale_step",
        "contract_id",
        "execution_mode",
        "note",
        "mae",
        "mfe",
    ]
    return tuple(str(row.get(k, "") or "").strip().lower() for k in keys)


def _read_file_bytes(path: str) -> bytes:
    try:
        if path and os.path.exists(path):
            with open(path, "rb") as handle:
                return handle.read()
    except Exception:
        pass
    return b""


def _snapshot_count() -> int:
    try:
        if not os.path.exists(SNAPSHOT_FILE):
            return 0
        with open(SNAPSHOT_FILE, "r", encoding="utf-8") as handle:
            return sum(1 for _ in handle)
    except Exception:
        return 0


def _existing_fingerprints(journal: Any) -> Tuple[set, set, set, set]:
    eval_signal_ids: set = set()
    outcome_signal_ids: set = set()
    eval_hashes: set = set()
    outcome_hashes: set = set()

    archive_path = getattr(journal, "_archive", None)
    if archive_path and os.path.exists(archive_path):
        try:
            with open(archive_path, "r", newline="", encoding="utf-8") as handle:
                for raw in csv.DictReader(handle):
                    row = _clean_record(raw)
                    kind = str(row.get("kind", "EVAL") or "EVAL").strip().upper()
                    signal_id = str(row.get("signal_id", "") or "").strip()

                    if kind == "EVAL":
                        if signal_id:
                            eval_signal_ids.add(signal_id)
                        eval_hashes.add(_eval_fingerprint(row))
                    elif kind == "OUTCOME":
                        if signal_id:
                            outcome_signal_ids.add(signal_id)
                        outcome_hashes.add(_outcome_fingerprint(row))
                    else:
                        # Be permissive: treat unknown kinds as merged rows.
                        if signal_id:
                            eval_signal_ids.add(signal_id)
                            if row.get("outcome"):
                                outcome_signal_ids.add(signal_id)
                        eval_hashes.add(_eval_fingerprint(row))
                        if row.get("outcome"):
                            outcome_hashes.add(_outcome_fingerprint(row))
        except Exception:
            pass

    # Fallback / reinforcement from the merged view.
    try:
        for raw in journal.read_archive_merged():
            row = _clean_record(raw)
            signal_id = str(row.get("signal_id", "") or "").strip()
            if signal_id:
                eval_signal_ids.add(signal_id)
                if row.get("outcome"):
                    outcome_signal_ids.add(signal_id)
            eval_hashes.add(_eval_fingerprint(row))
            if row.get("outcome"):
                outcome_hashes.add(_outcome_fingerprint(row))
    except Exception:
        pass

    return eval_signal_ids, outcome_signal_ids, eval_hashes, outcome_hashes


# ---------------------------------------------------------------------------
# (A) Backup / restore
# ---------------------------------------------------------------------------

def export_archive_csv_bytes(journal: Any) -> bytes:
    """Return the raw append-only archive CSV if present.

    This archive is the lossless master copy: every EVAL and OUTCOME event.
    """
    archive_path = getattr(journal, "_archive", None)
    data = _read_file_bytes(archive_path) if archive_path else b""
    if data:
        return data

    # Fallback: the live journal is better than nothing.
    try:
        return journal.to_csv_bytes() or b""
    except Exception:
        return b""


def export_merged_json_bytes(journal: Any) -> bytes:
    """Return a human-readable merged JSON view of the journal."""
    try:
        rows = journal.read_archive_merged() or []
    except Exception:
        rows = []

    clean_rows = [_clean_record(row) for row in rows]
    payload = {
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "rows": clean_rows,
    }
    return json.dumps(payload, indent=2, default=str).encode("utf-8")


def import_journal(journal: Any, data: Any, filename: str = "import.csv") -> Dict[str, int]:
    """Import an archive CSV or merged JSON into the journal idempotently.

    Merge rule:
      - EVAL rows are de-duplicated by signal_id where possible, otherwise by
        a content fingerprint.
      - OUTCOME rows are de-duplicated by signal_id.

    Re-importing the same file should add nothing the second time.
    """
    counts = {
        "eval_added": 0,
        "outcome_added": 0,
        "skipped": 0,
        "errors": 0,
    }

    try:
        if isinstance(data, (bytes, bytearray)):
            text = data.decode("utf-8-sig")
        else:
            text = str(data)
    except Exception:
        counts["errors"] += 1
        return counts

    eval_signal_ids, outcome_signal_ids, eval_hashes, outcome_hashes = _existing_fingerprints(journal)

    fname = str(filename or "").lower()
    stripped = text.lstrip()
    is_json = fname.endswith(".json") or stripped.startswith("[") or stripped.startswith("{")

    records: List[Tuple[str, Dict[str, Any]]] = []

    if is_json:
        try:
            loaded = json.loads(text)

            if isinstance(loaded, dict):
                if any(k in loaded for k in ("signal_id", "timestamp_utc", "score")):
                    loaded = [loaded]
                else:
                    loaded = (
                        loaded.get("rows")
                        or loaded.get("journal")
                        or loaded.get("data")
                        or loaded.get("records")
                        or []
                    )

            if isinstance(loaded, list):
                for row in loaded:
                    if isinstance(row, dict):
                        records.append(("MERGED", row))
            else:
                counts["errors"] += 1
                return counts
        except Exception:
            counts["errors"] += 1
            return counts
    else:
        try:
            reader = csv.DictReader(io.StringIO(text))
            fieldnames = [str(x).strip() for x in (reader.fieldnames or [])]
            has_kind = "kind" in fieldnames

            for raw in reader:
                if has_kind:
                    kind = str(raw.get("kind", "EVAL") or "EVAL").strip().upper()
                else:
                    kind = "MERGED"
                records.append((kind, raw))
        except Exception:
            counts["errors"] += 1
            return counts

    for kind, raw in records:
        try:
            row = _clean_record(raw)
            signal_id = str(row.get("signal_id", "") or "").strip()
            outcome = str(row.get("outcome", "") or "").strip().upper()

            if kind == "OUTCOME":
                if not signal_id:
                    counts["skipped"] += 1
                    continue

                fingerprint = _outcome_fingerprint(row)
                if signal_id in outcome_signal_ids or fingerprint in outcome_hashes:
                    counts["skipped"] += 1
                    continue

                journal.record_outcome(
                    signal_id,
                    outcome,
                    _to_float(row.get("pnl"), 0.0) or 0.0,
                    _to_float(row.get("stake"), 0.0) or 0.0,
                    row.get("contract_id") or None,
                    row.get("execution_mode", "") or "",
                    row.get("martingale_step", "") or "",
                    row.get("note", "") or "",
                    row.get("mae", "") or "",
                    row.get("mfe", "") or "",
                )

                outcome_signal_ids.add(signal_id)
                outcome_hashes.add(fingerprint)
                counts["outcome_added"] += 1
                continue

            # EVAL or MERGED
            fingerprint = _eval_fingerprint(row)
            if (signal_id and signal_id in eval_signal_ids) or fingerprint in eval_hashes:
                counts["skipped"] += 1
            else:
                journal.record_evaluation(row)
                if signal_id:
                    eval_signal_ids.add(signal_id)
                eval_hashes.add(fingerprint)
                counts["eval_added"] += 1

            if outcome and signal_id:
                outcome_fp = _outcome_fingerprint(row)
                if signal_id in outcome_signal_ids or outcome_fp in outcome_hashes:
                    counts["skipped"] += 1
                else:
                    journal.record_outcome(
                        signal_id,
                        outcome,
                        _to_float(row.get("pnl"), 0.0) or 0.0,
                        _to_float(row.get("stake"), 0.0) or 0.0,
                        row.get("contract_id") or None,
                        row.get("execution_mode", "") or "",
                        row.get("martingale_step", "") or "",
                        row.get("note", "") or "",
                        row.get("mae", "") or "",
                        row.get("mfe", "") or "",
                    )
                    outcome_signal_ids.add(signal_id)
                    outcome_hashes.add(outcome_fp)
                    counts["outcome_added"] += 1
            elif outcome and not signal_id:
                counts["skipped"] += 1

        except Exception:
            counts["errors"] += 1

    return counts


# ---------------------------------------------------------------------------
# (B) Post-mortem
# ---------------------------------------------------------------------------

def _is_trending_review(row: Dict[str, Any]) -> bool:
    for key in ("trend", "tf_30m", "tf_1h"):
        if str(row.get(key, "") or "").strip().upper() in {"UP", "DOWN"}:
            return True
    return False


def _weakest_factor(row: Dict[str, Any]) -> Optional[str]:
    values: List[Tuple[float, str]] = []
    for key in FACTOR_KEYS:
        value = _to_float(row.get(key), None)
        if value is not None:
            values.append((value, FACTOR_TO_BASE.get(key, key)))

    if not values:
        return None

    values.sort(key=lambda item: item[0])
    return values[0][1]


def _trade_brief(row: Dict[str, Any], pnl: float, mae: Optional[float], mfe: Optional[float], lens: str) -> Dict[str, Any]:
    mae_f = _to_float(mae, None)
    mfe_f = _to_float(mfe, None)
    spread = None
    if mae_f is not None and mfe_f is not None:
        spread = round(mfe_f - mae_f, 5)

    return {
        "timestamp_utc": row.get("timestamp_utc", ""),
        "signal_id": row.get("signal_id", ""),
        "symbol": row.get("symbol", ""),
        "direction": row.get("direction", ""),
        "trend": row.get("trend", ""),
        "score": _to_float(row.get("score"), None),
        "threshold": _to_float(row.get("threshold"), None),
        "regime": row.get("regime", ""),
        "duration_min": row.get("duration_min", ""),
        "outcome": row.get("outcome", ""),
        "pnl": round(pnl, 2),
        "mae": mae_f,
        "mfe": mfe_f,
        "mfe_minus_mae": spread,
        "execution_mode": row.get("execution_mode", ""),
        "note": row.get("note", ""),
        "lens": lens,
    }


def _edge_add(bucket: Dict[str, Any], won: bool, pnl: float) -> None:
    bucket["trades"] += 1
    bucket["pnl"] += pnl
    if won:
        bucket["wins"] += 1


def _finalize_edges(edges: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for key, data in edges.items():
        trades = int(data.get("trades", 0))
        wins = int(data.get("wins", 0))
        pnl = float(data.get("pnl", 0.0))
        out[str(key)] = {
            "trades": trades,
            "wins": wins,
            "win_rate": round((wins / trades * 100.0) if trades else 0.0, 1),
            "pnl": round(pnl, 2),
        }
    return out


def compute_postmortem(journal: Any) -> Dict[str, Any]:
    """Compute the offline post-mortem from recorded data only."""
    try:
        rows = journal.read_archive_merged() or []
    except Exception:
        rows = []

    reviews = 0
    taken = 0
    closed = 0
    wins = 0
    losses = 0
    net_pnl = 0.0

    avoidable_losses: List[Dict[str, Any]] = []
    fragile_wins: List[Dict[str, Any]] = []
    gatekeeper_counter: Counter = Counter()

    edges_symbol: Dict[str, Dict[str, Any]] = defaultdict(lambda: {"trades": 0, "wins": 0, "pnl": 0.0})
    edges_hour: Dict[str, Dict[str, Any]] = defaultdict(lambda: {"trades": 0, "wins": 0, "pnl": 0.0})
    edges_regime: Dict[str, Dict[str, Any]] = defaultdict(lambda: {"trades": 0, "wins": 0, "pnl": 0.0})

    for raw in rows:
        row = _clean_record(raw)
        reviews += 1

        was_taken = _to_bool(row.get("taken")) or _to_bool(row.get("executed"))
        if was_taken:
            taken += 1

        outcome = str(row.get("outcome", "") or "").strip().upper()
        pnl = _to_float(row.get("pnl"), 0.0) or 0.0
        ts = _parse_timestamp(row.get("timestamp_utc"))
        mae = _to_float(row.get("mae"), None)
        mfe = _to_float(row.get("mfe"), None)

        if outcome in {"WON", "LOST"}:
            closed += 1
            net_pnl += pnl
            won = outcome == "WON"

            if won:
                wins += 1
            else:
                losses += 1

            symbol = str(row.get("symbol", "") or "unknown").strip() or "unknown"
            hour = f"{ts.hour:02d}" if ts else "unknown"
            regime = str(row.get("regime", "") or "unknown").strip() or "unknown"

            _edge_add(edges_symbol[symbol], won, pnl)
            _edge_add(edges_hour[hour], won, pnl)
            _edge_add(edges_regime[regime], won, pnl)

            if outcome == "LOST" and mae is not None and mfe is not None and mfe > mae * 1.0:
                avoidable_losses.append(
                    _trade_brief(row, pnl, mae, mfe, "duration_exit")
                )

            if outcome == "WON" and mae is not None and mfe is not None and mae > mfe * 1.0:
                fragile_wins.append(
                    _trade_brief(row, pnl, mae, mfe, "entry_timing")
                )

        if not was_taken:
            score = _to_float(row.get("score"), None)
            threshold = _to_float(row.get("threshold"), float(SCORE_MAX))
            if score is not None and threshold is not None:
                gap = threshold - score
                if 0 <= gap <= 8 and _is_trending_review(row):
                    weakest = _weakest_factor(row)
                    if weakest:
                        gatekeeper_counter[weakest] += 1

    avoidable_losses.sort(key=lambda item: _to_float(item.get("mfe_minus_mae"), 0.0) or 0.0, reverse=True)
    fragile_wins.sort(key=lambda item: abs(_to_float(item.get("mfe_minus_mae"), 0.0) or 0.0), reverse=True)

    win_rate = round((wins / closed * 100.0) if closed else 0.0, 1)

    return {
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "summary": {
            "reviews": reviews,
            "taken": taken,
            "closed": closed,
            "wins": wins,
            "losses": losses,
            "win_rate": win_rate,
            "net_pnl": round(net_pnl, 2),
            "snapshots_recorded": _snapshot_count(),
        },
        "avoidable_losses": avoidable_losses,
        "fragile_wins": fragile_wins,
        "gatekeeper_factors": gatekeeper_counter.most_common(),
        "edges": {
            "by_symbol": _finalize_edges(edges_symbol),
            "by_hour_utc": _finalize_edges(edges_hour),
            "by_regime": _finalize_edges(edges_regime),
        },
    }


# ---------------------------------------------------------------------------
# (C) Gate backtest
# ---------------------------------------------------------------------------

def _recompute_score(row: Dict[str, Any], multipliers: Dict[str, float]) -> Optional[int]:
    total = 0.0
    seen = 0

    for key in FACTOR_KEYS:
        raw_value = _to_float(row.get(key), None)
        if raw_value is None:
            continue

        base_name = FACTOR_TO_BASE.get(key, key.replace("s_", ""))
        max_points = float(BASE_FACTOR_WEIGHTS.get(base_name, 1) or 1)
        multiplier = float(multipliers.get(base_name, 1.0) or 1.0)

        # This mirrors the live score when multiplier == 1.0:
        # normalised factor * base weight * multiplier.
        total += (raw_value / max_points) * max_points * multiplier
        seen += 1

    if seen == 0:
        return _to_float(row.get("score"), None)

    return int(round(total))


def _baseline_sweep(clean_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    taken = 0
    closed = 0
    wins = 0
    net_pnl = 0.0

    for row in clean_rows:
        was_taken = _to_bool(row.get("taken")) or _to_bool(row.get("executed"))
        if not was_taken:
            continue

        taken += 1
        outcome = str(row.get("outcome", "") or "").strip().upper()
        pnl = _to_float(row.get("pnl"), 0.0) or 0.0

        if outcome in {"WON", "LOST"}:
            closed += 1
            net_pnl += pnl
            if outcome == "WON":
                wins += 1

    return {
        "variant": "AS-RECORDED (ground truth)",
        "threshold": "as-recorded",
        "would_take": taken,
        "kept": taken,
        "kept_pnl": round(net_pnl, 2),
        "kept_win_rate": round((wins / closed * 100.0) if closed else 0.0, 1) if closed else None,
        "dropped": 0,
        "dropped_pnl": 0.0,
        "dropped_losses_avoided": 0,
        "dropped_wins_lost": 0,
        "added_unknown": 0,
    }


def sweep_gates(journal: Any) -> List[Dict[str, Any]]:
    """Offline, non-destructive gate backtest.

    This never changes the bot. It only asks: if we had used this variant and
    threshold, what would have been kept / dropped / added-unknown?
    """
    try:
        rows = journal.read_archive_merged() or []
    except Exception:
        rows = []

    clean_rows = [_clean_record(row) for row in rows]
    results: List[Dict[str, Any]] = [_baseline_sweep(clean_rows)]

    for variant_name, multipliers in WEIGHT_VARIANTS.items():
        for threshold in SWEEP_THRESHOLDS:
            would_take = 0
            kept = 0
            kept_closed = 0
            kept_wins = 0
            kept_pnl = 0.0

            dropped = 0
            dropped_closed = 0
            dropped_pnl = 0.0
            dropped_losses_avoided = 0
            dropped_wins_lost = 0

            added_unknown = 0

            for row in clean_rows:
                recomputed = _recompute_score(row, multipliers)
                if recomputed is None:
                    continue

                would = recomputed >= threshold
                was_taken = _to_bool(row.get("taken")) or _to_bool(row.get("executed"))
                outcome = str(row.get("outcome", "") or "").strip().upper()
                pnl = _to_float(row.get("pnl"), 0.0) or 0.0
                closed = outcome in {"WON", "LOST"}

                if would:
                    would_take += 1

                if would and was_taken:
                    kept += 1
                    if closed:
                        kept_closed += 1
                        kept_pnl += pnl
                        if outcome == "WON":
                            kept_wins += 1

                elif (not would) and was_taken:
                    dropped += 1
                    if closed:
                        dropped_closed += 1
                        dropped_pnl += pnl
                        if outcome == "LOST":
                            dropped_losses_avoided += 1
                        elif outcome == "WON":
                            dropped_wins_lost += 1

                elif would and (not was_taken):
                    added_unknown += 1

            results.append(
                {
                    "variant": variant_name,
                    "threshold": threshold,
                    "would_take": would_take,
                    "kept": kept,
                    "kept_pnl": round(kept_pnl, 2),
                    "kept_win_rate": round((kept_wins / kept_closed * 100.0) if kept_closed else 0.0, 1)
                    if kept_closed
                    else None,
                    "dropped": dropped,
                    "dropped_pnl": round(dropped_pnl, 2),
                    "dropped_losses_avoided": dropped_losses_avoided,
                    "dropped_wins_lost": dropped_wins_lost,
                    "added_unknown": added_unknown,
                }
            )

    return results


def export_preset_text(variant_name: str, threshold: Any) -> str:
    """Export a plain-text preset proposal.

    This is deliberately proposal-only. It must be forward-tested on demo and
    manually opted into later. Nothing here auto-applies.
    """
    multipliers = WEIGHT_VARIANTS.get(variant_name, WEIGHT_VARIANTS["current"])

    lines: List[str] = []
    lines.append("MomentumMaster TF — Offline Preset Proposal")
    lines.append("==========================================")
    lines.append(f"generated_utc: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"variant: {variant_name}")
    lines.append(f"proposed_entry_score_threshold: {threshold}")
    lines.append("status: PROPOSAL_ONLY")
    lines.append("auto_apply: false")
    lines.append("forward_test_required: true")
    lines.append("")
    lines.append("This file is a human decision artifact. It does not modify the bot.")
    lines.append("If you want to use it, forward-test on demo first, then opt in manually.")
    lines.append("")
    lines.append("weight_multipliers:")
    for name in sorted(BASE_FACTOR_WEIGHTS):
        lines.append(f"  {name}: {float(multipliers.get(name, 1.0)):.2f}")
    lines.append("")
    lines.append("base_factor_max_points:")
    for name in sorted(BASE_FACTOR_WEIGHTS):
        lines.append(f"  {name}: {BASE_FACTOR_WEIGHTS[name]}")
    lines.append("")
    lines.append("Interpretation:")
    lines.append("- kept_pnl = real P&L from trades this variant would still have taken.")
    lines.append("- dropped_pnl = real P&L from trades this variant would have skipped.")
    lines.append("  Negative dropped_pnl means the variant would have avoided losers.")
    lines.append("  Positive dropped_pnl means it would have cut winners.")
    lines.append("- added_unknown = setups the variant would have taken but the bot did not.")
    lines.append("  These have no settled P&L and must be forward-tested.")
    lines.append("")
    lines.append("Suggested manual next step:")
    lines.append("1. Read the post-mortem.")
    lines.append("2. Compare this variant against AS-RECORDED.")
    lines.append("3. If it is better and still believable, test it on demo.")
    lines.append("4. Only then consider adding it as a named sensitivity preset.")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# (D) Learning bundle
# ---------------------------------------------------------------------------

def _read_me_first_text() -> str:
    return """MomentumMaster TF — Learning Bundle
===================================

This bundle is the offline learning loop.

CADENCE
- Review this weekly, not after every trade.
- Small samples overfit fast. Binary options are noisy. Be boring.

CONTENTS
- trade_journal.csv
    Live journal view.
- journal_archive.csv
    Append-only master copy. This is the lossless record.
- trade_snapshots.jsonl
    Candle windows around taken trades only.
    Each line is one taken setup with compact candle rows per timeframe.
- postmortem.json
    Structured post-mortem.
- gate_backtest.json
    Offline gate sweep. Non-destructive. Proposal only.
- READ_ME_FIRST.txt
    This file.

LENSES
- avoidable_losses
    LOST trades where MFE > MAE.
    Price was in favour, then reversed before expiry.
    Lever: duration / exit choice.

- fragile_wins
    WON trades where MAE > MFE.
    The trade survived, but it spent too much time against you.
    Lever: entry timing / trigger strictness.

- gatekeeper_factors
    For near-miss stand-asides in trending conditions, the weakest soft factor.
    Lever: the one gate to re-test, not everything at once.

- edges
    By symbol, hour, and regime.
    Lever: selection. Trade the best pools, not everything.

THE CEILING
- The bot does NOT rewrite itself.
- This bundle helps a human propose one change.
- That change must be forward-tested on demo.
- Only then should it be manually opted into.

A blank day is legitimate.
Even stand-asides produce data.
"""


def build_learning_bundle(journal: Any) -> bytes:
    """Build a zip bundle for offline review."""
    buffer = io.BytesIO()

    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as bundle:
        try:
            bundle.writestr("trade_journal.csv", journal.to_csv_bytes() or b"")
        except Exception:
            bundle.writestr("trade_journal.csv", b"")

        try:
            bundle.writestr("journal_archive.csv", export_archive_csv_bytes(journal) or b"")
        except Exception:
            bundle.writestr("journal_archive.csv", b"")

        try:
            if os.path.exists(SNAPSHOT_FILE):
                bundle.write(SNAPSHOT_FILE, arcname="trade_snapshots.jsonl")
            else:
                bundle.writestr("trade_snapshots.jsonl", b"")
        except Exception:
            bundle.writestr("trade_snapshots.jsonl", b"")

        try:
            postmortem = compute_postmortem(journal)
            bundle.writestr(
                "postmortem.json",
                json.dumps(postmortem, indent=2, default=str).encode("utf-8"),
            )
        except Exception:
            bundle.writestr("postmortem.json", b"{}")

        try:
            sweep = sweep_gates(journal)
            bundle.writestr(
                "gate_backtest.json",
                json.dumps(sweep, indent=2, default=str).encode("utf-8"),
            )
        except Exception:
            bundle.writestr("gate_backtest.json", b"[]")

        bundle.writestr("READ_ME_FIRST.txt", _read_me_first_text())

    return buffer.getvalue()
