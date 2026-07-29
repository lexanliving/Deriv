"""
src/strategy.py
MomentumMaster TF — multi-factor confluence engine ("Meridian").
Also exposes a full per-candle evaluation record (taken or rejected) so the
decision journal can log why every setup was or wasn't traded.
"""
import time
import uuid
from collections import deque
from typing import Any, Dict, List, Optional, Tuple

from config import (
    ADX_MIN_TREND, ADX_PERIOD, CANDLE_GRANULARITIES, DIVERGENCE_LOOKBACK,
    ENTRY_SCORE_THRESHOLD, ENTRY_TIMEFRAME, MACD_FAST, MACD_SIGNAL, MACD_SLOW,
    MTF_MIN_AGREEMENT, RSI_PERIOD, TREND_TIMEFRAMES,
)
from src.logger import get_logger

logger = get_logger("strategy")

SCORE_MAX = 25
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


def _candle_metrics(candle) -> Dict[str, float]:
    open_price = _safe_float(candle.get("open"))
    high = _safe_float(candle.get("high"))
    low = _safe_float(candle.get("low"))
    close = _safe_float(candle.get("close"))
    candle_range = max(high - low, 1e-12)
    body = abs(close - open_price)
    upper_wick = high - max(close, open_price)
    lower_wick = min(close, open_price) - low
    return {
        "open": open_price, "high": high, "low": low, "close": close,
        "range": candle_range,
        "body_ratio": body / candle_range,
        "close_position": (close - low) / candle_range,
        "upper_wick_ratio": upper_wick / candle_range,
        "lower_wick_ratio": lower_wick / candle_range,
    }


def _percentile_rank(values, x: float) -> float:
    if not values:
        return 0.5
    count = 0
    for v in values:
        if v <= x:
            count += 1
    return count / float(len(values))


def _candlestick_pattern_score(candle, prev, direction: str) -> int:
    if candle is None or prev is None:
        return 0
    m = _candle_metrics(candle)
    p = _candle_metrics(prev)
    bullish_engulf = (m["close"] > m["open"] and p["close"] < p["open"]
                      and m["close"] >= p["open"] and m["open"] <= p["close"])
    bearish_engulf = (m["close"] < m["open"] and p["close"] > p["open"]
                      and m["close"] <= p["open"] and m["open"] >= p["close"])
    bull_pin = m["lower_wick_ratio"] >= 0.45 and m["body_ratio"] <= 0.40 and m["close_position"] >= 0.55
    bear_pin = m["upper_wick_ratio"] >= 0.45 and m["body_ratio"] <= 0.40 and m["close_position"] <= 0.45
    if direction == "BUY":
        return 2 if bullish_engulf else (1 if bull_pin else 0)
    return 2 if bearish_engulf else (1 if bear_pin else 0)


class _TimeframeTracker:
    __slots__ = (
        "granularity", "closes", "last_epoch", "last_candle", "prev_candle",
        "ema_fast", "ema_slow", "ema_fast_trail", "close", "prev_close",
        "slope", "tr_values", "atr", "bias",
        "rsi", "rsi_history", "_avg_gain", "_avg_loss",
        "ema12", "ema26", "macd_hist", "macd_hist_trail", "_macd_signal",
        "_tr_smooth", "_dm_plus_smooth", "_dm_minus_smooth",
        "di_plus", "di_minus", "adx",
        "recent_highs", "recent_lows",
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
        self.rsi: Optional[float] = None
        self.rsi_history: deque = deque(maxlen=max(30, DIVERGENCE_LOOKBACK * 4))
        self._avg_gain: Optional[float] = None
        self._avg_loss: Optional[float] = None
        self.ema12: Optional[float] = None
        self.ema26: Optional[float] = None
        self.macd_hist: float = 0.0
        self.macd_hist_trail: deque = deque(maxlen=3)
        self._macd_signal: Optional[float] = None
        self._tr_smooth: Optional[float] = None
        self._dm_plus_smooth: Optional[float] = None
        self._dm_minus_smooth: Optional[float] = None
        self.di_plus: float = 0.0
        self.di_minus: float = 0.0
        self.adx: Optional[float] = None
        self.recent_highs: deque = deque(maxlen=6)
        self.recent_lows: deque = deque(maxlen=6)
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
            self._advance(candle)
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
        self.rsi = None
        self.rsi_history.clear()
        self._avg_gain = None
        self._avg_loss = None
        self.ema12 = None
        self.ema26 = None
        self.macd_hist = 0.0
        self.macd_hist_trail.clear()
        self._macd_signal = None
        self._tr_smooth = None
        self._dm_plus_smooth = None
        self._dm_minus_smooth = None
        self.di_plus = 0.0
        self.di_minus = 0.0
        self.adx = None
        self.recent_highs.clear()
        self.recent_lows.clear()

    def _advance(self, candle) -> None:
        close = _safe_float(candle.get("close"))
        high = _safe_float(candle.get("high"))
        low = _safe_float(candle.get("low"))
        prior_candle = self.last_candle
        self.prev_candle = prior_candle
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

        tr = None
        if self.prev_close > 0:
            tr = max(high - low, abs(high - self.prev_close), abs(low - self.prev_close))
            self.tr_values.append(tr)
            window = min(self._atr_period, len(self.tr_values))
            if window:
                self.atr = sum(list(self.tr_values)[-window:]) / window

        if self.prev_close > 0:
            gain = max(close - self.prev_close, 0.0)
            loss = max(self.prev_close - close, 0.0)
            if self._avg_gain is None:
                self._avg_gain = gain
                self._avg_loss = loss
            else:
                self._avg_gain += (gain - self._avg_gain) / RSI_PERIOD
                self._avg_loss += (loss - self._avg_loss) / RSI_PERIOD
            if self._avg_loss <= 1e-12:
                self.rsi = 100.0
            else:
                self.rsi = 100.0 - (100.0 / (1.0 + self._avg_gain / self._avg_loss))
            self.rsi_history.append(self.rsi)

        if self.ema12 is None:
            self.ema12 = close
            self.ema26 = close
            self._macd_signal = 0.0
            self.macd_hist = 0.0
        else:
            self.ema12 += (2.0 / (MACD_FAST + 1)) * (close - self.ema12)
            self.ema26 += (2.0 / (MACD_SLOW + 1)) * (close - self.ema26)
            macd_line = self.ema12 - self.ema26
            if self._macd_signal is None:
                self._macd_signal = macd_line
            else:
                self._macd_signal += (2.0 / (MACD_SIGNAL + 1)) * (macd_line - self._macd_signal)
            self.macd_hist = macd_line - self._macd_signal
        self.macd_hist_trail.append(self.macd_hist)

        if prior_candle is not None and tr is not None:
            prior_high = _safe_float(prior_candle.get("high"))
            prior_low = _safe_float(prior_candle.get("low"))
            up_move = high - prior_high
            down_move = prior_low - low
            dm_plus = up_move if (up_move > down_move and up_move > 0) else 0.0
            dm_minus = down_move if (down_move > up_move and down_move > 0) else 0.0
            if self._tr_smooth is None:
                self._tr_smooth = tr
                self._dm_plus_smooth = dm_plus
                self._dm_minus_smooth = dm_minus
            else:
                self._tr_smooth += (tr - self._tr_smooth) / ADX_PERIOD
                self._dm_plus_smooth += (dm_plus - self._dm_plus_smooth) / ADX_PERIOD
                self._dm_minus_smooth += (dm_minus - self._dm_minus_smooth) / ADX_PERIOD
            if self._tr_smooth > 1e-12:
                self.di_plus = 100.0 * self._dm_plus_smooth / self._tr_smooth
                self.di_minus = 100.0 * self._dm_minus_smooth / self._tr_smooth
            else:
                self.di_plus = 0.0
                self.di_minus = 0.0
            di_sum = self.di_plus + self.di_minus
            dx = 100.0 * abs(self.di_plus - self.di_minus) / di_sum if di_sum > 1e-12 else 0.0
            self.adx = dx if self.adx is None else self.adx + (dx - self.adx) / ADX_PERIOD

        self.recent_highs.append(high)
        self.recent_lows.append(low)
        self.close = close
        self.last_epoch = int(_safe_float(candle.get("epoch")))
        self._refresh_bias()

    def _resync(self, closed) -> None:
        self.reset()
        for candle in closed:
            self._advance(candle)

    def _refresh_bias(self) -> None:
        if self.ema_fast is None or self.ema_slow is None or self.adx is None:
            self.bias = "FLAT"
            return
        strong_trend = self.adx >= ADX_MIN_TREND
        if (self.close > self.ema_fast > self.ema_slow and self.slope > 0
                and strong_trend and self.di_plus > self.di_minus):
            self.bias = "UP"
        elif (self.close < self.ema_fast < self.ema_slow and self.slope < 0
                and strong_trend and self.di_minus > self.di_plus):
            self.bias = "DOWN"
        else:
            self.bias = "FLAT"

    def structure_score(self, direction: str) -> int:
        if direction == "BUY":
            lows = list(self.recent_lows)
            if len(lows) >= 3 and lows[-1] > lows[-3]:
                return 2
            if len(lows) >= 2 and lows[-1] >= lows[-2]:
                return 1
            return 0
        highs = list(self.recent_highs)
        if len(highs) >= 3 and highs[-1] < highs[-3]:
            return 2
        if len(highs) >= 2 and highs[-1] <= highs[-2]:
            return 1
        return 0

    def divergence_against(self, direction: str) -> bool:
        n = DIVERGENCE_LOOKBACK
        closes = list(self.closes)
        rsis = list(self.rsi_history)
        if len(closes) <= n or len(rsis) <= n:
            return False
        price_now, price_then = closes[-1], closes[-1 - n]
        rsi_now, rsi_then = rsis[-1], rsis[-1 - n]
        if direction == "BUY":
            return price_now > price_then and rsi_now < rsi_then - 1.0
        return price_now < price_then and rsi_now > rsi_then + 1.0


class StrategyEngine:
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
        self._pending_signal_id: Optional[str] = None
        self._last_consumed_signal_id: Optional[str] = None
        self._last_rejection: Optional[str] = None
        self._last_evaluation: Optional[Dict[str, Any]] = None
        self._current_price = 0.0
        self._trades_in_trend = 0
        self._in_cooldown = False
        self._state_version = 0
        self._state_signature: Optional[Tuple[Any, ...]] = None
        self._mtf_sig: Tuple[Any, ...] = ()
        self._breakdown_sig: Tuple[Any, ...] = ()
        logger.info(
            "Meridian confluence engine initialised | entry_threshold=%d/%d | entry_tf=%s | trend_tfs=%s",
            self._entry_score_threshold, SCORE_MAX, self._entry_tf, self._trend_tfs,
        )

    @property
    def state_version(self) -> int:
        return self._state_version

    def process_tick(self, price: float) -> Optional[str]:
        if price > 0:
            self._current_price = price
        return None

    def consume_signal(self) -> Optional[str]:
        signal = self._pending_signal
        self._last_consumed_signal_id = self._pending_signal_id
        self._pending_signal = None
        self._pending_signal_id = None
        if signal is None:
            return None
        if time.time() - self._pending_signal_time > SIGNAL_MAX_AGE_SECONDS:
            logger.info("Discarded stale %s signal (older than %ds).", signal, SIGNAL_MAX_AGE_SECONDS)
            self._sync_state_version()
            return None
        self._sync_state_version()
        return signal

    @property
    def last_consumed_signal_id(self) -> Optional[str]:
        return self._last_consumed_signal_id

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
        if tf_biases is not None:
            self._mtf_tf_biases = tf_biases
            self._mtf_sig = tuple(sorted(tf_biases.items()))
        self._mtf_bias = bias
        self._mtf_agreement = agreement
        self._sync_state_version()

    def get_state(self) -> Dict[str, Any]:
        entry_tracker = self._trackers.get(self._entry_tf)
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
            "entry_adx": round(entry_tracker.adx, 1) if entry_tracker and entry_tracker.adx is not None else None,
            "entry_rsi": round(entry_tracker.rsi, 1) if entry_tracker and entry_tracker.rsi is not None else None,
            "entry_macd_hist": round(entry_tracker.macd_hist, 5) if entry_tracker else None,
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
        self._pending_signal_id = None
        self._last_consumed_signal_id = None
        self._last_rejection = None
        self._last_evaluation = None
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
            return
        self._update_trend_bias()
        entry_tracker = self._trackers[self._entry_tf]
        if entry_tracker.atr is not None:
            self._atr_history.append(entry_tracker.atr)
        if entry_tracker.close > 0:
            self._current_price = entry_tracker.close
        if (entry_tracker.last_epoch is not None
                and entry_tracker.last_epoch != self._last_entry_epoch_evaluated):
            self._last_entry_epoch_evaluated = entry_tracker.last_epoch
            self._evaluate_entry_candle(entry_tracker)
            self._last_evaluation = self._build_evaluation(entry_tracker)
        self._sync_state_version()

    def _build_evaluation(self, tracker) -> Dict[str, Any]:
        bd = self._last_signal_score_breakdown or {}
        if self._pending_signal:
            direction = self._pending_signal
        elif self._trend_direction == "UP":
            direction = "BUY"
        elif self._trend_direction == "DOWN":
            direction = "SELL"
        else:
            direction = "-"
        return {
            "signal_id": self._pending_signal_id or "",
            "direction": direction,
            "trend": self._trend_direction or "-",
            "taken": "TRUE" if self._pending_signal else "FALSE",
            "rejection_reason": self._last_rejection or "",
            "score": self._last_signal_score,
            "threshold": self._entry_score_threshold,
            "s_trend": bd.get("trend", ""),
            "s_trigger": bd.get("trigger", ""),
            "s_momentum": bd.get("momentum", ""),
            "s_volatility": bd.get("volatility", ""),
            "s_alignment": bd.get("alignment", ""),
            "s_adx": bd.get("adx", ""),
            "s_macd": bd.get("macd", ""),
            "s_rsi_zone": bd.get("rsi_zone", ""),
            "s_pattern": bd.get("pattern", ""),
            "s_structure": bd.get("structure", ""),
            "entry_adx": round(tracker.adx, 1) if tracker.adx is not None else "",
            "entry_rsi": round(tracker.rsi, 1) if tracker.rsi is not None else "",
            "entry_macd_hist": round(tracker.macd_hist, 5),
            "atr": round(tracker.atr, 5) if tracker.atr is not None else "",
            "close": round(tracker.close, 5) if tracker.close else "",
        }

    def get_last_evaluation(self) -> Optional[Dict[str, Any]]:
        record = self._last_evaluation
        self._last_evaluation = None
        return record

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
        self._last_rejection = reason
        logger.debug("Setup rejected: %s", reason)

    def _evaluate_entry_candle(self, tracker: _TimeframeTracker) -> None:
        self._last_signal_score = 0
        self._last_signal_score_breakdown = {}
        self._last_rejection = None
        if self._trend_direction not in ("UP", "DOWN"):
            self._pattern_stage = "IDLE"
            self._last_rejection = "no 30m+1h trend agreement"
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
        if tracker.adx is None or tracker.rsi is None:
            self._pattern_stage = "TREND"
            return

        signal = "BUY" if self._trend_direction == "UP" else "SELL"
        metrics = _candle_metrics(candle)
        close = metrics["close"]
        body_ratio = metrics["body_ratio"]

        separation = abs(tracker.ema_fast - tracker.ema_slow)
        if separation < 0.30 * atr:
            self._reject("EMAs too flat - no trend strength")
            return
        if tracker.adx < ADX_MIN_TREND:
            self._reject(f"entry-tf ADX {tracker.adx:.1f} below {ADX_MIN_TREND} - 15m is choppy")
            return
        if len(self._atr_history) >= 24:
            vol_rank = _percentile_rank(self._atr_history, atr)
            if vol_rank < 0.10:
                self._reject("volatility too dead")
                return
            if vol_rank > 0.95:
                self._reject("volatility spike - unstable conditions")
                return

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
        if tracker.divergence_against(signal):
            self._reject("RSI/price divergence against trade direction - momentum fading")
            return

        trend_score = 5
        trigger_score = 3 if body_ratio >= 0.65 else (2 if body_ratio >= 0.50 else (1 if body_ratio >= 0.35 else 0))
        momentum_score = (3 if directional_close_position >= 0.72
                          else 2 if directional_close_position >= 0.60
                          else 1 if directional_close_position >= 0.50 else 0)
        atr_ratio = atr / close if close > 0 else 0.0
        volatility_score = 2 if 0.00005 <= atr_ratio <= 0.020 else (1 if atr_ratio > 0 else 0)
        alignment_score = 1 if self._mtf_tf_biases.get("5m") == self._trend_direction else 0
        adx_score = 3 if tracker.adx >= 35 else (2 if tracker.adx >= 25 else 1)
        macd_aligned = tracker.macd_hist > 0 if signal == "BUY" else tracker.macd_hist < 0
        macd_trail = list(tracker.macd_hist_trail)
        macd_rising = len(macd_trail) >= 2 and (
            macd_trail[-1] > macd_trail[-2] if signal == "BUY" else macd_trail[-1] < macd_trail[-2])
        macd_score = 2 if (macd_aligned and macd_rising) else (1 if macd_aligned else 0)
        rsi = tracker.rsi
        if signal == "BUY":
            rsi_score = 2 if 45.0 <= rsi <= 70.0 else (1 if 70.0 < rsi <= 82.0 else 0)
        else:
            rsi_score = 2 if 30.0 <= rsi <= 55.0 else (1 if 18.0 <= rsi < 30.0 else 0)
        pattern_score = _candlestick_pattern_score(candle, prev, signal)
        structure_score = tracker.structure_score(signal)

        score = (trend_score + trigger_score + momentum_score + volatility_score
                 + alignment_score + adx_score + macd_score + rsi_score
                 + pattern_score + structure_score)
        breakdown = {
            "trend": trend_score, "trigger": trigger_score, "momentum": momentum_score,
            "volatility": volatility_score, "alignment": alignment_score, "adx": adx_score,
            "macd": macd_score, "rsi_zone": rsi_score, "pattern": pattern_score,
            "structure": structure_score,
        }
        self._last_signal_score = score
        self._last_signal_score_breakdown = breakdown
        self._breakdown_sig = tuple(sorted(breakdown.items()))

        if score >= self._entry_score_threshold:
            self._pending_signal = signal
            self._pending_signal_id = uuid.uuid4().hex[:10]
            self._pending_signal_time = time.time()
            self._pattern_stage = "SIGNAL"
            self._last_entry_mode = "confluence"
            logger.info(
                "CONFLUENCE %s setup | Score %d/%d | %s | close=%.5f | atr=%.5f | adx=%.1f | rsi=%.1f",
                signal, score, SCORE_MAX, breakdown, close, atr, tracker.adx, rsi,
            )
        else:
            self._reject(f"score {score}/{SCORE_MAX} below threshold {self._entry_score_threshold}")

    def _sync_state_version(self) -> None:
        signature = (
            self._trend_direction, self._pattern_stage, self._mtf_bias,
            self._mtf_agreement, self._last_entry_mode, self._last_signal_score,
            self._pending_signal, self._mtf_sig, self._breakdown_sig,
        )
        if signature != self._state_signature:
            self._state_signature = signature
            self._state_version += 1