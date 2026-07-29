"""
src/strategy.py
MomentumMaster TF — candle-trend engine ("Meridian").

Trades only on a clear higher-timeframe trend:
- 30m and 1h must agree on direction and be non-flat (hard requirement).
- A closed 15m candle provides the trigger (no mid-candle repainting).
- 5m adds an alignment bonus.
- Adaptive ATR-normalised gates: EMA separation, volatility band, extension guard.

Signals are produced only when a new 15m candle closes, so the engine stays idle
(and calm) between candles. Public interface used by TradingEngine:
    update_candles / consume_signal / process_tick / get_state /
    on_trade_executed / on_signal_skipped / get_current_price / reset / state_version
"""

import time
from collections import deque
from typing import Any, Dict, List, Optional, Tuple

from config import (
    CANDLE_GRANULARITIES,
    ENTRY_SCORE_THRESHOLD,
    ENTRY_TIMEFRAME,
    MTF_MIN_AGREEMENT,
    TREND_TIMEFRAMES,
)
from src.logger import get_logger

logger = get_logger("strategy")

SCORE_MAX = 14
SIGNAL_MAX_AGE_SECONDS = 240


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _closed_candles(candles, granularity, now, delay_seconds=30.0):
    closed = []
    for candle in candles:
        epoch = _safe_float(candle.get("epoch"), 0.0)
        if epoch <= 0:
            continue
        if epoch + granularity <= now - delay_seconds:
            closed.append(candle)
    return closed


def _ema_series(values: List[float], period: int) -> List[float]:
    if not values:
        return []
    alpha = 2.0 / (float(period) + 1.0)
    ema = [values[0]]
    for value in values[1:]:
        ema.append(alpha * value + (1.0 - alpha) * ema[-1])
    return ema


def _candle_metrics(candle) -> Dict[str, float]:
    open_price = _safe_float(candle.get("open"))
    high = _safe_float(candle.get("high"))
    low = _safe_float(candle.get("low"))
    close = _safe_float(candle.get("close"))
    candle_range = max(high - low, 1e-12)
    body = abs(close - open_price)
    return {
        "open": open_price,
        "high": high,
        "low": low,
        "close": close,
        "range": candle_range,
        "body_ratio": body / candle_range,
        "close_position": (close - low) / candle_range,
    }


def _percentile_rank(values, x: float) -> float:
    if not values:
        return 0.5
    count = 0
    for v in values:
        if v <= x:
            count += 1
    return count / float(len(values))


class _TimeframeTracker:
    """O(1)-per-candle indicator state for one timeframe."""

    __slots__ = (
        "granularity", "closes", "last_epoch", "last_candle", "prev_candle",
        "ema_fast", "ema_slow", "ema_fast_trail", "close", "prev_close",
        "slope", "tr_values", "atr", "bias",
        "_fast_alpha", "_slow_alpha", "_atr_period",
    )

    def __init__(self, granularity, lookback=80, fast=20, slow=50, atr_period=14):
        self.granularity = granularity
        self.closes: deque = deque(maxlen=lookback)
        self.last_epoch: Optional[int] = None
        self.last_candle = None
        self.prev_candle = None
        self.ema_fast: Optional[float] = None
        self.ema_slow: Optional[float] = None
        self.ema_fast_trail: deque = deque(maxlen=6)
        self.close = 0.0
        self.prev_close = 0.0
        self.slope = 0.0
        self.tr_values: deque = deque(maxlen=atr_period * 3)
        self.atr: Optional[float] = None
        self.bias = "FLAT"
        self._fast_alpha = 2.0 / (fast + 1)
        self._slow_alpha = 2.0 / (slow + 1)
        self._atr_period = atr_period

    def absorb(self, closed) -> bool:
        if not closed:
            return False
        newest_epoch = int(_safe_float(closed[-1].get("epoch")))
        if self.last_epoch is not None and newest_epoch <= self.last_epoch:
            return False
        if self.last_epoch is None:
            self._resync(closed)
            return True
        if newest_epoch != self.last_epoch + self.granularity:
            self._resync(closed)
            return True
        fresh = [c for c in closed if int(_safe_float(c.get("epoch"))) > self.last_epoch]
        for candle in fresh:
            self._step(candle)
        return bool(fresh)

    def reset(self) -> None:
        self.closes.clear()
        self.last_epoch = None
        self.last_candle = None
        self.prev_candle = None
        self.ema_fast = None
        self.ema_slow = None
        self.ema_fast_trail.clear()
        self.close = 0.0
        self.prev_close = 0.0
        self.slope = 0.0
        self.tr_values.clear()
        self.atr = None
        self.bias = "FLAT"

    def _step(self, candle) -> None:
        close = _safe_float(candle.get("close"))
        high = _safe_float(candle.get("high"))
        low = _safe_float(candle.get("low"))
        self.prev_candle = self.last_candle
        self.last_candle = candle
        if self.close > 0:
            self.prev_close = self.close
        self.closes.append(close)
        if self.ema_fast is None:
            self.ema_fast = close
            self.ema_slow = close
        else:
            self.ema_fast += self._fast_alpha * (close - self.ema_fast)
            self.ema_slow += self._slow_alpha * (close - self.ema_slow)
        self.ema_fast_trail.append(self.ema_fast)
        self.slope = self.ema_fast - self.ema_fast_trail[0]
        self.close = close
        if self.prev_close > 0:
            tr = max(high - low, abs(high - self.prev_close), abs(low - self.prev_close))
            self.tr_values.append(tr)
            window = min(self._atr_period, len(self.tr_values))
            if window:
                recent = list(self.tr_values)[-window:]
                self.atr = sum(recent) / window
        self.last_epoch = int(_safe_float(candle.get("epoch")))
        self._refresh_bias()

    def _resync(self, closed) -> None:
        self.closes.clear()
        self.tr_values.clear()
        self.ema_fast_trail.clear()
        closes: List[float] = []
        prev_close = 0.0
        for candle in closed:
            close = _safe_float(candle.get("close"))
            high = _safe_float(candle.get("high"))
            low = _safe_float(candle.get("low"))
            closes.append(close)
            self.closes.append(close)
            if prev_close > 0:
                self.tr_values.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
            prev_close = close
        if len(closes) >= 2:
            fast_series = _ema_series(closes, 20)
            slow_series = _ema_series(closes, 50)
            self.ema_fast = fast_series[-1]
            self.ema_slow = slow_series[-1]
            for value in fast_series[-6:]:
                self.ema_fast_trail.append(value)
            self.slope = fast_series[-1] - fast_series[-6] if len(fast_series) >= 6 else 0.0
        else:
            self.ema_fast = closes[-1] if closes else None
            self.ema_slow = closes[-1] if closes else None
            self.slope = 0.0
        window = min(self._atr_period, len(self.tr_values))
        if window:
            recent = list(self.tr_values)[-window:]
            self.atr = sum(recent) / window
        else:
            self.atr = None
        self.close = closes[-1] if closes else 0.0
        self.prev_close = closes[-2] if len(closes) >= 2 else self.close
        self.last_candle = closed[-1] if closed else None
        self.prev_candle = closed[-2] if len(closed) >= 2 else None
        self.last_epoch = int(_safe_float(closed[-1].get("epoch"))) if closed else None
        self._refresh_bias()

    def _refresh_bias(self) -> None:
        if self.ema_fast is None or self.ema_slow is None:
            self.bias = "FLAT"
            return
        if self.close > self.ema_fast > self.ema_slow and self.slope > 0:
            self.bias = "UP"
        elif self.close < self.ema_fast < self.ema_slow and self.slope < 0:
            self.bias = "DOWN"
        else:
            self.bias = "FLAT"


class StrategyEngine:
    """Candle-trend strategy: trade only on a clear 30m+1h trend with a 15m trigger."""

    def __init__(
        self,
        velocity_threshold: float = 0.55,
        burst_threshold: float = 0.72,
        mtf_min_agreement: int = MTF_MIN_AGREEMENT,
        entry_score_threshold: int = ENTRY_SCORE_THRESHOLD,
        **_: Any,
    ):
        del velocity_threshold, burst_threshold, mtf_min_agreement
        self._entry_score_threshold = entry_score_threshold

        self._trackers: Dict[str, _TimeframeTracker] = {
            tf: _TimeframeTracker(gran) for tf, gran in CANDLE_GRANULARITIES.items()
        }
        self._entry_tf = ENTRY_TIMEFRAME
        self._trend_tfs = list(TREND_TIMEFRAMES)

        self._atr_history: deque = deque(maxlen=240)
        self._last_entry_epoch_evaluated: Optional[int] = None

        self._trend_direction: Optional[str] = None
        self._mtf_bias: Optional[str] = None
        self._mtf_agreement = 0
        self._mtf_tf_biases: Dict[str, str] = {}

        self._pattern_stage = "IDLE"
        self._last_entry_mode: Optional[str] = None
        self._last_signal_score = 0
        self._last_signal_score_breakdown: Dict[str, int] = {}

        self._pending_signal: Optional[str] = None
        self._pending_signal_time = 0.0
        self._current_price = 0.0

        self._trades_in_trend = 0
        self._in_cooldown = False

        self._state_version = 0
        self._state_signature: Optional[Tuple[Any, ...]] = None
        self._mtf_sig: Tuple[Any, ...] = ()
        self._breakdown_sig: Tuple[Any, ...] = ()

        logger.info(
            "Meridian candle-trend initialised | entry_threshold=%d/%d | entry_tf=%s | trend_tfs=%s",
            self._entry_score_threshold, SCORE_MAX, self._entry_tf, self._trend_tfs,
        )

    # --- public interface ---
    @property
    def state_version(self) -> int:
        return self._state_version

    def process_tick(self, price: float) -> Optional[str]:
        if price > 0:
            self._current_price = price
        return None

    def consume_signal(self) -> Optional[str]:
        signal = self._pending_signal
        self._pending_signal = None
        if signal is None:
            return None
        if time.time() - self._pending_signal_time > SIGNAL_MAX_AGE_SECONDS:
            logger.info("Discarded stale %s signal (older than %ds).", signal, SIGNAL_MAX_AGE_SECONDS)
            self._sync_state_version()
            return None
        self._sync_state_version()
        return signal

    def get_current_price(self) -> float:
        return self._current_price

    def on_trade_executed(self) -> None:
        self._trades_in_trend += 1
        self._pending_signal = None
        self._pattern_stage = "IDLE"
        self._sync_state_version()

    def on_signal_skipped(self) -> None:
        self._pending_signal = None
        self._pattern_stage = "TREND" if self._trend_direction else "IDLE"
        self._sync_state_version()

    def update_mtf_bias(self, bias, agreement=0, tf_biases=None) -> None:
        # Retained for interface compatibility; biases are computed from candles.
        if tf_biases is not None:
            self._mtf_tf_biases = tf_biases
            self._mtf_sig = tuple(sorted(tf_biases.items()))
        self._mtf_bias = bias
        self._mtf_agreement = agreement
        self._sync_state_version()

    def get_state(self) -> Dict[str, Any]:
        return {
            "trend_direction": self._trend_direction,
            "trend_tick_count": 0,
            "trend_kind": "candle",
            "trend_age": 0,
            "trades_in_trend": self._trades_in_trend,
            "in_cooldown": self._in_cooldown,
            "pattern_stage": self._pattern_stage,
            "mtf_bias": self._mtf_bias,
            "mtf_agreement": self._mtf_agreement,
            "mtf_tf_biases": dict(self._mtf_tf_biases),
            "micro_bias": None,
            "last_signal_score": self._last_signal_score,
            "last_signal_score_breakdown": dict(self._last_signal_score_breakdown),
            "last_entry_mode": self._last_entry_mode,
        }

    def reset(self) -> None:
        for tracker in self._trackers.values():
            tracker.reset()
        self._atr_history.clear()
        self._last_entry_epoch_evaluated = None
        self._trend_direction = None
        self._mtf_bias = None
        self._mtf_agreement = 0
        self._mtf_tf_biases = {}
        self._pattern_stage = "IDLE"
        self._last_entry_mode = None
        self._last_signal_score = 0
        self._last_signal_score_breakdown = {}
        self._pending_signal = None
        self._pending_signal_time = 0.0
        self._current_price = 0.0
        self._trades_in_trend = 0
        self._in_cooldown = False
        self._mtf_sig = ()
        self._breakdown_sig = ()
        self._state_signature = None
        self._state_version += 1

    def update_candles(self, candles_by_tf, now: float) -> None:
        any_new = False
        for tf, tracker in self._trackers.items():
            raw = candles_by_tf.get(tf) or []
            closed = _closed_candles(raw, tracker.granularity, now)
            if tracker.absorb(closed):
                any_new = True
        if not any_new:
            return  # common case: no new closed candle -> nothing to recompute

        self._update_trend_bias()

        entry_tracker = self._trackers[self._entry_tf]
        if entry_tracker.atr is not None:
            self._atr_history.append(entry_tracker.atr)
        if entry_tracker.close > 0:
            self._current_price = entry_tracker.close

        if (
            entry_tracker.last_epoch is not None
            and entry_tracker.last_epoch != self._last_entry_epoch_evaluated
        ):
            self._last_entry_epoch_evaluated = entry_tracker.last_epoch
            self._evaluate_entry_candle(entry_tracker)

        self._sync_state_version()

    def _update_trend_bias(self) -> None:
        self._mtf_tf_biases = {tf: t.bias for tf, t in self._trackers.items()}
        votes = [self._mtf_tf_biases.get(tf, "FLAT") for tf in self._trend_tfs]
        up_votes = votes.count("UP")
        down_votes = votes.count("DOWN")
        if up_votes == len(votes):
            self._trend_direction, self._mtf_bias, self._mtf_agreement = "UP", "UP", up_votes
        elif down_votes == len(votes):
            self._trend_direction, self._mtf_bias, self._mtf_agreement = "DOWN", "DOWN", down_votes
        else:
            self._trend_direction = None
            self._mtf_agreement = max(up_votes, down_votes)
            self._mtf_bias = "UP" if up_votes > down_votes else ("DOWN" if down_votes > up_votes else None)
        if self._pending_signal and self._trend_direction is None:
            self._pending_signal = None
        if self._pattern_stage != "SIGNAL":
            self._pattern_stage = "TREND" if self._trend_direction else "IDLE"
        self._mtf_sig = tuple(sorted(self._mtf_tf_biases.items()))

    def _reject(self, reason: str) -> None:
        self._pattern_stage = "TREND" if self._trend_direction else "IDLE"
        logger.debug("Setup rejected: %s", reason)

    def _evaluate_entry_candle(self, tracker: _TimeframeTracker) -> None:
        self._last_signal_score = 0
        self._last_signal_score_breakdown = {}

        if self._trend_direction not in ("UP", "DOWN"):
            self._pattern_stage = "IDLE"
            return

        candle = tracker.last_candle
        prev = tracker.prev_candle
        atr = tracker.atr
        if candle is None or prev is None or atr is None or atr <= 0:
            self._pattern_stage = "TREND"
            return
        if tracker.ema_fast is None or tracker.ema_slow is None:
            self._pattern_stage = "TREND"
            return

        signal = "BUY" if self._trend_direction == "UP" else "SELL"
        metrics = _candle_metrics(candle)
        close = metrics["close"]
        body_ratio = metrics["body_ratio"]

        # Gate 1: trend strength (flat/entangled EMAs = no trade).
        separation = abs(tracker.ema_fast - tracker.ema_slow)
        if separation < 0.30 * atr:
            self._reject("EMAs too flat - no trend strength")
            return

        # Gate 2: adaptive volatility band.
        if len(self._atr_history) >= 24:
            vol_rank = _percentile_rank(self._atr_history, atr)
            if vol_rank < 0.10:
                self._reject("volatility too dead")
                return
            if vol_rank > 0.95:
                self._reject("volatility spike - unstable conditions")
                return

        # Gate 3: trigger + EMA + extension.
        if signal == "BUY":
            trigger = close > _safe_float(prev.get("high")) and close > metrics["open"]
            ema_ok = close > tracker.ema_fast
            exhausted = close > tracker.ema_fast + 2.75 * atr
            directional_close_position = metrics["close_position"]
        else:
            trigger = close < _safe_float(prev.get("low")) and close < metrics["open"]
            ema_ok = close < tracker.ema_fast
            exhausted = close < tracker.ema_fast - 2.75 * atr
            directional_close_position = 1.0 - metrics["close_position"]

        if not trigger:
            self._reject("no trigger break of prior candle")
            return
        if not ema_ok:
            self._reject("close not beyond fast EMA")
            return
        if exhausted:
            self._reject("move exhausted - too far from EMA")
            return

        # Scoring (max 14).
        trend_score = 5  # 30m + 1h agreement is mandatory to reach here
        if body_ratio >= 0.65:
            trigger_score = 3
        elif body_ratio >= 0.50:
            trigger_score = 2
        elif body_ratio >= 0.35:
            trigger_score = 1
        else:
            trigger_score = 0
        if directional_close_position >= 0.72:
            momentum_score = 3
        elif directional_close_position >= 0.60:
            momentum_score = 2
        elif directional_close_position >= 0.50:
            momentum_score = 1
        else:
            momentum_score = 0
        atr_ratio = atr / close if close > 0 else 0.0
        if 0.00005 <= atr_ratio <= 0.020:
            volatility_score = 2
        elif atr_ratio > 0:
            volatility_score = 1
        else:
            volatility_score = 0
        alignment_score = 1 if self._mtf_tf_biases.get("5m") == self._trend_direction else 0

        score = trend_score + trigger_score + momentum_score + volatility_score + alignment_score
        breakdown = {
            "trend": trend_score,
            "trigger": trigger_score,
            "momentum": momentum_score,
            "volatility": volatility_score,
            "alignment": alignment_score,
        }
        self._last_signal_score = score
        self._last_signal_score_breakdown = breakdown
        self._breakdown_sig = tuple(sorted(breakdown.items()))

        if score >= self._entry_score_threshold:
            self._pending_signal = signal
            self._pending_signal_time = time.time()
            self._pattern_stage = "SIGNAL"
            self._last_entry_mode = "candle-trend"
            logger.info(
                "CANDLE %s setup | Score %d/%d | %s | close=%.5f | atr=%.5f",
                signal, score, SCORE_MAX, breakdown, close, atr,
            )
        else:
            self._reject(f"score {score}/{SCORE_MAX} below threshold {self._entry_score_threshold}")

    def _sync_state_version(self) -> None:
        signature = (
            self._trend_direction,
            self._pattern_stage,
            self._mtf_bias,
            self._mtf_agreement,
            self._last_entry_mode,
            self._last_signal_score,
            self._pending_signal,
            self._mtf_sig,
            self._breakdown_sig,
        )
        if signature != self._state_signature:
            self._state_signature = signature
            self._state_version += 1


class MTFAnalyzer:
    """Retained for compatibility; the candle engine computes biases internally."""

    def __init__(self, min_agreement: int = MTF_MIN_AGREEMENT):
        self._min_agreement = min_agreement

    def analyze(self, candles_by_tf: Dict[str, List[Dict]]) -> Optional[str]:
        return self.analyze_with_strength(candles_by_tf)[0]

    def analyze_with_strength(self, candles_by_tf) -> Tuple[Optional[str], int, Dict[str, str]]:
        tf_biases: Dict[str, str] = {}
        for label, candles in candles_by_tf.items():
            tf_biases[label] = "FLAT"
        return None, 0, tf_biases