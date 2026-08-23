"""Small persistence layer for the digit terminal.

In addition to lossless journal export / merged export / idempotent restore,
this module now provides read-only, journal-driven research analytics for the
digit strategy. Nothing here mutates the live strategy; it only interprets
what the journal already recorded.
"""
from __future__ import annotations

import csv
import io
import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from config import LOG_DIR
from src.journal import ARCHIVE_COLUMNS, COLUMNS

MIN_PROPOSAL_TRADES = 3
MIN_PROPOSAL_REVIEWS = 30


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
    keys = (
        "timestamp_utc", "symbol", "direction", "strategy_mode",
        "barrier", "duration_unit", "entry_digit", "lower_confirmation_digit",
    )
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


# ---------------------------------------------------------------------------
# Digit research analytics (read-only, journal-driven)
# ---------------------------------------------------------------------------

def _f(value: Any, default: float = 0.0) -> float:
    try:
        text = str(value).strip()
        if not text:
            return default
        return float(text)
    except (TypeError, ValueError):
        return default


def _f_opt(value: Any) -> Optional[float]:
    try:
        text = str(value).strip()
        if not text:
            return None
        return float(text)
    except (TypeError, ValueError):
        return None


def _i(value: Any, default: int = 0) -> int:
    try:
        text = str(value).strip()
        if not text:
            return default
        return int(float(text))
    except (TypeError, ValueError):
        return default


def _row_datetime(row: Dict[str, Any]) -> str:
    return str(row.get("timestamp_utc", "") or "").strip()


def _row_date(row: Dict[str, Any]) -> str:
    text = _row_datetime(row)
    return text[:10] if len(text) >= 10 else ""


def _row_hour(row: Dict[str, Any]) -> str:
    text = _row_datetime(row)
    return text[11:13] if len(text) >= 13 else ""


def normalize_digit_rows(journal: Any) -> List[Dict[str, Any]]:
    """Parse merged journal rows into typed digit-strategy records."""
    out: List[Dict[str, Any]] = []

    for raw in (journal.read_archive_merged() or []):
        row = _clean_record(raw)

        mode = str(row.get("strategy_mode", "") or "").strip()
        if mode and mode != "DIGIT_OVER_6":
            continue

        sid = str(row.get("signal_id", "") or "").strip()
        rejection = str(row.get("rejection_reason", "") or "").strip()
        outcome = str(row.get("outcome", "") or "").strip().upper()

        out.append(
            {
                "signal_id": sid,
                "is_entry": bool(sid),
                "is_review": not bool(sid),
                "taken": str(row.get("taken", "") or "").strip().upper() == "TRUE",
                "qualifies": (not sid)
                and str(row.get("direction", "") or "").strip() == "OVER6"
                and not rejection,
                "symbol": str(row.get("symbol", "") or "").strip(),
                "timestamp_utc": _row_datetime(row),
                "date": _row_date(row),
                "hour": _row_hour(row),
                "threshold_pct": _f(row.get("threshold"), 31.0),
                "p_fast": _f_opt(row.get("p_over6_fast")),
                "p_medium": _f_opt(row.get("p_over6_medium")),
                "p_slow": _f_opt(row.get("p_over6_slow")),
                "avg_fast": _f_opt(row.get("p_over6_avg_fast")),
                "avg_medium": _f_opt(row.get("p_over6_avg_medium")),
                "avg_slow": _f_opt(row.get("p_over6_avg_slow")),
                "low_avg_fast": _f_opt(row.get("p_1to6_avg_fast")),
                "low_avg_medium": _f_opt(row.get("p_1to6_avg_medium")),
                "low_avg_slow": _f_opt(row.get("p_1to6_avg_slow")),
                "lower_required": _i(row.get("lower_confirmation_required"), 0),
                "lower_count": _i(row.get("lower_confirmation_count"), 0),
                "entry_digit": _i(row.get("entry_digit"), -1),
                "outcome": outcome,
                "pnl": _f(row.get("pnl"), 0.0),
                "stake": _f(row.get("stake"), 0.0),
                "rejection": rejection,
                "note": str(row.get("note", "") or "").strip(),
            }
        )

    return out


def _passes_gate(row: Dict[str, Any], threshold_frac: float, require_slow: bool = True) -> bool:
    fast = row.get("p_fast")
    medium = row.get("p_medium")
    slow = row.get("p_slow")
    avg_fast = row.get("avg_fast")
    avg_medium = row.get("avg_medium")
    avg_slow = row.get("avg_slow")
    low_fast = row.get("low_avg_fast")
    low_medium = row.get("low_avg_medium")
    low_slow = row.get("low_avg_slow")

    if fast is None or medium is None or avg_fast is None or avg_medium is None:
        return False
    if low_fast is None or low_medium is None:
        return False
    if fast < threshold_frac or medium < threshold_frac:
        return False
    if avg_fast <= low_fast or avg_medium <= low_medium:
        return False

    if require_slow:
        slow_min = max(0.30, threshold_frac - 0.01)
        if slow is None or avg_slow is None or low_slow is None:
            return False
        if slow < slow_min or avg_slow <= low_slow:
            return False

    return True


def compute_digit_summary(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    reviews = sum(1 for r in rows if r["is_review"])
    arms = sum(1 for r in rows if r["is_review"] and r["qualifies"])
    entries = [r for r in rows if r["is_entry"] and r["taken"]]
    closed = [r for r in entries if r["outcome"] in {"WON", "LOST"}]
    wins = sum(1 for r in closed if r["outcome"] == "WON")
    losses = sum(1 for r in closed if r["outcome"] == "LOST")
    unknown = sum(1 for r in entries if r["outcome"] == "UNKNOWN")
    cancelled = sum(1 for r in entries if r["outcome"] == "CANCELLED")
    net_pnl = sum(r["pnl"] for r in closed)
    win_rate = (wins / len(closed) * 100.0) if closed else 0.0

    return {
        "reviews": reviews,
        "arms": arms,
        "arm_rate": (arms / reviews * 100.0) if reviews else 0.0,
        "entries": len(entries),
        "closed": len(closed),
        "wins": wins,
        "losses": losses,
        "unknown": unknown,
        "cancelled": cancelled,
        "win_rate": round(win_rate, 1),
        "net_pnl": round(net_pnl, 2),
    }


def compute_daily_progress(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    daily: Dict[str, Dict[str, Any]] = {}

    for r in rows:
        date = r["date"]
        if not date:
            continue
        d = daily.setdefault(
            date,
            {
                "date": date,
                "reviews": 0,
                "arms": 0,
                "entries": 0,
                "closed": 0,
                "wins": 0,
                "losses": 0,
                "pnl": 0.0,
                "_med_sum": 0.0,
                "_med_n": 0,
            },
        )

        if r["is_review"]:
            d["reviews"] += 1
            if r["qualifies"]:
                d["arms"] += 1
        elif r["taken"]:
            d["entries"] += 1
            if r["outcome"] in {"WON", "LOST"}:
                d["closed"] += 1
                d["pnl"] += r["pnl"]
                if r["outcome"] == "WON":
                    d["wins"] += 1
                else:
                    d["losses"] += 1

        if r["p_medium"] is not None:
            d["_med_sum"] += r["p_medium"]
            d["_med_n"] += 1

    result: List[Dict[str, Any]] = []
    cumulative = 0.0
    for date in sorted(daily):
        d = daily[date]
        cumulative += d["pnl"]
        result.append(
            {
                "date": d["date"],
                "reviews": d["reviews"],
                "arms": d["arms"],
                "entries": d["entries"],
                "closed": d["closed"],
                "wins": d["wins"],
                "losses": d["losses"],
                "win_rate": round((d["wins"] / d["closed"] * 100.0) if d["closed"] else 0.0, 1),
                "pnl": round(d["pnl"], 2),
                "cum_pnl": round(cumulative, 2),
                "avg_medium_7_9_pct": round((d["_med_sum"] / d["_med_n"] * 100.0) if d["_med_n"] else 0.0, 1),
            }
        )
    return result


def _group(rows: List[Dict[str, Any]], key: str) -> Dict[str, List[Dict[str, Any]]]:
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        groups.setdefault(str(r.get(key, "") or ""), []).append(r)
    return groups


def _avg(values: List[Optional[float]]) -> float:
    clean = [v for v in values if v is not None]
    return (sum(clean) / len(clean)) if clean else 0.0


def compute_review_conditions(rows: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    reviews = [r for r in rows if r["is_review"]]

    tables: Dict[str, List[Dict[str, Any]]] = {}
    for key, label in (("symbol", "market"), ("hour", "hour_utc")):
        out: List[Dict[str, Any]] = []
        for name, group in _group(reviews, key).items():
            if not name:
                continue
            arms = sum(1 for r in group if r["qualifies"])
            out.append(
                {
                    label: name,
                    "reviews": len(group),
                    "arms": arms,
                    "arm_rate_pct": round((arms / len(group) * 100.0) if group else 0.0, 1),
                    "avg_fast_7_9_pct": round(_avg([r["p_fast"] for r in group]) * 100.0, 1),
                    "avg_medium_7_9_pct": round(_avg([r["p_medium"] for r in group]) * 100.0, 1),
                    "avg_slow_7_9_pct": round(_avg([r["p_slow"] for r in group]) * 100.0, 1),
                }
            )
        out.sort(key=lambda item: item["avg_medium_7_9_pct"], reverse=True)
        tables[f"by_{label}"] = out
    return tables


def compute_condition_edges(rows: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    closed = [r for r in rows if r["is_entry"] and r["taken"] and r["outcome"] in {"WON", "LOST"}]

    def edge_table(key: str, label: str) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for name, group in _group(closed, key).items():
            if not name:
                continue
            wins = sum(1 for r in group if r["outcome"] == "WON")
            out.append(
                {
                    label: name,
                    "trades": len(group),
                    "wins": wins,
                    "win_rate_pct": round((wins / len(group) * 100.0) if group else 0.0, 1),
                    "pnl": round(sum(r["pnl"] for r in group), 2),
                    "avg_medium_7_9_pct": round(_avg([r["p_medium"] for r in group]) * 100.0, 1),
                }
            )
        out.sort(key=lambda item: item["pnl"], reverse=True)
        return out

    return {
        "by_symbol": edge_table("symbol", "market"),
        "by_hour": edge_table("hour", "hour_utc"),
        "by_lower_n": edge_table("lower_required", "lower_N"),
        "by_threshold": edge_table("threshold_pct", "threshold_pct"),
    }


def compute_gatekeepers(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    counts: Dict[str, int] = {}

    for r in rows:
        if not r["is_review"] or r["qualifies"] or not r["rejection"]:
            continue
        text = r["rejection"].lower()

        if "slow" in text:
            key = "slow support gate"
        elif "below" in text:
            key = "7–9 share below threshold"
        elif "average" in text or "exceed" in text or "per-digit" in text:
            key = "per-digit average comparison"
        elif "warming" in text or "need" in text:
            key = "warm-up / insufficient ticks"
        elif "killed" in text:
            key = "upper digit killed sequence"
        else:
            key = "other / operational"

        counts[key] = counts.get(key, 0) + 1

    out = [{"factor": k, "count": v} for k, v in counts.items()]
    out.sort(key=lambda item: item["count"], reverse=True)
    return out


def compute_missed_avoidable(rows: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    avoidable: List[Dict[str, Any]] = []
    fragile: List[Dict[str, Any]] = []
    missed: List[Dict[str, Any]] = []

    for r in rows:
        if r["is_entry"] and r["taken"] and r["outcome"] in {"WON", "LOST"}:
            weak = (
                r["p_medium"] is not None
                and (r["p_medium"] * 100.0) < (r["threshold_pct"] + 5.0)
            )
            base = {
                "timestamp_utc": r["timestamp_utc"],
                "symbol": r["symbol"],
                "outcome": r["outcome"],
                "pnl": round(r["pnl"], 2),
                "lower_N": r["lower_required"],
                "threshold_pct": r["threshold_pct"],
                "medium_7_9_pct": round((r["p_medium"] or 0.0) * 100.0, 1),
            }

            if r["outcome"] == "LOST":
                lenses = []
                if weak:
                    lenses.append("weak arm — medium share barely above threshold; lens: raise threshold")
                if r["lower_required"] <= 1:
                    lenses.append("fast entry N=1; lens: test N=2 on demo")
                if lenses:
                    avoidable.append({**base, "lens": " | ".join(lenses)})
            elif r["outcome"] == "WON" and weak:
                fragile.append({**base, "lens": "won on weak condition — verify sustainability before trusting it"})

        if r["is_review"] and not r["qualifies"]:
            thr = r["threshold_pct"] / 100.0
            strong = (
                r["p_fast"] is not None
                and r["p_medium"] is not None
                and r["p_fast"] >= thr
                and r["p_medium"] >= thr
                and r["avg_fast"] is not None
                and r["low_avg_fast"] is not None
                and r["avg_fast"] > r["low_avg_fast"]
                and r["avg_medium"] is not None
                and r["low_avg_medium"] is not None
                and r["avg_medium"] > r["low_avg_medium"]
            )
            if strong and "slow" in r["rejection"].lower():
                missed.append(
                    {
                        "timestamp_utc": r["timestamp_utc"],
                        "symbol": r["symbol"],
                        "fast_7_9_pct": round((r["p_fast"] or 0.0) * 100.0, 1),
                        "medium_7_9_pct": round((r["p_medium"] or 0.0) * 100.0, 1),
                        "slow_7_9_pct": round((r["p_slow"] or 0.0) * 100.0, 1),
                        "blocked_by": "slow support only",
                    }
                )

    return {"avoidable_losses": avoidable, "fragile_wins": fragile, "missed_arms": missed}


def sweep_digit_gates(rows: List[Dict[str, Any]], thresholds_pct: Tuple[int, ...] = (28, 30, 31, 33, 35, 37, 40)) -> List[Dict[str, Any]]:
    reviews = [r for r in rows if r["is_review"]]
    entries = [r for r in rows if r["is_entry"] and r["taken"]]
    closed = [r for r in entries if r["outcome"] in {"WON", "LOST"}]

    def stats(label: str, passing_entries: List[Dict[str, Any]], would_arm: Optional[int], note: str) -> Dict[str, Any]:
        wins = sum(1 for r in passing_entries if r["outcome"] == "WON")
        losses = sum(1 for r in passing_entries if r["outcome"] == "LOST")
        return {
            "setting": label,
            "would_arm_reviews": would_arm if would_arm is not None else "",
            "trades": len(passing_entries),
            "wins": wins,
            "losses": losses,
            "win_rate_pct": round((wins / (wins + losses) * 100.0) if (wins + losses) else 0.0, 1),
            "pnl": round(sum(r["pnl"] for r in passing_entries if r["outcome"] in {"WON", "LOST"}), 2),
            "note": note,
        }

    out = [stats("AS-RECORDED", closed, sum(1 for r in reviews if r["qualifies"]), "ground truth — what actually happened")]

    for pct in thresholds_pct:
        frac = pct / 100.0
        would_arm = sum(1 for r in reviews if _passes_gate(r, frac))
        passing = [r for r in closed if _passes_gate(r, frac)]
        out.append(stats(f"threshold {pct}%", passing, would_arm, "proposal — replay of recorded reviews only"))

    return out


def recommend_digit_settings(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    summary = compute_digit_summary(rows)
    sweep = sweep_digit_gates(rows)
    edges = compute_condition_edges(rows)
    conditions = compute_review_conditions(rows)

    def best(table: List[Dict[str, Any]], key: str, min_trades: int = MIN_PROPOSAL_TRADES) -> Optional[Dict[str, Any]]:
        candidates = [t for t in table if int(t.get("trades", 0)) >= min_trades]
        if not candidates:
            return None
        return max(candidates, key=lambda t: (float(t.get("win_rate_pct", 0.0)), float(t.get("pnl", 0.0))))

    threshold_pick = None
    for row in sweep[1:]:
        if int(row["trades"]) >= MIN_PROPOSAL_TRADES:
            if threshold_pick is None or (float(row["win_rate_pct"]), float(row["pnl"])) > (
                float(threshold_pick["win_rate_pct"]),
                float(threshold_pick["pnl"]),
            ):
                threshold_pick = row

    lower_pick = best(edges.get("by_lower_n", []), "lower_N")
    symbol_pick = best(edges.get("by_symbol", []), "market")
    hour_pick = best(edges.get("by_hour", []), "hour_utc")

    def best_review(table: List[Dict[str, Any]], label: str) -> Optional[Dict[str, Any]]:
        candidates = [t for t in table if int(t.get("reviews", 0)) >= MIN_PROPOSAL_REVIEWS]
        if not candidates:
            return None
        return max(candidates, key=lambda t: float(t.get("avg_medium_7_9_pct", 0.0)))

    env_symbol = best_review(conditions.get("by_market", []), "market")
    env_hour = best_review(conditions.get("by_hour", []), "hour_utc")

    return {
        "summary": summary,
        "threshold": threshold_pick,
        "lower_n": lower_pick,
        "symbol": symbol_pick,
        "hour": hour_pick,
        "env_symbol": env_symbol,
        "env_hour": env_hour,
        "min_trades": MIN_PROPOSAL_TRADES,
        "min_reviews": MIN_PROPOSAL_REVIEWS,
    }


def export_preset_text(recommendation: Dict[str, Any]) -> str:
    lines = [
        "MomentumMaster Digit — research preset proposal",
        "STATUS: PROPOSAL ONLY. Do not auto-apply. Forward-test on demo first.",
        f"generated_utc: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}",
        "",
    ]

    thr = recommendation.get("threshold")
    if thr:
        lines.append(f"threshold_pct: {thr['setting'].replace('threshold ', '').replace('%', '')}  (observed win rate {thr['win_rate_pct']}% over {thr['trades']} trades)")
    else:
        lines.append("threshold_pct: keep current (not enough closed trades yet)")

    lower = recommendation.get("lower_n")
    if lower:
        lines.append(f"lower_confirmations: {lower['lower_N']}  (observed win rate {lower['win_rate_pct']}% over {lower['trades']} trades)")
    else:
        lines.append("lower_confirmations: keep current (not enough closed trades yet)")

    symbol = recommendation.get("symbol")
    if symbol:
        lines.append(f"preferred_market: {symbol['market']}  (observed P&L {symbol['pnl']} over {symbol['trades']} trades)")
    else:
        lines.append("preferred_market: keep current")

    hour = recommendation.get("hour")
    if hour:
        lines.append(f"preferred_hour_utc: {hour['hour_utc']}:00  (observed win rate {hour['win_rate_pct']}% over {hour['trades']} trades)")
    else:
        lines.append("preferred_hour_utc: keep current")

    env_symbol = recommendation.get("env_symbol")
    env_hour = recommendation.get("env_hour")
    lines.append("")
    lines.append("Where 7–9 shows up most (reviews, not trades):")
    if env_symbol:
        lines.append(f"  market {env_symbol['market']}: avg medium 7–9 {env_symbol['avg_medium_7_9_pct']}% over {env_symbol['reviews']} reviews")
    if env_hour:
        lines.append(f"  hour {env_hour['hour_utc']}:00 UTC: avg medium 7–9 {env_hour['avg_medium_7_9_pct']}% over {env_hour['reviews']} reviews")

    return "\n".join(lines)
