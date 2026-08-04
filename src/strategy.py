"""src/strategy.py — MomentumMaster TF confluence engine."""

import time
import uuid
from collections import deque
from typing import Any, Dict, Optional

from config import (
    ADX_MIN_TREND,
    ADX_PERIOD,
    CANDLE_GRANULARITIES,
    DIVERGENCE_LOOKBACK,
    ENTRY_SCORE_THRESHOLD,
    ENTRY_TIMEFRAME,
    ENTRY_TIMEFRAME_BY_DURATION,
    MACD_FAST,
    MACD_SIGNAL,
    MACD_SLOW,
    REGIME_EXHAUSTION_ATR,
    REGIME_LONG_1H_ADX_FLOOR,
    REGIME_SHORT_5M_ADX_FLOOR,
    REGIME_TRIGGER_BODY_MIN,
    REGIME_VOL_BAND,
    RSI_PERIOD,
    TREND_TIMEFRAMES,
)
from src.logger import get_logger

logger = get_logger("strategy")

SCORE_MAX = 25
SIGNAL_MAX_AGE_SECONDS = 240
CORE_FLOOR = 7


def _safe_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _delay_for(granularity):
    if granularity <= 60:
        return 6.0

    if granularity <= 300:
        return 12.0

    return 30.0


def _closed_candles(candles, granularity, now, delay_seconds=30.0):
    closed = []

    for candle in candles:
        epoch = _safe_float(candle.get("epoch"), 0.0)

        if epoch <= 0:
            continue

        if epoch + granularity <= now - delay_seconds:
            closed.append(candle)

    return closed


def _candle_metrics(candle):
    o = _safe_float(candle.get("open"))
    h = _safe_float(candle.get("high"))
    l = _safe_float(candle.get("low"))
    c = _safe_float(candle.get("close"))

    rng = max(h - l, 1e-12)
    body = abs(c - o)

    return {
        "open": o,
        "high": h,
        "low": l,
        "close": c,
        "range": rng,
        "body_ratio": body / rng,
        "close_position": (c - l) / rng,
        "upper_wick_ratio": (h - max(c, o)) / rng,
        "lower_wick_ratio": (min(c, o) - l) / rng,
    }


def _candlestick_pattern_score(candle, prev, direction):
    if candle is None or prev is None:
        return 0

    m = _candle_metrics(candle)
    p = _candle_metrics(prev)

    bull_engulf = (
        m["close"] > m["open"]
        and p["close"] < p["open"]
        and m["close"] >= p["open"]
        and m["open"] <= p["close"]
    )

    bear_engulf = (
        m["close"] < m["open"]
        and p["close"] > p["open"]
        and m["close"] <= p["open"]
        and m["open"] >= p["close"]
    )

    bull_pin = (
        m["lower_wick_ratio"] >= 0.45
        and m["body_ratio"] <= 0.40
        and m["close_position"] >= 0.55
    )

    bear_pin = (
        m["upper_wick_ratio"] >= 0.45
        and m["body_ratio"] <= 0.40
        and m["close_position"] <= 0.45
    )

    if direction == "BUY":
        return 2 if bull_engulf else (1 if bull_pin else 0)

    return 2 if bear_engulf else (1 if bear_pin else 0)


class _TimeframeTracker:
    __slots__ = (
        "granularity",
        "closes",
        "last_epoch",
        "last_candle",
        "prev_candle",
        "ema_fast",
        "ema_slow",
        "ema_fast_trail",
        "close",
        "prev_close",
        "slope",
        "tr_values",
        "atr",
        "bias",
        "rsi",
        "rsi_history",
        "_avg_gain",
        "_avg_loss",
        "ema12",
        "ema26",
        "macd_hist",
        "macd_hist_trail",
        "_macd_signal",
        "_tr_smooth",
        "_dm_plus_smooth",
        "_dm_minus_smooth",
        "di_plus",
        "di_minus",
        "adx",
        "recent_highs",
        "recent_lows",
        "_fast_alpha",
        "_slow_alpha",
        "_atr_period",
    )

    def __init__(self, granularity, lookback=80, fast=20, slow=50, atr_period=14):
        self.granularity = granularity

        self.closes = deque(maxlen=lookback)
        self.last_epoch = None
        self.last_candle = None
        self.prev_candle = None

        self.ema_fast = None
        self.ema_slow = None
        self.ema_fast_trail = deque(maxlen=6)

        self.close = 0.0
        self.prev_close = 0.0
        self.slope = 0.0

        self.tr_values = deque(maxlen=atr_period * 3)
        self.atr = None
        self.bias = "FLAT"
        self.rsi = None
        self.rsi_history = deque(maxlen=max(30, DIVERGENCE_LOOKBACK * 4))
        self._avg_gain = None
        self._avg_loss = None

        self.ema12 = None
        self.ema26 = None
        self.macd_hist = 0.0
        self.macd_hist_trail = deque(maxlen=3)
        self._macd_signal = None

        self._tr_smooth = None
        self._dm_plus_smooth = None
        self._dm_minus_smooth = None
        self.di_plus = 0.0
        self.di_minus = 0.0
        self.adx = None

        self.recent_highs = deque(maxlen=6)
        self.recent_lows = deque(maxlen=6)

        self._fast_alpha = 2.0 / (fast + 1)
        self._slow_alpha = 2.0 / (slow + 1)
        self._atr_period = atr_period

    def absorb(self, closed):
        if not closed:
            return False

        newest = int(_safe_float(closed[-1].get("epoch")))

        if self.last_epoch is None:
            self._resync(closed)
            return True

        if newest != self.last_epoch + self.granularity:
            self._resync(closed)
            return True

        fresh = [c for c in closed if int(_safe_float(c.get("epoch"))) > self.last_epoch]

        for candle in fresh:
            self._advance(candle)

        return bool(fresh)

    def reset(self):
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

    def _advance(self, candle):
        close = _safe_float(candle.get("close"))
        high = _safe_float(candle.get("high"))
        low = _safe_float(candle.get("low"))

        prior = self.last_candle
        self.prev_candle = prior
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

        w = min(self._atr_period, len(self.tr_values))
        if w:
            self.atr = sum(list(self.tr_values)[-w:]) / w

        if self.prev_close > 0:
            gain = max(close - self.prev_close, 0.0)
            loss = max(self.prev_close - close, 0.0)

            if self._avg_gain is None:
                self._avg_gain = gain
                self._avg_loss = loss
            else:
                self._avg_gain += (gain - self._avg_gain) / RSI_PERIOD
                self._avg_loss += (loss - self._avg_loss) / RSI_PERIOD

            self.rsi = (
                100.0
                if self._avg_loss <= 1e-12
                else 100.0 - (100.0 / (1.0 + self._avg_gain / self._avg_loss))
            )

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

            self._macd_signal = (
                macd_line
                if self._macd_signal is None
                else self._macd_signal + (2.0 / (MACD_SIGNAL + 1)) * (macd_line - self._macd_signal)
            )

            self.macd_hist = macd_line - self._macd_signal
            self.macd_hist_trail.append(self.macd_hist)

        if prior is not None and tr is not None:
            ph = _safe_float(prior.get("high"))
            pl = _safe_float(prior.get("low"))

            up = high - ph
            dn = pl - low

            dm_plus = up if (up > dn and up > 0) else 0.0
            dm_minus = dn if (dn > up and dn > 0) else 0.0

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

    def _resync(self, closed):
        self.reset()

        for candle in closed:
            self._advance(candle)

    def _refresh_bias(self):
        if self.ema_fast is None or self.ema_slow is None or self.adx is None:
            self.bias = "FLAT"
            return

        strong = self.adx >= ADX_MIN_TREND

        if (
            self.close > self.ema_fast > self.ema_slow
            and self.slope > 0
            and strong
            and self.di_plus > self.di_minus
        ):
            self.bias = "UP"
        elif (
            self.close < self.ema_fast < self.ema_slow
            and self.slope < 0
            and strong
            and self.di_minus > self.di_plus
        ):
            self.bias = "DOWN"
        else:
            self.bias = "FLAT"

    def structure_score(self, direction):
        if direction == "BUY":
            lows = list(self.recent_lows)
            highs = list(self.recent_highs)

            if len(lows) >= 3 and lows[-1] > lows[-3] and len(highs) >= 2 and highs[-1] >= highs[-2]:
                return 2

            if len(lows) >= 3 and lows[-1] > lows[-3]:
                return 1

            if len(lows) >= 2 and lows[-1] >= lows[-2]:
                return 1

            return 0

        highs = list(self.recent_highs)
        lows = list(self.recent_lows)

        if len(highs) >= 3 and highs[-1] < highs[-3] and len(lows) >= 2 and lows[-1] <= lows[-2]:
            return 2

        if len(highs) >= 3 and highs[-1] < highs[-3]:
            return 1

        if len(highs) >= 2 and highs[-1] <= highs[-2]:
            return 1

        return 0

    def divergence_against(self, direction):
        n = DIVERGENCE_LOOKBACK
        closes = list(self.closes)
        rsis = list(self.rsi_history)

        if len(closes) <= n or len(rsis) <= n:
            return False

        pn, pt = closes[-1], closes[-1 - n]
        rn, rt = rsis[-1], rsis[-1 - n]

        if direction == "BUY":
            return pn > pt and rn < rt - 1.0

        return pn < pt and rn > rt + 1.0


class StrategyEngine:
    @staticmethod
    def _regime_for(minutes):
        if minutes <= 15:
            return "SHORT"

        if minutes <= 30:
            return "MEDIUM"

        return "LONG"

    def __init__(
        self,
        entry_score_threshold=ENTRY_SCORE_THRESHOLD,
        contract_duration_minutes=30,
        entry_adx_floor=ADX_MIN_TREND,
        **_ignored,
    ):
        self._entry_score_threshold = entry_score_threshold
        self._entry_adx_floor = entry_adx_floor
        self._contract_minutes = int(contract_duration_minutes)
        self._regime = self._regime_for(self._contract_minutes)

        self._trackers = {
            tf: _TimeframeTracker(gran)
            for tf, gran in CANDLE_GRANULARITIES.items()
        }

        self._entry_tf = ENTRY_TIMEFRAME_BY_DURATION.get(self._contract_minutes, ENTRY_TIMEFRAME)

        if self._entry_tf not in self._trackers:
            self._entry_tf = ENTRY_TIMEFRAME

        self._trend_tfs = list(TREND_TIMEFRAMES)
        self._atr_history = deque(maxlen=240)

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
        self._last_express_bonus = 0.0

        self._current_price = 0.0
        self._trades_in_trend = 0
        self._in_cooldown = False

        self._state_version = 0
        self._state_signature = None
        self._mtf_sig = ()
        self._breakdown_sig = ()
        self._recommended_duration = self._contract_minutes

        logger.info(
            "Confluence engine ready | threshold=%d/%d | %s ADX floor=%d | regime=%s (%dm)",
            self._entry_score_threshold,
            SCORE_MAX,
            self._entry_tf,
            self._entry_adx_floor,
            self._regime,
            self._contract_minutes,
        )

    @property
    def state_version(self):
        return self._state_version

    @property
    def express_bonus(self):
        return self._last_express_bonus

    def process_tick(self, price):
        if price > 0:
            self._current_price = price

        return None

    def consume_signal(self):
        signal = self._pending_signal
        self._last_consumed_signal_id = self._pending_signal_id

        self._pending_signal = None
        self._pending_signal_id = None

        if signal is None:
            return None

        if time.time() - self._pending_signal_time > SIGNAL_MAX_AGE_SECONDS:
            self._sync_state_version()
            return None

        self._sync_state_version()
        return signal

    @property
    def last_consumed_signal_id(self):
        return self._last_consumed_signal_id

    def get_current_price(self):
        return self._current_price

    def on_trade_executed(self):
        self._trades_in_trend += 1
        self._pending_signal = None
        self._pattern_stage = "IDLE"
        self._sync_state_version()

    def on_signal_skipped(self):
        self._pending_signal = None
        self._pattern_stage = "TREND" if self._trend_direction else "IDLE"
        self._sync_state_version()

    def update_mtf_bias(self, bias, agreement=0, tf_biases=None):
        if tf_biases is not None:
            self._mtf_tf_biases = tf_biases
            self._mtf_sig = tuple(sorted(tf_biases.items()))

        self._mtf_bias = bias
        self._mtf_agreement = agreement
        self._sync_state_version()

    def get_state(self):
        et = self._trackers.get(self._entry_tf)

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
            "entry_adx": round(et.adx, 1) if et and et.adx is not None else None,
            "entry_rsi": round(et.rsi, 1) if et and et.rsi is not None else None,
            "entry_macd_hist": round(et.macd_hist, 5) if et else None,
            "recommended_duration": self._recommended_duration,
        }

    def reset(self):
        for t in self._trackers.values():
            t.reset()

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
        self._last_express_bonus = 0.0

        self._current_price = 0.0
        self._trades_in_trend = 0
        self._in_cooldown = False

        self._mtf_sig = ()
        self._breakdown_sig = ()
        self._state_signature = None
        self._state_version += 1

    def update_candles(self, candles_by_tf, now):
        any_new = False
        entry_new = False

        for tf, tracker in self._trackers.items():
            closed = _closed_candles(
                candles_by_tf.get(tf) or [],
                tracker.granularity,
                now,
                _delay_for(tracker.granularity),
            )

            if tracker.absorb(closed):
                any_new = True

                if tf == self._entry_tf:
                    entry_new = True

        if not any_new:
            return

        self._update_trend_bias()

        et = self._trackers[self._entry_tf]

        if entry_new and et.atr is not None:
            self._atr_history.append(et.atr)

        if et.close > 0:
            self._current_price = et.close

        if et.last_epoch is not None and et.last_epoch != self._last_entry_epoch_evaluated:
            self._last_entry_epoch_evaluated = et.last_epoch
            self._evaluate_entry_candle(et)
            self._last_evaluation = self._build_evaluation(et)

        self._sync_state_version()

    def _build_evaluation(self, tracker):
        bd = self._last_signal_score_breakdown or {}

        direction = self._pending_signal or (
            "BUY" if self._trend_direction == "UP" else ("SELL" if self._trend_direction == "DOWN" else "-")
        )

        return {
            "signal_id": self._pending_signal_id or "",
            "direction": direction,
            "trend": self._trend_direction or "-",
            "taken": "TRUE" if self._pending_signal else "FALSE",
            "rejection_reason": self._last_rejection or "",
            "score": self._last_signal_score,
            "threshold": self._entry_score_threshold,
            "regime": self._regime,
            "duration_min": self._contract_minutes,
            "recommended_duration": self._recommended_duration,
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
            "tf_5m": self._mtf_tf_biases.get("5m", ""),
            "tf_15m": self._mtf_tf_biases.get("15m", ""),
            "tf_30m": self._mtf_tf_biases.get("30m", ""),
            "tf_1h": self._mtf_tf_biases.get("1h", ""),
            "mtf_agreement": self._mtf_agreement,
        }

    def get_last_evaluation(self):
        rec = self._last_evaluation
        self._last_evaluation = None
        return rec

    def _update_trend_bias(self):
        self._mtf_tf_biases = {tf: t.bias for tf, t in self._trackers.items()}

        b30 = self._mtf_tf_biases.get("30m", "FLAT")
        b1h = self._mtf_tf_biases.get("1h", "FLAT")

        if self._regime == "SHORT":
            if b30 == "UP" and b1h != "DOWN":
                self._trend_direction, self._mtf_bias = "UP", b30
            elif b30 == "DOWN" and b1h != "UP":
                self._trend_direction, self._mtf_bias = "DOWN", b30
            else:
                self._trend_direction = None
                self._mtf_bias = b30 if b30 != "FLAT" else b1h

            self._mtf_agreement = (1 if self._trend_direction else 0) + (
                1 if b1h == self._trend_direction else 0
            )
        else:
            if b30 == "UP" and b1h == "UP":
                self._trend_direction, self._mtf_bias, self._mtf_agreement = "UP", "UP", 2
            elif b30 == "DOWN" and b1h == "DOWN":
                self._trend_direction, self._mtf_bias, self._mtf_agreement = "DOWN", "DOWN", 2
            else:
                self._trend_direction = None

                up = (b30 == "UP") + (b1h == "UP")
                dn = (b30 == "DOWN") + (b1h == "DOWN")

                self._mtf_agreement = max(up, dn)
                self._mtf_bias = "UP" if up > dn else ("DOWN" if dn > up else None)

        if self._pending_signal and self._trend_direction is None:
            self._pending_signal = None

        if self._pattern_stage != "SIGNAL":
            self._pattern_stage = "TREND" if self._trend_direction else "IDLE"

        self._mtf_sig = tuple(sorted(self._mtf_tf_biases.items()))

    def _reject(self, reason):
        self._pattern_stage = "TREND" if self._trend_direction else "IDLE"
        self._last_rejection = reason
        logger.debug("Setup rejected: %s", reason)

    def _evaluate_entry_candle(self, tracker):
        self._last_signal_score = 0
        self._last_signal_score_breakdown = {}
        self._last_rejection = None
        self._last_express_bonus = 0.0
        self._recommended_duration = self._contract_minutes

        if self._trend_direction not in ("UP", "DOWN"):
            self._pattern_stage = "IDLE"
            self._last_rejection = "no trend agreement for this contract length"
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

        if abs(tracker.ema_fast - tracker.ema_slow) < 0.30 * atr:
            self._reject("EMAs too flat - no trend strength")
            return

        if self._entry_tf != "1m" and tracker.adx < self._entry_adx_floor:
            self._reject(
                f"entry-tf ADX {tracker.adx:.1f} below {self._entry_adx_floor} - {self._entry_tf} is choppy"
            )
            return

        atr_ratio = atr / close if close > 0 else 0.0
        vol_floor, vol_ceiling = REGIME_VOL_BAND.get(self._regime, (0.00010, 0.05))

        if atr_ratio < vol_floor:
            self._reject(f"market too quiet right now for a {self._contract_minutes}m contract")
            return

        if atr_ratio > vol_ceiling:
            self._reject("volatility spiking - unstable conditions")
            return

        if atr_ratio > 0.012 and self._contract_minutes > 15:
            self._reject(
                f"high volatility + {self._contract_minutes}m contract = reversal risk; use a shorter contract"
            )
            self._recommended_duration = 15
            return

        if signal == "BUY":
            trigger = close > _safe_float(prev.get("high")) and close > metrics["open"]
            ema_ok = close > tracker.ema_fast
            dcp = metrics["close_position"]
        else:
            trigger = close < _safe_float(prev.get("low")) and close < metrics["open"]
            ema_ok = close < tracker.ema_fast
            dcp = 1.0 - metrics["close_position"]

        trigger_score = (
            4 if body_ratio >= 0.70 else
            3 if body_ratio >= 0.60 else
            2 if body_ratio >= 0.50 else
            1 if body_ratio >= 0.35 else
            0
        )

        momentum_score = (
            4 if dcp >= 0.78 else
            3 if dcp >= 0.68 else
            2 if dcp >= 0.58 else
            1 if dcp >= 0.50 else
            0
        )

        adx_score = 2 if tracker.adx >= 30 else 1 if tracker.adx >= 22 else 0

        macd_aligned = tracker.macd_hist > 0 if signal == "BUY" else tracker.macd_hist < 0
        mt = list(tracker.macd_hist_trail)
        macd_rising = len(mt) >= 2 and (mt[-1] > mt[-2] if signal == "BUY" else mt[-1] < mt[-2])
        macd_score = 2 if (macd_aligned and macd_rising) else 1 if macd_aligned else 0

        pattern_score = _candlestick_pattern_score(candle, prev, signal)
        structure_score = tracker.structure_score(signal)

        core_strength = trigger_score + momentum_score + adx_score + macd_score + pattern_score
        express_bonus = 1.0 if core_strength >= 11 else (0.5 if core_strength >= 9 else 0.0)
        self._last_express_bonus = express_bonus

        relaxed_mult = REGIME_EXHAUSTION_ATR.get(self._regime, 2.75) + express_bonus
        exhausted = (
            close > tracker.ema_fast + relaxed_mult * atr
            if signal == "BUY"
            else close < tracker.ema_fast - relaxed_mult * atr
        )

        if not trigger:
            self._reject("no trigger break of prior candle")
            return

        if not ema_ok:
            self._reject("close not beyond fast EMA")
            return

        if exhausted:
            self._reject(f"move exhausted - too far from EMA (>{relaxed_mult:.2f} ATR)")
            return

        if tracker.divergence_against(signal):
            self._reject("RSI/price divergence against trade direction - momentum fading")
            return

        if structure_score < 1:
            self._reject("entry-tf market structure broken - no swing respect (hard gate)")
            return

        if self._regime == "SHORT":
            if body_ratio < REGIME_TRIGGER_BODY_MIN:
                self._reject(
                    f"trigger candle too weak (body {body_ratio:.2f} < {REGIME_TRIGGER_BODY_MIN}) for a contract <=15m"
                )
                return

            need = "UP" if signal == "BUY" else "DOWN"
            five = self._trackers.get("5m")

            if five is None or five.bias != need:
                self._reject("5m not aligned with the trade (required for contracts <=15m)")
                return

            if five.adx is None or five.adx < REGIME_SHORT_5M_ADX_FLOOR:
                self._reject("5m not actually trending, only directionally biased (required for contracts <=15m)")
                return

        elif self._regime == "LONG":
            h = self._trackers.get("1h")

            if h is None or h.structure_score(signal) < 1:
                self._reject("1h market structure not intact (required for contracts >=60m)")
                return

            if h.adx is None or h.adx < REGIME_LONG_1H_ADX_FLOOR:
                self._reject(f"1h ADX below {REGIME_LONG_1H_ADX_FLOOR} - trend too weak to hold for 60m+")
                return

            h_macd_aligned = h.macd_hist > 0 if signal == "BUY" else h.macd_hist < 0
            if not h_macd_aligned:
                self._reject("1h MACD not aligned with the trade (required for contracts >=60m)")
                return

        trend_score = 5 if self._mtf_agreement >= 2 else 3

        volatility_score = (
            3 if 0.0002 <= atr_ratio <= 0.012 else
            2 if vol_floor <= atr_ratio <= vol_ceiling else
            1 if atr_ratio > 0 else
            0
        )

        b5 = self._mtf_tf_biases.get("5m")
        b15 = self._mtf_tf_biases.get("15m")

        alignment_score = (
            2 if (b5 == self._trend_direction and b15 == self._trend_direction) else
            1 if b5 == self._trend_direction else
            0
        )

        rsi = tracker.rsi

        if signal == "BUY":
            rsi_score = 2 if 45.0 <= rsi <= 70.0 else (1 if 70.0 < rsi <= 82.0 else 0)
        else:
            rsi_score = 2 if 30.0 <= rsi <= 55.0 else (1 if 18.0 <= rsi < 30.0 else 0)

        if (trigger_score + momentum_score + structure_score) < CORE_FLOOR:
            self._reject(
                f"weak core (trigger+momentum+structure={trigger_score + momentum_score + structure_score} < {CORE_FLOOR}) despite score"
            )
            return

        if alignment_score < 1:
            self._reject("5m not aligned with the trade (quality gate)")
            return

        raw_score = (
            trend_score
            + trigger_score
            + momentum_score
            + volatility_score
            + alignment_score
            + adx_score
            + macd_score
            + rsi_score
            + pattern_score
            + structure_score
        )

        score = min(SCORE_MAX, raw_score)

        breakdown = {
            "trend": trend_score,
            "trigger": trigger_score,
            "momentum": momentum_score,
            "volatility": volatility_score,
            "alignment": alignment_score,
            "adx": adx_score,
            "macd": macd_score,
            "rsi_zone": rsi_score,
            "pattern": pattern_score,
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
            self._last_entry_mode = "confluence-express" if express_bonus else "confluence"

            logger.info(
                "CONFLUENCE %s | %d/%d | regime=%s | entry=%s | express=+%.1f | core=%d | %s",
                signal,
                score,
                SCORE_MAX,
                self._regime,
                self._entry_tf,
                express_bonus,
                core_strength,
                breakdown,
            )
        else:
            self._reject(f"score {score}/{SCORE_MAX} below threshold {self._entry_score_threshold}")

    def _sync_state_version(self):
        sig = (
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

        if sig != self._state_signature:
            self._state_signature = sig
            self._state_version += 1
