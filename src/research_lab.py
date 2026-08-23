"""src/research_lab.py — read-only research tape, offline backtester, and advisor.

This module is research-only.
It does not place trades.
It does not modify live strategy behavior.
It does not alter the dashboard engine.

It builds a local research tape from Deriv tick history, then simulates
paper trades across condition grids using that tape.
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import threading
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from config import AVAILABLE_MARKETS
from src.api_client import DerivAPIClient
from src.journal import get_journal

RESEARCH_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "logs",
)
RESEARCH_DB = os.path.join(RESEARCH_DIR, "research.db")

REVIEW_INTERVAL_SECONDS = 60
LOWER_DIGIT_MAX = 6
OVER_DIGITS = (7, 8, 9)
COMPARISON_DIGITS = tuple(range(1, 7))

DEFAULT_THRESHOLDS_PCT = (28, 30, 31, 33, 35, 37, 40)
DEFAULT_LOWER_NS = (1, 2, 3, 4, 5, 6, 8, 10, 12, 15, 20)
DEFAULT_UPPER_MODES = ("kill", "reset")
DEFAULT_WINDOW_SETS: Tuple[Tuple[int, int, int], ...] = (
    (10, 30, 120),
    (20, 50, 200),
    (30, 80, 300),
)

_DB_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Database plumbing
# ---------------------------------------------------------------------------

def _ensure_db() -> None:
    os.makedirs(RESEARCH_DIR, exist_ok=True)

    with _DB_LOCK, sqlite3.connect(RESEARCH_DB, timeout=30) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ticks (
                symbol TEXT NOT NULL,
                epoch INTEGER NOT NULL,
                price REAL NOT NULL,
                digit INTEGER NOT NULL,
                precision INTEGER NOT NULL DEFAULT 2,
                PRIMARY KEY (symbol, epoch)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tape_meta (
                symbol TEXT PRIMARY KEY,
                first_epoch INTEGER,
                last_epoch INTEGER,
                tick_count INTEGER,
                last_updated_utc TEXT
            )
            """
        )
        conn.commit()


def _utc_from_epoch(epoch: Optional[int]) -> str:
    try:
        if epoch is None:
            return ""
        return datetime.fromtimestamp(int(epoch), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Digit / precision helpers
# ---------------------------------------------------------------------------

def _precision_from_pip(value: Any) -> int:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return 2

    if 0 < numeric < 1:
        exponent = Decimal(str(numeric)).as_tuple().exponent
        if isinstance(exponent, int) and exponent < 0:
            return max(0, min(10, -exponent))

    if numeric.is_integer() and 0 <= numeric <= 10:
        return int(numeric)

    return 2


def _digit_from_price(price: Any, precision: int) -> int:
    scaled = Decimal(str(price)) * (Decimal(10) ** int(precision))
    integer = int(scaled.to_integral_value(rounding=ROUND_HALF_UP))
    return abs(integer) % 10


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        text = str(value).strip()
        if not text:
            return default
        return float(text)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Deriv evidence collection
# ---------------------------------------------------------------------------

def get_accounts_sync(api_token: str, app_id: str) -> List[Dict[str, Any]]:
    async def _run() -> List[Dict[str, Any]]:
        return await DerivAPIClient.get_accounts(api_token, app_id)

    return asyncio.run(_run())


async def _fetch_symbol_precisions(client: DerivAPIClient) -> Dict[str, int]:
    items = await client.get_active_symbols(full=True)
    mapping: Dict[str, int] = {}

    for item in items:
        symbol = str(item.get("symbol", "") or "").strip()
        if not symbol:
            continue

        pip = item.get("pip_size")
        if pip in (None, ""):
            pip = item.get("pip")
        if pip in (None, ""):
            pip = item.get("pipSize")

        mapping[symbol] = _precision_from_pip(pip)

    return mapping


def _insert_ticks(rows: Sequence[Tuple[str, int, float, int, int]]) -> int:
    if not rows:
        return 0

    _ensure_db()

    with _DB_LOCK, sqlite3.connect(RESEARCH_DB, timeout=30) as conn:
        before = conn.total_changes

        conn.executemany(
            """
            INSERT OR IGNORE INTO ticks(symbol, epoch, price, digit, precision)
            VALUES (?, ?, ?, ?, ?)
            """,
            rows,
        )
        inserted = max(0, conn.total_changes - before)

        symbols = sorted({row[0] for row in rows})
        now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        for symbol in symbols:
            cur = conn.execute(
                """
                SELECT COUNT(*), MIN(epoch), MAX(epoch)
                FROM ticks
                WHERE symbol = ?
                """,
                (symbol,),
            )
            tick_count, first_epoch, last_epoch = cur.fetchone()

            conn.execute("DELETE FROM tape_meta WHERE symbol = ?", (symbol,))
            conn.execute(
                """
                INSERT INTO tape_meta(symbol, first_epoch, last_epoch, tick_count, last_updated_utc)
                VALUES (?, ?, ?, ?, ?)
                """,
                (symbol, first_epoch, last_epoch, tick_count, now_utc),
            )

        conn.commit()
        return inserted


def collect_symbol_history(
    api_token: str,
    app_id: str,
    account_id: str,
    symbol: str,
    count: int = 5000,
) -> Dict[str, Any]:
    """Fetch tick history from Deriv and append it to the local research tape."""

    async def _run() -> Dict[str, Any]:
        client = DerivAPIClient(api_token, app_id, account_id)

        if not await client.connect():
            raise RuntimeError(client.last_error or "Could not connect to Deriv.")

        try:
            precisions = await _fetch_symbol_precisions(client)
            precision = int(precisions.get(symbol, 2))

            history = await client.get_ticks_history(symbol, count=max(100, min(5000, int(count))))
            prices = history.get("prices", []) or []
            times = history.get("times", []) or []

            rows: List[Tuple[str, int, float, int, int]] = []

            for price_raw, time_raw in zip(prices, times):
                try:
                    epoch = int(float(time_raw))
                    price = float(price_raw)
                    digit = _digit_from_price(price, precision)
                    rows.append((symbol, epoch, price, digit, precision))
                except Exception:
                    continue

            inserted = _insert_ticks(rows)

            return {
                "symbol": symbol,
                "fetched": len(rows),
                "inserted": inserted,
                "precision": precision,
            }
        finally:
            await client.disconnect()

    return asyncio.run(_run())


def tape_summary() -> List[Dict[str, Any]]:
    _ensure_db()

    with _DB_LOCK, sqlite3.connect(RESEARCH_DB, timeout=30) as conn:
        cur = conn.execute(
            """
            SELECT symbol, first_epoch, last_epoch, tick_count, last_updated_utc
            FROM tape_meta
            ORDER BY symbol
            """
        )
        rows = cur.fetchall()

    summary: List[Dict[str, Any]] = []

    for symbol, first_epoch, last_epoch, tick_count, last_updated_utc in rows:
        summary.append(
            {
                "symbol": symbol,
                "ticks": int(tick_count or 0),
                "first_utc": _utc_from_epoch(first_epoch),
                "last_utc": _utc_from_epoch(last_epoch),
                "last_updated_utc": str(last_updated_utc or ""),
            }
        )

    return summary


def load_tape(symbol: str) -> List[Tuple[int, float, int]]:
    _ensure_db()

    with _DB_LOCK, sqlite3.connect(RESEARCH_DB, timeout=30) as conn:
        cur = conn.execute(
            """
            SELECT epoch, price, digit
            FROM ticks
            WHERE symbol = ?
            ORDER BY epoch ASC
            """,
            (symbol,),
        )
        return [(int(epoch), float(price), int(digit)) for epoch, price, digit in cur.fetchall()]


# ---------------------------------------------------------------------------
# Offline simulation core
# ---------------------------------------------------------------------------

def _window_stats_from_digits(
    digits: Sequence[int],
    end_index: int,
    window: int,
) -> Optional[Dict[str, float]]:
    window = int(window)

    if window <= 0:
        return None

    start = end_index - window + 1

    if start < 0:
        return None

    values = list(digits[start : end_index + 1])

    if len(values) < window:
        return None

    counts = Counter(values)

    over_count = sum(counts[d] for d in OVER_DIGITS)
    comparison_count = sum(counts[d] for d in COMPARISON_DIGITS)

    p_over6 = over_count / window
    p_1to6 = comparison_count / window

    p_over6_avg = p_over6 / 3.0
    p_1to6_avg = p_1to6 / 6.0

    return {
        "window": float(window),
        "over6_count": float(over_count),
        "comparison_count_1to6": float(comparison_count),
        "p_over6": p_over6,
        "p_1to6": p_1to6,
        "p_over6_avg": p_over6_avg,
        "p_1to6_avg": p_1to6_avg,
    }


def _stats_for_window_set(
    digits: Sequence[int],
    review_index: int,
    window_set: Tuple[int, int, int],
) -> Dict[str, Optional[Dict[str, float]]]:
    fast_window, medium_window, slow_window = window_set

    return {
        "fast": _window_stats_from_digits(digits, review_index, fast_window),
        "medium": _window_stats_from_digits(digits, review_index, medium_window),
        "slow": _window_stats_from_digits(digits, review_index, slow_window),
    }


def _qualify_stats(
    stats: Dict[str, Optional[Dict[str, float]]],
    threshold_pct: float,
) -> bool:
    fast = stats.get("fast")
    medium = stats.get("medium")
    slow = stats.get("slow")

    if not fast or not medium or not slow:
        return False

    threshold = float(threshold_pct) / 100.0
    slow_threshold = max(0.30, threshold - 0.01)

    if fast["p_over6"] < threshold:
        return False
    if medium["p_over6"] < threshold:
        return False
    if slow["p_over6"] < slow_threshold:
        return False

    if fast["p_over6_avg"] <= fast["p_1to6_avg"]:
        return False
    if medium["p_over6_avg"] <= medium["p_1to6_avg"]:
        return False
    if slow["p_over6_avg"] <= slow["p_1to6_avg"]:
        return False

    return True


def _first_entry_positions(
    post_digits: Sequence[int],
    lower_ns: Sequence[int],
    upper_mode: str,
) -> Dict[int, Optional[int]]:
    """Return first post-sequence position where each lower-N completes."""

    positions: Dict[int, Optional[int]] = {int(n): None for n in lower_ns}
    remaining = {int(n) for n in lower_ns}
    count = 0

    for position, digit in enumerate(post_digits):
        if digit <= LOWER_DIGIT_MAX:
            count += 1

            for required in list(remaining):
                if count >= required:
                    positions[required] = position
                    remaining.discard(required)

            if not remaining:
                break
        else:
            if str(upper_mode).lower() == "kill":
                break

            count = 0

    return positions


def _prepare_buckets(
    epochs: Sequence[int],
    digits: Sequence[int],
    max_window: int,
    lower_ns: Sequence[int],
    upper_modes: Sequence[str],
) -> List[Dict[str, Any]]:
    bucket_indices: Dict[int, List[int]] = {}

    for index, epoch in enumerate(epochs):
        bucket = int(epoch) // REVIEW_INTERVAL_SECONDS
        bucket_indices.setdefault(bucket, []).append(index)

    infos: List[Dict[str, Any]] = []
    required_review_index = max(0, int(max_window) - 1)

    for bucket in sorted(bucket_indices):
        indices = bucket_indices[bucket]

        if not indices:
            continue

        review_index = indices[0]

        if review_index < required_review_index:
            continue

        boundary_epoch = epochs[review_index]

        post_indices = [
            index
            for index in indices
            if epochs[index] > boundary_epoch
        ]

        if not post_indices:
            continue

        post_digits = [digits[index] for index in post_indices]

        entry_maps: Dict[str, Dict[int, Optional[int]]] = {}

        for mode in upper_modes:
            entry_maps[str(mode)] = _first_entry_positions(post_digits, lower_ns, mode)

        infos.append(
            {
                "bucket": bucket,
                "review_index": review_index,
                "boundary_epoch": boundary_epoch,
                "post_indices": post_indices,
                "entry_maps": entry_maps,
            }
        )

    return infos


def run_full_backtest(
    symbol: str,
    thresholds_pct: Sequence[float] = DEFAULT_THRESHOLDS_PCT,
    lower_ns: Sequence[int] = DEFAULT_LOWER_NS,
    upper_modes: Sequence[str] = DEFAULT_UPPER_MODES,
    window_sets: Sequence[Tuple[int, int, int]] = DEFAULT_WINDOW_SETS,
    duration_ticks: int = 1,
    payout_ratio: float = 0.95,
    min_trades: int = 1,
) -> List[Dict[str, Any]]:
    tape = load_tape(symbol)

    if len(tape) < 100:
        return []

    epochs = [int(row[0]) for row in tape]
    digits = [int(row[2]) for row in tape]

    window_sets_clean = [tuple(int(x) for x in ws) for ws in window_sets]
    thresholds_clean = [float(x) for x in thresholds_pct]
    lower_ns_clean = sorted({int(x) for x in lower_ns})
    upper_modes_clean = [str(x).lower() for x in upper_modes]

    if not window_sets_clean or not thresholds_clean or not lower_ns_clean or not upper_modes_clean:
        return []

    max_window = max(max(ws) for ws in window_sets_clean)

    infos = _prepare_buckets(
        epochs=epochs,
        digits=digits,
        max_window=max_window,
        lower_ns=lower_ns_clean,
        upper_modes=upper_modes_clean,
    )

    if not infos:
        return []

    results: List[Dict[str, Any]] = []
    duration = max(1, int(duration_ticks))
    payout = float(payout_ratio)

    for window_set in window_sets_clean:
        stats_by_info = [
            _stats_for_window_set(digits, info["review_index"], window_set)
            for info in infos
        ]

        for threshold_pct in thresholds_clean:
            qualify_by_info = [
                _qualify_stats(stats, threshold_pct)
                for stats in stats_by_info
            ]

            for upper_mode in upper_modes_clean:
                for lower_n in lower_ns_clean:
                    trades = 0
                    wins = 0
                    losses = 0
                    net_pnl = 0.0

                    for info_index, info in enumerate(infos):
                        if not qualify_by_info[info_index]:
                            continue

                        entry_map = info["entry_maps"].get(upper_mode, {})
                        post_position = entry_map.get(lower_n)

                        if post_position is None:
                            continue

                        entry_index = info["post_indices"][post_position]
                        settle_index = entry_index + duration

                        if settle_index >= len(digits):
                            continue

                        settle_digit = digits[settle_index]
                        win = settle_digit > 6

                        trades += 1

                        if win:
                            wins += 1
                            net_pnl += payout
                        else:
                            losses += 1
                            net_pnl -= 1.0

                    if trades > 0 and trades >= int(min_trades):
                        results.append(
                            {
                                "symbol": symbol,
                                "window_set": f"{window_set[0]}/{window_set[1]}/{window_set[2]}",
                                "fast_window": window_set[0],
                                "medium_window": window_set[1],
                                "slow_window": window_set[2],
                                "threshold_pct": round(float(threshold_pct), 2),
                                "lower_N": int(lower_n),
                                "upper_mode": upper_mode,
                                "duration_ticks": duration,
                                "payout_ratio": round(payout, 4),
                                "trades": trades,
                                "wins": wins,
                                "losses": losses,
                                "win_rate_pct": round((wins / trades * 100.0) if trades else 0.0, 1),
                                "net_pnl": round(net_pnl, 2),
                                "expectancy": round((net_pnl / trades) if trades else 0.0, 4),
                            }
                        )

    results.sort(
        key=lambda row: (
            row["net_pnl"],
            row["win_rate_pct"],
            row["trades"],
        ),
        reverse=True,
    )

    return results


def simulate_condition_trades(
    symbol: str,
    threshold_pct: float,
    lower_n: int,
    upper_mode: str,
    window_set: Tuple[int, int, int],
    duration_ticks: int = 1,
    payout_ratio: float = 0.95,
) -> List[Dict[str, Any]]:
    tape = load_tape(symbol)

    if len(tape) < 100:
        return []

    epochs = [int(row[0]) for row in tape]
    digits = [int(row[2]) for row in tape]

    window_set_clean = tuple(int(x) for x in window_set)
    max_window = max(window_set_clean)

    infos = _prepare_buckets(
        epochs=epochs,
        digits=digits,
        max_window=max_window,
        lower_ns=[int(lower_n)],
        upper_modes=[str(upper_mode).lower()],
    )

    trades: List[Dict[str, Any]] = []
    duration = max(1, int(duration_ticks))
    payout = float(payout_ratio)
    upper_mode_clean = str(upper_mode).lower()

    for info in infos:
        stats = _stats_for_window_set(digits, info["review_index"], window_set_clean)

        if not _qualify_stats(stats, threshold_pct):
            continue

        entry_map = info["entry_maps"].get(upper_mode_clean, {})
        post_position = entry_map.get(int(lower_n))

        if post_position is None:
            continue

        entry_index = info["post_indices"][post_position]
        settle_index = entry_index + duration

        if settle_index >= len(digits):
            continue

        entry_epoch = epochs[entry_index]
        entry_digit = digits[entry_index]
        settle_digit = digits[settle_index]
        win = settle_digit > 6

        pnl = payout if win else -1.0

        ts = datetime.fromtimestamp(int(entry_epoch), tz=timezone.utc)

        trades.append(
            {
                "timestamp_utc": ts.strftime("%Y-%m-%d %H:%M:%S"),
                "date": ts.strftime("%Y-%m-%d"),
                "hour": ts.strftime("%H"),
                "symbol": symbol,
                "threshold_pct": round(float(threshold_pct), 2),
                "lower_N": int(lower_n),
                "upper_mode": upper_mode_clean,
                "window_set": f"{window_set_clean[0]}/{window_set_clean[1]}/{window_set_clean[2]}",
                "duration_ticks": duration,
                "entry_digit": int(entry_digit),
                "settle_digit": int(settle_digit),
                "outcome": "WON" if win else "LOST",
                "pnl": round(pnl, 4),
            }
        )

    return trades


# ---------------------------------------------------------------------------
# Actual journal progress
# ---------------------------------------------------------------------------

def read_digit_journal_rows() -> List[Dict[str, Any]]:
    try:
        journal = get_journal()
        merged = journal.read_archive_merged() or []
    except Exception:
        merged = []

    rows: List[Dict[str, Any]] = []

    for raw in merged:
        row = {
            str(key): "" if value is None else str(value).strip()
            for key, value in (raw or {}).items()
        }

        if row.get("strategy_mode") != "DIGIT_OVER_6":
            continue

        signal_id = row.get("signal_id", "")
        timestamp = row.get("timestamp_utc", "")
        date = timestamp[:10] if len(timestamp) >= 10 else ""
        hour = timestamp[11:13] if len(timestamp) >= 13 else ""

        is_review = not bool(signal_id)
        qualifies = (
            is_review
            and row.get("direction", "") == "OVER6"
            and not row.get("rejection_reason", "")
        )
        taken = row.get("taken", "").upper() == "TRUE"
        outcome = row.get("outcome", "").upper()

        rows.append(
            {
                "signal_id": signal_id,
                "timestamp_utc": timestamp,
                "date": date,
                "hour": hour,
                "symbol": row.get("symbol", ""),
                "is_review": is_review,
                "qualifies": qualifies,
                "taken": taken,
                "outcome": outcome,
                "pnl": _safe_float(row.get("pnl"), 0.0),
                "lower_required": int(_safe_float(row.get("lower_confirmation_required"), 0)),
                "threshold_pct": _safe_float(row.get("threshold"), 31.0),
            }
        )

    return rows


def compute_actual_summary(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    reviews = sum(1 for row in rows if row["is_review"])
    arms = sum(1 for row in rows if row["is_review"] and row["qualifies"])
    entries = [row for row in rows if row["is_review"] is False and row["taken"]]
    closed = [row for row in entries if row["outcome"] in {"WON", "LOST"}]

    wins = sum(1 for row in closed if row["outcome"] == "WON")
    losses = sum(1 for row in closed if row["outcome"] == "LOST")
    unknown = sum(1 for row in entries if row["outcome"] == "UNKNOWN")
    cancelled = sum(1 for row in entries if row["outcome"] == "CANCELLED")
    net_pnl = sum(row["pnl"] for row in closed)

    return {
        "reviews": reviews,
        "arms": arms,
        "arm_rate_pct": round((arms / reviews * 100.0) if reviews else 0.0, 1),
        "entries": len(entries),
        "closed": len(closed),
        "wins": wins,
        "losses": losses,
        "unknown": unknown,
        "cancelled": cancelled,
        "win_rate_pct": round((wins / len(closed) * 100.0) if closed else 0.0, 1),
        "net_pnl": round(net_pnl, 2),
    }


def compute_actual_daily(rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    daily: Dict[str, Dict[str, Any]] = {}

    for row in rows:
        date = row["date"]

        if not date:
            continue

        day = daily.setdefault(
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
            },
        )

        if row["is_review"]:
            day["reviews"] += 1

            if row["qualifies"]:
                day["arms"] += 1
        elif row["taken"]:
            day["entries"] += 1

            if row["outcome"] in {"WON", "LOST"}:
                day["closed"] += 1
                day["pnl"] += row["pnl"]

                if row["outcome"] == "WON":
                    day["wins"] += 1
                else:
                    day["losses"] += 1

    result: List[Dict[str, Any]] = []
    cumulative = 0.0

    for date in sorted(daily):
        day = daily[date]
        cumulative += day["pnl"]

        result.append(
            {
                "date": day["date"],
                "reviews": day["reviews"],
                "arms": day["arms"],
                "entries": day["entries"],
                "closed": day["closed"],
                "wins": day["wins"],
                "losses": day["losses"],
                "win_rate_pct": round((day["wins"] / day["closed"] * 100.0) if day["closed"] else 0.0, 1),
                "pnl": round(day["pnl"], 2),
                "cum_pnl": round(cumulative, 2),
            }
        )

    return result


def simulated_daily_from_trades(trades: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    daily: Dict[str, Dict[str, Any]] = {}

    for trade in trades:
        date = str(trade.get("date", "")).strip()

        if not date:
            continue

        day = daily.setdefault(
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
            },
        )

        day["entries"] += 1
        day["closed"] += 1
        day["pnl"] += _safe_float(trade.get("pnl"), 0.0)

        if str(trade.get("outcome", "")).upper() == "WON":
            day["wins"] += 1
        else:
            day["losses"] += 1

    result: List[Dict[str, Any]] = []
    cumulative = 0.0

    for date in sorted(daily):
        day = daily[date]
        cumulative += day["pnl"]

        result.append(
            {
                "date": day["date"],
                "reviews": day["reviews"],
                "arms": day["arms"],
                "entries": day["entries"],
                "closed": day["closed"],
                "wins": day["wins"],
                "losses": day["losses"],
                "win_rate_pct": round((day["wins"] / day["closed"] * 100.0) if day["closed"] else 0.0, 1),
                "pnl": round(day["pnl"], 2),
                "cum_pnl": round(cumulative, 2),
            }
        )

    return result


def aggregate_monthly(daily: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    monthly: Dict[str, Dict[str, Any]] = {}

    for day in daily:
        key = str(day.get("date", ""))[:7]

        if not key:
            continue

        month = monthly.setdefault(
            key,
            {
                "month": key,
                "reviews": 0,
                "arms": 0,
                "entries": 0,
                "closed": 0,
                "wins": 0,
                "losses": 0,
                "pnl": 0.0,
            },
        )

        for field in ("reviews", "arms", "entries", "closed", "wins", "losses"):
            month[field] += int(day.get(field, 0) or 0)

        month["pnl"] += _safe_float(day.get("pnl"), 0.0)

    result: List[Dict[str, Any]] = []
    cumulative = 0.0

    for key in sorted(monthly):
        month = monthly[key]
        cumulative += month["pnl"]

        result.append(
            {
                "month": month["month"],
                "reviews": month["reviews"],
                "arms": month["arms"],
                "entries": month["entries"],
                "closed": month["closed"],
                "wins": month["wins"],
                "losses": month["losses"],
                "win_rate_pct": round((month["wins"] / month["closed"] * 100.0) if month["closed"] else 0.0, 1),
                "pnl": round(month["pnl"], 2),
                "cum_pnl": round(cumulative, 2),
            }
        )

    return result


def aggregate_yearly(daily: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    yearly: Dict[str, Dict[str, Any]] = {}

    for day in daily:
        key = str(day.get("date", ""))[:4]

        if not key:
            continue

        year = yearly.setdefault(
            key,
            {
                "year": key,
                "reviews": 0,
                "arms": 0,
                "entries": 0,
                "closed": 0,
                "wins": 0,
                "losses": 0,
                "pnl": 0.0,
            },
        )

        for field in ("reviews", "arms", "entries", "closed", "wins", "losses"):
            year[field] += int(day.get(field, 0) or 0)

        year["pnl"] += _safe_float(day.get("pnl"), 0.0)

    result: List[Dict[str, Any]] = []
    cumulative = 0.0

    for key in sorted(yearly):
        year = yearly[key]
        cumulative += year["pnl"]

        result.append(
            {
                "year": year["year"],
                "reviews": year["reviews"],
                "arms": year["arms"],
                "entries": year["entries"],
                "closed": year["closed"],
                "wins": year["wins"],
                "losses": year["losses"],
                "win_rate_pct": round((year["wins"] / year["closed"] * 100.0) if year["closed"] else 0.0, 1),
                "pnl": round(year["pnl"], 2),
                "cum_pnl": round(cumulative, 2),
            }
        )

    return result


def available_markets() -> Dict[str, str]:
    return dict(sorted(AVAILABLE_MARKETS.items(), key=lambda pair: str(pair[0]).lower()))
