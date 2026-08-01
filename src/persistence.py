"""src/persistence.py — offline learning loop: backup, post-mortem, gate backtest, bundle.

This module is additive and isolated. It reads the journal and optional snapshot
file only. It never imports the engine and never changes live strategy rules.
"""
import csv
import io
import json
import os
import zipfile
from collections import Counter, defaultdict
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from src.logger import get_logger

logger = get_logger("persistence")

# Mirror the live confluence schema exactly.
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

# These are the max points per factor in the live 25-point confidence stack.
FACTOR_MAX = {
    "s_trend": 5,
    "s_trigger": 3,
    "s_momentum": 3,
    "s_volatility": 2,
    "s_alignment": 1,
    "s_adx": 3,
    "s_macd": 2,
    "s_rsi_zone": 2,
    "s_pattern": 2,
    "s_structure": 2,
}

# Offline weight variants.
# The current variant reproduces the live score when confidence is computed as:
#   sum(round((recorded_factor_score / factor_max) * variant_weight))
WEIGHT_VARIANTS = {
    "current": {
        "s_trend": 5,
        "s_trigger": 3,
        "s_momentum": 3,
        "s_volatility": 2,
        "s_alignment": 1,
        "s_adx": 3,
        "s_macd": 2,
        "s_rsi_zone": 2,
        "s_pattern": 2,
        "s_structure": 2,
    },
    "trend_heavy": {
        "s_trend": 7,
        "s_trigger": 2,
        "s_momentum": 2,
        "s_volatility": 2,
        "s_alignment": 2,
        "s_adx": 4,
        "s_macd": 2,
        "s_rsi_zone": 1,
        "s_pattern": 1,
        "s_structure": 2,
    },
    "execution_heavy": {
        "s_trend": 3,
        "s_trigger": 5,
        "s_momentum": 5,
        "s_volatility": 1,
        "s_alignment": 1,
        "s_adx": 3,
        "s_macd": 3,
        "s_rsi_zone": 1,
        "s_pattern": 2,
        "s_structure": 1,
    },
    "pullback_patient": {
        "s_trend": 5,
        "s_trigger": 2,
        "s_momentum": 2,
        "s_volatility": 2,
        "s_alignment": 2,
        "s_adx": 3,
        "s_macd": 2,
        "s_rsi_zone": 3,
        "s_pattern": 2,
        "s_structure": 2,
    },
}

GATE_THRESHOLDS = [13, 16, 20, 23]
_TREND_VALUES = {"UP", "DOWN"}
_CLOSED_OUTCOMES = {"WON", "LOST"}


def _clean_row(row: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for k, v in (row or {}).items():
        if k is None:
            continue
        key = str(k).strip()
        if isinstance(v, str):
            v = v.strip()
        out[key] = v
    return out


def _safe_float(value: Any, default: Optional[float] = 0.0) -> Optional[float]:
    if value is None:
        return default
    if isinstance(value, bool):
        return float(value)
    try:
        if isinstance(value, str):
            value = value.strip().replace(",", "")
            if value == "":
                return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    f = _safe_float(value, None)
    if f is None:
        return default
    return int(f)


def _safe_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().upper() in {"TRUE", "1", "YES", "Y"}


def _parse_ts(value: Any) -> Optional[datetime]:
    s = str(value or "").strip()
    if not s:
        return None
    formats = (
        "%Y-%m-%d %H:%M:%S UTC",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S.%f",
    )
    for fmt in formats:
        try:
            return datetime.strptime(s, fmt)
        except Exception:
            pass
    try:
        return datetime.fromisoformat(s.replace("Z", ""))
    except Exception:
        return None


def _journal_paths(journal) -> Tuple[str, str]:
    live = str(getattr(journal, "_live", "") or "")
    archive = str(getattr(journal, "_archive", "") or "")
    return live, archive


def snapshots_path(journal) -> str:
    live, _ = _journal_paths(journal)
    if not live:
        return ""
    return os.path.join(os.path.dirname(live), "trade_snapshots.jsonl")


def _read_snapshots_bytes(journal) -> bytes:
    path = snapshots_path(journal)
    try:
        if path and os.path.exists(path):
            with open(path, "rb") as f:
                return f.read()
    except Exception as exc:
        logger.debug("Snapshot read failed: %s", exc)
    return b""


def _count_snapshot_lines(journal) -> int:
    path = snapshots_path(journal)
    try:
        if not path or not os.path.exists(path):
            return 0
        count = 0
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for _ in f:
                count += 1
        return count
    except Exception as exc:
        logger.debug("Snapshot count failed: %s", exc)
        return 0


def _existing_ids(journal) -> Tuple[set, set]:
    eval_ids = set()
    outcome_ids = set()

    _, archive_path = _journal_paths(journal)
    live_path, _ = _journal_paths(journal)

    try:
        if archive_path and os.path.exists(archive_path):
            with open(archive_path, "r", newline="", encoding="utf-8") as f:
                for raw in csv.DictReader(f):
                    row = _clean_row(raw)
                    sid = str(row.get("signal_id", "") or "").strip()
                    if not sid:
                        continue
                    kind = str(row.get("kind", "") or "").strip().upper()
                    if kind == "EVAL":
                        eval_ids.add(sid)
                    elif kind == "OUTCOME":
                        outcome_ids.add(sid)
    except Exception as exc:
        logger.debug("Existing archive id scan failed: %s", exc)

    try:
        if live_path and os.path.exists(live_path):
            with open(live_path, "r", newline="", encoding="utf-8") as f:
                for raw in csv.DictReader(f):
                    row = _clean_row(raw)
                    sid = str(row.get("signal_id", "") or "").strip()
                    if not sid:
                        continue
                    eval_ids.add(sid)
                    outcome = str(row.get("outcome", "") or "").strip().upper()
                    if outcome:
                        outcome_ids.add(sid)
    except Exception as exc:
        logger.debug("Existing live id scan failed: %s", exc)

    return eval_ids, outcome_ids


def export_archive_csv_bytes(journal) -> bytes:
    _, archive_path = _journal_paths(journal)
    try:
        if archive_path and os.path.exists(archive_path):
            with open(archive_path, "rb") as f:
                return f.read()
    except Exception as exc:
        logger.warning("Archive export failed: %s", exc)
    return b""


def export_merged_json_bytes(journal) -> bytes:
    try:
        rows = journal.read_archive_merged() or journal.read_rows() or []
        rows = [_clean_row(r) for r in rows]
        return json.dumps(rows, indent=2, default=str).encode("utf-8")
    except Exception as exc:
        logger.warning("Merged JSON export failed: %s", exc)
        return b"[]"


def _record_outcome_from_row(journal, row: Dict[str, Any]) -> None:
    sid = str(row.get("signal_id", "") or "").strip()
    outcome = str(row.get("outcome", "") or "").strip()
    pnl = _safe_float(row.get("pnl"), 0.0) or 0.0
    stake = _safe_float(row.get("stake"), 0.0) or 0.0

    cid_raw = str(row.get("contract_id", "") or "").strip()
    contract_id: Optional[Any]
    if not cid_raw or cid_raw.lower() in {"none", "null"}:
        contract_id = None
    else:
        try:
            contract_id = int(float(cid_raw))
        except Exception:
            contract_id = cid_raw

    execution_mode = str(row.get("execution_mode", "") or "")
    martingale_step = str(row.get("martingale_step", "") or "")
    note = str(row.get("note", "") or "")
    mae = row.get("mae", "") if row.get("mae", "") is not None else ""
    mfe = row.get("mfe", "") if row.get("mfe", "") is not None else ""

    journal.record_outcome(
        sid,
        outcome,
        pnl,
        stake,
        contract_id,
        execution_mode,
        martingale_step,
        note,
        mae,
        mfe,
    )


def _import_csv(
    journal,
    text: str,
    eval_ids: set,
    outcome_ids: set,
    local_eval: set,
    local_outcome: set,
    counts: Dict[str, int],
) -> None:
    text = (text or "").strip()
    if not text:
        counts["errors"] += 1
        return

    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        counts["errors"] += 1
        return

    fields = {str(f).strip() for f in reader.fieldnames if f is not None}
    is_archive = "kind" in fields

    for raw in reader:
        row = _clean_row(raw)
        if not row:
            continue

        sid = str(row.get("signal_id", "") or "").strip()
        if not sid:
            counts["skipped"] += 1
            continue

        kind = str(row.get("kind", "") or "").strip().upper() if is_archive else ""

        try:
            if is_archive:
                if kind == "EVAL":
                    if sid in eval_ids or sid in local_eval:
                        counts["skipped"] += 1
                    else:
                        journal.record_evaluation(row)
                        local_eval.add(sid)
                        counts["eval_added"] += 1
                elif kind == "OUTCOME":
                    if sid in outcome_ids or sid in local_outcome:
                        counts["skipped"] += 1
                    else:
                        _record_outcome_from_row(journal, row)
                        local_outcome.add(sid)
                        counts["outcome_added"] += 1
                else:
                    counts["skipped"] += 1
            else:
                # Treat as merged/live-style rows.
                if sid in eval_ids or sid in local_eval:
                    counts["skipped"] += 1
                else:
                    journal.record_evaluation(row)
                    local_eval.add(sid)
                    counts["eval_added"] += 1

                outcome = str(row.get("outcome", "") or "").strip().upper()
                if outcome and outcome not in {"NONE", "PENDING"}:
                    if sid in outcome_ids or sid in local_outcome:
                        counts["skipped"] += 1
                    else:
                        _record_outcome_from_row(journal, row)
                        local_outcome.add(sid)
                        counts["outcome_added"] += 1
        except Exception as exc:
            logger.debug("CSV import row failed: %s", exc)
            counts["errors"] += 1


def _import_json(
    journal,
    text: str,
    eval_ids: set,
    outcome_ids: set,
    local_eval: set,
    local_outcome: set,
    counts: Dict[str, int],
) -> None:
    try:
        obj = json.loads(text)
    except Exception as exc:
        logger.debug("JSON import parse failed: %s", exc)
        counts["errors"] += 1
        return

    if isinstance(obj, list):
        rows = obj
    elif isinstance(obj, dict):
        rows = obj.get("rows", [])
        if not isinstance(rows, list):
            rows = []
    else:
        rows = []

    if not rows:
        counts["skipped"] += 1
        return

    for raw in rows:
        row = _clean_row(raw if isinstance(raw, dict) else {})
        if not row:
            continue

        sid = str(row.get("signal_id", "") or "").strip()
        if not sid:
            counts["skipped"] += 1
            continue

        try:
            if sid in eval_ids or sid in local_eval:
                counts["skipped"] += 1
            else:
                journal.record_evaluation(row)
                local_eval.add(sid)
                counts["eval_added"] += 1

            outcome = str(row.get("outcome", "") or "").strip().upper()
            if outcome and outcome not in {"NONE", "PENDING"}:
                if sid in outcome_ids or sid in local_outcome:
                    counts["skipped"] += 1
                else:
                    _record_outcome_from_row(journal, row)
                    local_outcome.add(sid)
                    counts["outcome_added"] += 1
        except Exception as exc:
            logger.debug("JSON import row failed: %s", exc)
            counts["errors"] += 1


def import_journal(journal, data: Any, filename: str) -> Dict[str, int]:
    counts = {"eval_added": 0, "outcome_added": 0, "skipped": 0, "errors": 0}

    try:
        if isinstance(data, bytes):
            text = data.decode("utf-8-sig")
        else:
            text = str(data)
    except Exception as exc:
        logger.warning("Import decode failed: %s", exc)
        counts["errors"] += 1
        return counts

    eval_ids, outcome_ids = _existing_ids(journal)
    local_eval = set()
    local_outcome = set()

    fname = str(filename or "").lower()
    stripped = text.lstrip()

    try:
        if fname.endswith(".json") or stripped.startswith("[") or stripped.startswith("{"):
            _import_json(journal, text, eval_ids, outcome_ids, local_eval, local_outcome, counts)
        else:
            _import_csv(journal, text, eval_ids, outcome_ids, local_eval, local_outcome, counts)
    except Exception as exc:
        logger.warning("Import failed: %s", exc)
        counts["errors"] += 1

    return counts


def _is_trending_review(row: Dict[str, Any]) -> bool:
    trend = str(row.get("trend", "") or "").strip().upper()
    tf30 = str(row.get("tf_30m", "") or "").strip().upper()
    tf1h = str(row.get("tf_1h", "") or "").strip().upper()
    return trend in _TREND_VALUES or tf30 in _TREND_VALUES or tf1h in _TREND_VALUES


def _weakest_factor(row: Dict[str, Any]) -> Optional[str]:
    vals: List[Tuple[float, str]] = []
    for key in FACTOR_KEYS:
        v = _safe_float(row.get(key), None)
        if v is not None:
            vals.append((v, key))
    if not vals:
        return None
    min_val = min(v for v, _ in vals)
    for key in FACTOR_KEYS:
        v = _safe_float(row.get(key), None)
        if v is not None and v == min_val:
            return key
    return vals[0][1]


def _edge_add(edges: Dict[str, Dict[str, float]], key: str, outcome: str, pnl: float) -> None:
    key = str(key or "UNKNOWN").strip() or "UNKNOWN"
    if key not in edges:
        edges[key] = {"trades": 0, "wins": 0, "pnl": 0.0}
    edges[key]["trades"] += 1
    edges[key]["pnl"] += pnl
    if outcome == "WON":
        edges[key]["wins"] += 1


def _finalize_edges(edges: Dict[str, Dict[str, float]]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for key, v in edges.items():
        trades = int(v.get("trades", 0))
        wins = int(v.get("wins", 0))
        pnl = float(v.get("pnl", 0.0))
        out[key] = {
            "trades": trades,
            "wins": wins,
            "win_rate": round((wins / trades * 100.0) if trades else 0.0, 1),
            "pnl": round(pnl, 2),
        }
    return out


def _post_row(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "signal_id": str(row.get("signal_id", "") or ""),
        "timestamp_utc": str(row.get("timestamp_utc", "") or ""),
        "symbol": str(row.get("symbol", "") or ""),
        "direction": str(row.get("direction", "") or ""),
        "trend": str(row.get("trend", "") or ""),
        "score": _safe_int(row.get("score"), 0),
        "threshold": _safe_int(row.get("threshold"), 0),
        "regime": str(row.get("regime", "") or ""),
        "duration_min": str(row.get("duration_min", "") or ""),
        "stake": _safe_float(row.get("stake"), 0.0),
        "pnl": _safe_float(row.get("pnl"), 0.0),
        "mae": _safe_float(row.get("mae"), None),
        "mfe": _safe_float(row.get("mfe"), None),
        "note": str(row.get("note", "") or ""),
    }


def compute_postmortem(journal) -> Dict[str, Any]:
    rows: List[Dict[str, Any]] = []
    try:
        rows = [_clean_row(r) for r in (journal.read_archive_merged() or journal.read_rows() or [])]
    except Exception as exc:
        logger.warning("Post-mortem read failed: %s", exc)

    reviews = len(rows)
    taken = 0
    closed = 0
    wins = 0
    losses = 0
    net_pnl = 0.0

    avoidable_losses: List[Dict[str, Any]] = []
    fragile_wins: List[Dict[str, Any]] = []
    gatekeeper_counter: Counter = Counter()

    edges_symbol: Dict[str, Dict[str, float]] = defaultdict(lambda: {"trades": 0, "wins": 0, "pnl": 0.0})
    edges_hour: Dict[str, Dict[str, float]] = defaultdict(lambda: {"trades": 0, "wins": 0, "pnl": 0.0})
    edges_regime: Dict[str, Dict[str, float]] = defaultdict(lambda: {"trades": 0, "wins": 0, "pnl": 0.0})

    for row in rows:
        was_taken = _safe_bool(row.get("taken"))
        if was_taken:
            taken += 1

        outcome = str(row.get("outcome", "") or "").strip().upper()

        if outcome in _CLOSED_OUTCOMES:
            closed += 1
            pnl = _safe_float(row.get("pnl"), 0.0) or 0.0
            net_pnl += pnl

            if outcome == "WON":
                wins += 1
            else:
                losses += 1

            mae = _safe_float(row.get("mae"), None)
            mfe = _safe_float(row.get("mfe"), None)

            if outcome == "LOST" and mae is not None and mfe is not None and mfe > mae * 1.0:
                avoidable_losses.append(_post_row(row))

            if outcome == "WON" and mae is not None and mfe is not None and mae > mfe * 1.0:
                fragile_wins.append(_post_row(row))

            _edge_add(edges_symbol, row.get("symbol", "UNKNOWN"), outcome, pnl)

            ts = _parse_ts(row.get("timestamp_utc"))
            hour_key = f"{ts.hour:02d}" if ts is not None else "unknown"
            _edge_add(edges_hour, hour_key, outcome, pnl)

            regime = str(row.get("regime", "") or "").strip().upper() or "UNKNOWN"
            _edge_add(edges_regime, regime, outcome, pnl)

        elif not was_taken:
            score = _safe_float(row.get("score"), None)
            threshold = _safe_float(row.get("threshold"), None)
            if (
                score is not None
                and threshold is not None
                and 0 <= (threshold - score) <= 8
                and _is_trending_review(row)
            ):
                weakest = _weakest_factor(row)
                if weakest:
                    gatekeeper_counter[weakest] += 1

    avoidable_losses.sort(key=lambda r: str(r.get("timestamp_utc", "") or ""), reverse=True)
    fragile_wins.sort(key=lambda r: str(r.get("timestamp_utc", "") or ""), reverse=True)

    win_rate = round((wins / closed * 100.0) if closed else 0.0, 1)
    gatekeeper_factors = dict(gatekeeper_counter.most_common())
    top_gatekeeper = gatekeeper_counter.most_common(1)[0][0] if gatekeeper_counter else None

    return {
        "summary": {
            "reviews": reviews,
            "taken": taken,
            "closed": closed,
            "wins": wins,
            "losses": losses,
            "win_rate": win_rate,
            "net_pnl": round(net_pnl, 2),
            "snapshots_recorded": _count_snapshot_lines(journal),
        },
        "avoidable_losses": avoidable_losses,
        "fragile_wins": fragile_wins,
        "gatekeeper_factors": gatekeeper_factors,
        "top_gatekeeper": top_gatekeeper,
        "edges": {
            "by_symbol": _finalize_edges(edges_symbol),
            "by_hour": _finalize_edges(edges_hour),
            "by_regime": _finalize_edges(edges_regime),
        },
    }


def _confidence_from_row(row: Dict[str, Any], weights: Dict[str, int]) -> int:
    total = 0.0
    for key in FACTOR_KEYS:
        raw = _safe_float(row.get(key), None)
        if raw is None:
            continue
        maxv = float(FACTOR_MAX.get(key, 1))
        if maxv <= 0:
            continue
        if raw < 0:
            raw = 0.0
        if raw > maxv:
            raw = maxv
        frac = raw / maxv
        total += frac * float(weights.get(key, 0))
    return int(round(total))


def sweep_gates(journal) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    try:
        rows = [_clean_row(r) for r in (journal.read_archive_merged() or journal.read_rows() or [])]
    except Exception as exc:
        logger.warning("Gate sweep read failed: %s", exc)

    results: List[Dict[str, Any]] = []

    taken_rows = [r for r in rows if _safe_bool(r.get("taken"))]
    closed_taken = [
        r for r in taken_rows
        if str(r.get("outcome", "") or "").strip().upper() in _CLOSED_OUTCOMES
    ]
    baseline_pnl = sum((_safe_float(r.get("pnl"), 0.0) or 0.0) for r in closed_taken)
    baseline_wins = sum(
        1 for r in closed_taken
        if str(r.get("outcome", "") or "").strip().upper() == "WON"
    )

    results.append(
        {
            "variant": "AS-RECORDED (ground truth)",
            "threshold": "as-recorded",
            "would_take": len(taken_rows),
            "kept": len(closed_taken),
            "kept_pnl": round(baseline_pnl, 2),
            "kept_win_rate": round((baseline_wins / len(closed_taken) * 100.0) if closed_taken else 0.0, 1),
            "dropped": 0,
            "dropped_pnl": 0.0,
            "dropped_losses_avoided": 0,
            "dropped_wins_lost": 0,
            "added_unknown": 0,
        }
    )

    for variant_name, weights in WEIGHT_VARIANTS.items():
        for threshold in GATE_THRESHOLDS:
            would_take = 0

            kept_closed = 0
            kept_wins = 0
            kept_pnl = 0.0

            dropped_closed = 0
            dropped_pnl = 0.0
            dropped_losses_avoided = 0
            dropped_wins_lost = 0

            added_unknown = 0

            for row in rows:
                conf = _confidence_from_row(row, weights)
                would = conf >= threshold
                was = _safe_bool(row.get("taken"))
                outcome = str(row.get("outcome", "") or "").strip().upper()
                is_closed = outcome in _CLOSED_OUTCOMES
                pnl = (_safe_float(row.get("pnl"), 0.0) or 0.0) if is_closed else 0.0

                if would:
                    would_take += 1

                if would and was:
                    if is_closed:
                        kept_closed += 1
                        kept_pnl += pnl
                        if outcome == "WON":
                            kept_wins += 1
                elif (not would) and was:
                    if is_closed:
                        dropped_closed += 1
                        dropped_pnl += pnl
                        if outcome == "LOST":
                            dropped_losses_avoided += 1
                        elif outcome == "WON":
                            dropped_wins_lost += 1
                elif would and (not was):
                    added_unknown += 1

            results.append(
                {
                    "variant": variant_name,
                    "threshold": threshold,
                    "would_take": would_take,
                    "kept": kept_closed,
                    "kept_pnl": round(kept_pnl, 2),
                    "kept_win_rate": round((kept_wins / kept_closed * 100.0) if kept_closed else 0.0, 1),
                    "dropped": dropped_closed,
                    "dropped_pnl": round(dropped_pnl, 2),
                    "dropped_losses_avoided": dropped_losses_avoided,
                    "dropped_wins_lost": dropped_wins_lost,
                    "added_unknown": added_unknown,
                }
            )

    return results


def export_preset_text(variant: str, threshold: Any, metrics: Optional[Dict[str, Any]] = None) -> str:
    weights = WEIGHT_VARIANTS.get(variant, WEIGHT_VARIANTS["current"])
    now = datetime.utcnow().isoformat() + "Z"

    lines: List[str] = []
    lines.append("# MomentumMaster TF offline preset proposal")
    lines.append(f"# Generated UTC: {now}")
    lines.append("# THIS IS NOT AUTO-APPLIED.")
    lines.append("# Forward-test on demo before opting in manually.")
    lines.append("")
    lines.append(f"variant = {variant!r}")
    lines.append(f"threshold = {threshold!r}")
    lines.append("")

    if metrics:
        lines.append(
            "# Backtest: "
            f"kept_pnl={metrics.get('kept_pnl')}, "
            f"kept_win_rate={metrics.get('kept_win_rate')}%, "
            f"dropped_losses_avoided={metrics.get('dropped_losses_avoided')}, "
            f"dropped_wins_lost={metrics.get('dropped_wins_lost')}, "
            f"added_unknown={metrics.get('added_unknown')}"
        )
        lines.append("")

    lines.append("weights = {")
    for key in FACTOR_KEYS:
        lines.append(f"    {key!r}: {weights.get(key, 0)},")
    lines.append("}")
    lines.append("")
    lines.append("# Manual opt-in example after review:")
    lines.append(
        f"# STRATEGY_SENSITIVITY_PRESETS[\"Offline {variant} @{threshold}\"] = "
        f"{{\"entry_score_threshold\": {threshold}, \"entry_adx_floor\": 15}}"
    )

    return "\n".join(lines)


def _learning_readme_text(post: Dict[str, Any]) -> str:
    s = post.get("summary", {}) if isinstance(post, dict) else {}
    return (
        "MomentumMaster TF learning bundle\n"
        "=================================\n\n"
        "Cadence:\n"
        "- Review this bundle once per week, not after every trade.\n"
        "- Prefer one proposed change at a time.\n"
        "- Forward-test on demo before opting in manually.\n\n"
        "Honest ceiling:\n"
        "- Nothing in this bundle auto-mutates the bot.\n"
        "- The bot learns BETWEEN sessions, offline, through a human review loop.\n"
        "- A blank day is legitimate; stand-asides still produce gatekeeper data.\n\n"
        "What each part means:\n"
        "- trade_journal.csv: live journal view.\n"
        "- journal_archive.csv: lossless append-only master archive (EVAL + OUTCOME rows).\n"
        "- trade_snapshots.jsonl: candle windows captured around taken signals.\n"
        "- postmortem.json: computed lenses:\n"
        "  * avoidable_losses: LOST where MFE > MAE => duration/exit problem.\n"
        "  * fragile_wins: WON where MAE > MFE => entry-timing/noise problem.\n"
        "  * gatekeeper_factors: weakest soft factor on trending near-miss stand-asides.\n"
        "  * edges: by symbol, hour, and regime.\n\n"
        "Current summary:\n"
        f"- reviews: {s.get('reviews', 0)}\n"
        f"- taken: {s.get('taken', 0)}\n"
        f"- closed: {s.get('closed', 0)}\n"
        f"- wins: {s.get('wins', 0)}\n"
        f"- losses: {s.get('losses', 0)}\n"
        f"- win_rate: {s.get('win_rate', 0)}\n"
        f"- net_pnl: {s.get('net_pnl', 0)}\n"
        f"- snapshots_recorded: {s.get('snapshots_recorded', 0)}\n"
    )


def build_learning_bundle(journal) -> bytes:
    post = compute_postmortem(journal)
    buf = io.BytesIO()

    live_bytes = b""
    try:
        live_bytes = journal.to_csv_bytes() or b""
    except Exception as exc:
        logger.debug("Live journal export for bundle failed: %s", exc)

    archive_bytes = export_archive_csv_bytes(journal)
    snapshot_bytes = _read_snapshots_bytes(journal)
    post_bytes = json.dumps(post, indent=2, default=str).encode("utf-8")
    readme_bytes = _learning_readme_text(post).encode("utf-8")

    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("trade_journal.csv", live_bytes)
        zf.writestr("journal_archive.csv", archive_bytes)
        zf.writestr("trade_snapshots.jsonl", snapshot_bytes)
        zf.writestr("postmortem.json", post_bytes)
        zf.writestr("READ_ME_FIRST.txt", readme_bytes)

    return buf.getvalue()
