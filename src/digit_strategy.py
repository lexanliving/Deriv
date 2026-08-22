"""Rolling last-digit strategy for short Deriv Over/Under contracts.

This module deliberately keeps the live rule small and inspectable:

* the selected symbol's quoted last digit is counted from raw ticks;
* reviews occur once per minute;
* Over 6 is considered only when 7/8/9 meet the configured total-share
  threshold and their average per-digit frequency exceeds the 1–6 average
  in both fast and medium windows, with slow-window support;
* after a qualifying review, one digit 0-6 must print before the next tick
  becomes eligible for a one- or two-tick DIGITOVER contract;
* no martingale or candle/AI decision is involved here.

The trading engine requests a live proposal for execution and audit, but no
estimated-edge or guessed-probability check is used as an entry gate.
"""

from __future__ import annotations

import time
import uuid
from collections import Counter, deque
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Deque, Dict, Iterable, List, Optional, Tuple

from src.logger import get_logger

logger = get_logger("digit_strategy")

REVIEW_INTERVAL_SECONDS = 60.0
DEFAULT_WINDOWS = {"fast": 20, "medium": 50, "slow": 200}
DEFAULT_MIN_OVER6_SHARE = 0.31
DEFAULT_LOW_DIGIT_MAX = 6
COMPARISON_LOW_DIGITS = tuple(range(1, 7))
SIGNAL_MAX_AGE_SECONDS = 10.0


class DigitStrategyEngine:
    """A deterministic rolling-frequency strategy for Over 6."""

    def __init__(
        self,
        contract_duration_ticks: int = 1,
        quote_precision: int = 2,
        windows: Optional[Dict[str, int]] = None,
        min_over6_share: float = DEFAULT_MIN_OVER6_SHARE,
        low_digit_max: int = DEFAULT_LOW_DIGIT_MAX,
        review_interval_seconds: float = REVIEW_INTERVAL_SECONDS,
        **_: Any,
    ) -> None:
        self.contract_duration_ticks = int(contract_duration_ticks)
        self.quote_precision = max(0, int(quote_precision))
        self.windows = dict(windows or DEFAULT_WINDOWS)
        self.min_over6_share = float(min_over6_share)
        self.low_digit_max = int(low_digit_max)
        self.review_interval_seconds = float(review_interval_seconds)
        self._max_buffer = max(self.windows.values()) * 5

        self._digits: Deque[int] = deque(maxlen=self._max_buffer)
        self._prices: Deque[float] = deque(maxlen=self._max_buffer)
        self._epochs: Deque[float] = deque(maxlen=self._max_buffer)
        self._current_price = 0.0
        self._last_digit: Optional[int] = None
        self._last_tick_epoch: Optional[float] = None

        self._last_review_bucket: Optional[int] = None
        self._last_review: Optional[Dict[str, Any]] = None
        self._last_evaluation: Optional[Dict[str, Any]] = None
        self._entry_evaluation: Optional[Dict[str, Any]] = None
        self._last_rejection = "warming up"
        self._last_signal_score = 0
        self._last_signal_score_breakdown: Dict[str, Any] = {}
        self._last_entry_mode = "digit-frequency"

        self._armed = False
        self._condition_valid = False
        self._armed_context: Optional[Dict[str, Any]] = None
        self._last_qualifying_context: Optional[Dict[str, Any]] = None
        self._confirmation_seen = False
        self._confirmation_digit: Optional[int] = None
        self._confirmation_epoch: Optional[float] = None
        self._pending_signal: Optional[str] = None
        self._pending_signal_id: Optional[str] = None
        self._pending_signal_time = 0.0
        self._last_consumed_signal_id: Optional[str] = None
        self._trades_in_trend = 0
        self._state_version = 0
        self._state_signature: Optional[Tuple[Any, ...]] = None

    # ------------------------------------------------------------------ digits
    def _digit_from_price(self, price: float) -> int:
        scaled = Decimal(str(price)) * (Decimal(10) ** self.quote_precision)
        integer = int(scaled.to_integral_value(rounding=ROUND_HALF_UP))
        return abs(integer) % 10

    def seed_ticks(self, prices: Iterable[Any], epochs: Optional[Iterable[Any]] = None) -> None:
        epoch_list = list(epochs or [])
        for index, price in enumerate(prices):
            try:
                epoch = float(epoch_list[index]) if index < len(epoch_list) else time.time()
                self.process_tick(float(price), epoch, allow_signal=False)
            except (TypeError, ValueError):
                continue
        self._sync_state_version()

    def process_tick(self, price: float, epoch: Optional[float] = None, allow_signal: bool = True) -> Optional[str]:
        try:
            price = float(price)
        except (TypeError, ValueError):
            return None
        if price <= 0:
            return None
        tick_epoch = float(epoch if epoch is not None else time.time())
        digit = self._digit_from_price(price)
        self._current_price = price
        self._last_digit = digit
        self._last_tick_epoch = tick_epoch
        self._prices.append(price)
        self._epochs.append(tick_epoch)
        self._digits.append(digit)

        if allow_signal and self._armed and self._pending_signal is None:
            if not self._confirmation_seen:
                if digit <= self.low_digit_max:
                    self._confirmation_seen = True
                    self._confirmation_digit = digit
                    self._confirmation_epoch = tick_epoch
                    self._last_rejection = (
                        f"lower-tick confirmation received ({digit}); next tick is eligible"
                    )
            else:
                self._queue_signal(digit, tick_epoch)
        self._sync_state_version()
        return self._pending_signal

    # -------------------------------------------------------------- review rule
    def _window_stats(self, window: int) -> Optional[Dict[str, Any]]:
        if len(self._digits) < window:
            return None
        values = list(self._digits)[-window:]
        counts = Counter(values)
        over_count = sum(counts[d] for d in (7, 8, 9))
        low_count = sum(counts[d] for d in range(0, 7))
        comparison_count = sum(counts[d] for d in COMPARISON_LOW_DIGITS)
        p_over6 = over_count / window
        p_low = low_count / window
        p_1to6 = comparison_count / window
        p_over6_avg = p_over6 / 3.0
        p_1to6_avg = p_1to6 / 6.0
        return {
            "window": window,
            "count": window,
            "counts": {str(d): int(counts[d]) for d in range(10)},
            "over6_count": over_count,
            "low_count": low_count,
            "comparison_count_1to6": comparison_count,
            "p_over6": round(p_over6, 6),
            "p_low": round(p_low, 6),
            "p_1to6": round(p_1to6, 6),
            "p_over6_avg": round(p_over6_avg, 6),
            "p_1to6_avg": round(p_1to6_avg, 6),
            "dominance": round(p_over6 - p_1to6, 6),
            "per_digit_dominance": round(p_over6_avg - p_1to6_avg, 6),
        }

    def _candidate(self, stats: Dict[str, Optional[Dict[str, Any]]]) -> Tuple[bool, str]:
        fast = stats.get("fast")
        medium = stats.get("medium")
        slow = stats.get("slow")
        if not fast or not medium or not slow:
            return False, f"warming up — need {max(self.windows.values())} ticks"
        if fast["p_over6"] < self.min_over6_share:
            return False, f"7–9 share {fast['p_over6']:.1%} below {self.min_over6_share:.1%}"
        if medium["p_over6"] < self.min_over6_share:
            return False, f"medium 7–9 share {medium['p_over6']:.1%} below {self.min_over6_share:.1%}"
        if fast["p_over6_avg"] <= fast["p_1to6_avg"]:
            return False, "average 7–9 digit share does not exceed average 1–6 digit share in the fast window"
        if medium["p_over6_avg"] <= medium["p_1to6_avg"]:
            return False, "average 7–9 digit share does not exceed average 1–6 digit share in the medium window"
        slow_min_share = max(0.30, self.min_over6_share - 0.01)
        if slow["p_over6"] < slow_min_share or slow["p_over6_avg"] <= slow["p_1to6_avg"]:
            return False, f"slow window does not support the rule (needs at least {slow_min_share:.1%} 7–9)"
        return True, "average 7–9 digit frequency dominates average 1–6 frequency across fast and medium windows"

    def review_if_due(self, now: Optional[float] = None) -> Optional[Dict[str, Any]]:
        current = float(now if now is not None else time.time())
        bucket = int(current // self.review_interval_seconds)
        if self._last_review_bucket == bucket:
            return None
        self._last_review_bucket = bucket

        stats = {name: self._window_stats(size) for name, size in self.windows.items()}
        qualifies, reason = self._candidate(stats)
        self._last_signal_score = int(round((stats.get("medium") or {}).get("p_over6", 0.0) * 100))
        self._last_signal_score_breakdown = {
            "fast_over6_pct": round((stats.get("fast") or {}).get("p_over6", 0.0) * 100, 2),
            "medium_over6_pct": round((stats.get("medium") or {}).get("p_over6", 0.0) * 100, 2),
            "p_over6_slow": round((stats.get("slow") or {}).get("p_over6", 0.0) * 100, 2),
            "p_over6_avg_fast": round((stats.get("fast") or {}).get("p_over6_avg", 0.0) * 100, 2),
            "p_over6_avg_medium": round((stats.get("medium") or {}).get("p_over6_avg", 0.0) * 100, 2),
            "p_over6_avg_slow": round((stats.get("slow") or {}).get("p_over6_avg", 0.0) * 100, 2),
            "p_1to6_avg_fast": round((stats.get("fast") or {}).get("p_1to6_avg", 0.0) * 100, 2),
            "p_1to6_avg_medium": round((stats.get("medium") or {}).get("p_1to6_avg", 0.0) * 100, 2),
            "p_1to6_avg_slow": round((stats.get("slow") or {}).get("p_1to6_avg", 0.0) * 100, 2),
            "rule": reason,
        }

        if qualifies:
            self._condition_valid = True
            self._last_qualifying_context = {
                "review_bucket": bucket,
                "review_epoch": current,
                "stats": stats,
            }
            if not self._armed and self._pending_signal is None:
                self._armed = True
                self._confirmation_seen = False
                self._confirmation_digit = None
                self._confirmation_epoch = None
                self._armed_context = dict(self._last_qualifying_context)
                self._last_rejection = "armed — waiting for one lower tick (0–6)"
            elif self._armed:
                self._last_rejection = "still armed — waiting for one lower tick (0–6)"
        else:
            self._condition_valid = False
            self._last_qualifying_context = None
            if self._armed and self._pending_signal is None:
                self._armed = False
                self._confirmation_seen = False
                self._armed_context = None
            self._last_rejection = reason

        record = self._review_record(stats, qualifies, reason, current)
        self._last_review = record
        self._last_evaluation = record
        self._sync_state_version()
        return record

    def _review_record(
        self,
        stats: Dict[str, Optional[Dict[str, Any]]],
        qualifies: bool,
        reason: str,
        timestamp: float,
    ) -> Dict[str, Any]:
        fast = stats.get("fast") or {}
        medium = stats.get("medium") or {}
        slow = stats.get("slow") or {}
        return {
            "signal_id": "",
            "timestamp_utc": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(timestamp)),
            "direction": "OVER6" if qualifies else "-",
            "trend": "DIGIT_OVER6" if qualifies else "-",
            "taken": "FALSE",
            "executed": "FALSE",
            "rejection_reason": "" if qualifies else reason,
            "note": "armed; waiting for one lower tick" if qualifies else "",
            "score": self._last_signal_score,
            "threshold": round(self.min_over6_share * 100, 2),
            "regime": "DIGIT",
            "duration_min": round(self.contract_duration_ticks / 60.0, 4),
            "strategy_mode": "DIGIT_OVER_6",
            "barrier": "6",
            "duration_unit": "t",
            "digit_precision": self.quote_precision,
            "last_digit": self._last_digit if self._last_digit is not None else "",
            "digit_counts_fast": fast.get("counts", {}),
            "digit_counts_medium": medium.get("counts", {}),
            "digit_counts_slow": slow.get("counts", {}),
            "comparison_group": "1-6",
            "p_over6_fast": fast.get("p_over6", ""),
            "p_over6_medium": medium.get("p_over6", ""),
            "p_over6_slow": slow.get("p_over6", ""),
            "p_low_fast": fast.get("p_1to6", ""),
            "p_low_medium": medium.get("p_1to6", ""),
            "p_low_slow": slow.get("p_1to6", ""),
            "p_over6_avg_fast": fast.get("p_over6_avg", ""),
            "p_over6_avg_medium": medium.get("p_over6_avg", ""),
            "p_over6_avg_slow": slow.get("p_over6_avg", ""),
            "p_1to6_avg_fast": fast.get("p_1to6_avg", ""),
            "p_1to6_avg_medium": medium.get("p_1to6_avg", ""),
            "p_1to6_avg_slow": slow.get("p_1to6_avg", ""),
            "dominance_fast": fast.get("dominance", ""),
            "dominance_medium": medium.get("dominance", ""),
            "dominance_slow": slow.get("dominance", ""),
            "per_digit_dominance_fast": fast.get("per_digit_dominance", ""),
            "per_digit_dominance_medium": medium.get("per_digit_dominance", ""),
            "per_digit_dominance_slow": slow.get("per_digit_dominance", ""),
            "lower_confirmation_digit": self._confirmation_digit if self._confirmation_digit is not None else "",
            "entry_digit": "",
            "quote_ask": "",
            "quote_payout": "",
            "quote_break_even": "",
        }

    def _queue_signal(self, entry_digit: int, epoch: float) -> None:
        if not self._armed or not self._confirmation_seen:
            return
        self._pending_signal = "OVER6"
        self._pending_signal_id = uuid.uuid4().hex[:10]
        self._pending_signal_time = time.time()
        base = dict(self._armed_context or {})
        stats = base.get("stats") or {}
        record = self._review_record(stats, True, "", epoch)
        record.update({
            "signal_id": self._pending_signal_id,
            "timestamp_utc": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(epoch)),
            "taken": "TRUE",
            "note": "one lower tick confirmed; entered on the next tick",
            "rejection_reason": "",
            "lower_confirmation_digit": self._confirmation_digit,
            "entry_digit": entry_digit,
        })
        self._entry_evaluation = record
        self._last_rejection = "signal queued — Over 6 entry"
        logger.info(
            "DIGIT OVER6 signal | id=%s | entry_digit=%d | lower_confirmation=%s",
            self._pending_signal_id,
            entry_digit,
            self._confirmation_digit,
        )
        self._sync_state_version()

    # ---------------------------------------------------------------- signals
    def consume_signal(self) -> Optional[str]:
        signal = self._pending_signal
        self._last_consumed_signal_id = self._pending_signal_id
        self._pending_signal = None
        self._pending_signal_id = None
        if signal is None:
            return None
        if time.time() - self._pending_signal_time > SIGNAL_MAX_AGE_SECONDS:
            self._armed = False
            self._confirmation_seen = False
            self._last_rejection = "stale digit signal discarded"
            self._sync_state_version()
            return None
        self._sync_state_version()
        return signal

    def get_entry_evaluation(self) -> Optional[Dict[str, Any]]:
        record = self._entry_evaluation
        self._entry_evaluation = None
        return record

    @property
    def last_consumed_signal_id(self) -> Optional[str]:
        return self._last_consumed_signal_id

    @property
    def express_bonus(self) -> float:
        return 0.0

    def get_current_price(self) -> float:
        return self._current_price

    def on_trade_executed(self) -> None:
        """Clear the consumed signal while the current contract is in flight."""
        self._trades_in_trend += 1
        self._armed = False
        self._armed_context = None
        self._confirmation_seen = False
        self._confirmation_digit = None
        self._confirmation_epoch = None
        self._pending_signal = None
        self._pending_signal_id = None
        self._sync_state_version()

    def on_trade_finished(self, allow_rearm: bool = True) -> None:
        """Re-arm after settlement only when normal operation is still allowed."""
        if allow_rearm and self._condition_valid and self._pending_signal is None:
            self._armed = True
            self._armed_context = dict(self._last_qualifying_context or {})
            self._confirmation_seen = False
            self._confirmation_digit = None
            self._confirmation_epoch = None
            self._last_rejection = "trade finished — condition still valid; waiting for one lower tick"
        else:
            self._armed = False
            self._armed_context = None
            self._confirmation_seen = False
            self._confirmation_digit = None
            self._confirmation_epoch = None
            if not allow_rearm and self._condition_valid:
                self._last_rejection = "trade finished — re-entry disabled because stopping was requested"
        self._sync_state_version()

    def on_signal_skipped(self) -> None:
        self._armed = False
        self._armed_context = None
        self._confirmation_seen = False
        self._confirmation_digit = None
        self._confirmation_epoch = None
        self._pending_signal = None
        self._pending_signal_id = None
        self._last_rejection = "signal skipped by execution gate"
        self._sync_state_version()

    def get_last_evaluation(self) -> Optional[Dict[str, Any]]:
        record = self._last_evaluation
        self._last_evaluation = None
        return record

    def get_state(self) -> Dict[str, Any]:
        stats = {name: self._window_stats(size) for name, size in self.windows.items()}
        counts = Counter(self._digits)
        return {
            "trend_direction": "UP" if self._armed else None,
            "trend_tick_count": len(self._digits),
            "trend_kind": "digit-frequency",
            "trend_age": 0,
            "trades_in_trend": self._trades_in_trend,
            "in_cooldown": False,
            "pattern_stage": "SIGNAL" if self._pending_signal else ("PULLBACK" if self._confirmation_seen else ("TREND" if self._armed else "IDLE")),
            "mtf_bias": "UP" if self._armed else None,
            "mtf_agreement": 2 if self._armed else 0,
            "mtf_tf_biases": {name: ("UP" if (stats.get(name) or {}).get("p_over6_avg", 0) > (stats.get(name) or {}).get("p_1to6_avg", 1) else "FLAT") for name in self.windows},
            "micro_bias": "UP" if self._last_digit is not None and self._last_digit >= 7 else "DOWN",
            "last_entry_mode": self._last_entry_mode,
            "last_signal_score": self._last_signal_score,
            "last_signal_score_breakdown": dict(self._last_signal_score_breakdown),
            "entry_adx": None,
            "entry_rsi": None,
            "entry_macd_hist": None,
            "strategy_mode": "DIGIT_OVER_6",
            "digit_barrier": 6,
            "digit_precision": self.quote_precision,
            "last_digit": self._last_digit,
            "digit_counts": {str(d): int(counts[d]) for d in range(10)},
            "digit_windows": stats,
            "digit_armed": self._armed, "digit_condition_valid": self._condition_valid, "digit_lower_confirmed": self._confirmation_seen,
            "digit_lower_confirmation": self._confirmation_digit,
            "digit_last_rejection": self._last_rejection,
            "digit_contract_duration_ticks": self.contract_duration_ticks,
        }

    @property
    def state_version(self) -> int:
        return self._state_version

    def update_candles(self, *_: Any, **__: Any) -> None:
        return None

    def reset(self) -> None:
        self.__init__(
            contract_duration_ticks=self.contract_duration_ticks,
            quote_precision=self.quote_precision,
            windows=self.windows,
            min_over6_share=self.min_over6_share,
            low_digit_max=self.low_digit_max,
            review_interval_seconds=self.review_interval_seconds,
        )

    # --------------------------------------------------------------- internals
    def _sync_state_version(self) -> None:
        stats = tuple(
            round((self._window_stats(size) or {}).get("p_over6", -1.0), 4)
            for size in self.windows.values()
        )
        signature = (
            self._armed,
            self._confirmation_seen,
            self._pending_signal,
            self._last_digit,
            stats,
            self._last_rejection,
        )
        if signature != self._state_signature:
            self._state_signature = signature
            self._state_version += 1
