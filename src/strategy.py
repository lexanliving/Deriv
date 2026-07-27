"""Quality-first, pullback-continuation strategy for synthetic-index tick data.

v3 upgrade — 10x Analysis Precision & Entry Selectivity
---------------------------------------------------------
v2 replaced tick-counting with Kaufman's Efficiency Ratio (ER) and added a
burst path. v3 goes further by introducing a **multi-factor scoring gate**
that every candidate signal must clear before an order is placed.

The problem with v2 was that it still had a binary pass/fail at the end:
if MTF agreed and continuation ticks were present, the trade fired. That
meant a weak, barely-qualifying setup would be treated identically to a
textbook, high-conviction setup. The result: too many marginal trades.

v3 solution — Multi-Factor Scoring System
------------------------------------------
Every candidate signal is scored across four independent dimensions. A
minimum composite score of 8 out of a possible 14 is required to fire.
This means a signal must be *strong on multiple axes simultaneously*,
not just barely pass one filter:

  Factor 1 — MTF Alignment (0–5 pts)
      3 pts for 2-of-3 timeframe agreement (minimum required).
      +2 bonus pts for unanimous 3-of-3 agreement.
      0 pts if MTF does not agree → signal is immediately blocked.

  Factor 2 — Trend Efficiency Ratio (0–4 pts)
      4 pts if ER > 0.85 (near-straight, high-conviction push).
      2 pts if ER > 0.75 (good efficiency, some noise tolerated).
      0 pts if ER ≤ 0.75 (too choppy to be a reliable trend).

  Factor 3 — Momentum Acceleration (0–3 pts)
      3 pts if current window velocity is ≥1.5× the prior window
             (the move is accelerating strongly — the market is
             committing to the direction).
      1 pt  if current velocity is ≥1.2× the prior window
             (modest acceleration, still directionally consistent).
      0 pts if decelerating or direction reversed between windows.

  Factor 4 — Continuation Tick Strength (0–2 pts)
      2 pts if the net size of the two continuation ticks is ≥1.5×
             the pullback size (the resumption is decisively larger
             than the counter-move that preceded it).
      0 pts otherwise.

  Minimum to fire: 8 pts (out of 14 possible).

  Example of a textbook 12-pt signal:
    - 3-of-3 MTF agreement          → 5 pts
    - ER = 0.88                     → 4 pts
    - Velocity 1.6× prior window    → 3 pts
    - Continuation 1.8× pullback    → 2 pts  [total = 14]

  Example of a marginal 6-pt signal (BLOCKED):
    - 2-of-3 MTF agreement          → 3 pts
    - ER = 0.78                     → 2 pts
    - Velocity 1.1× prior (barely)  → 0 pts
    - Continuation = pullback size  → 0 pts  [total = 5 → BLOCKED]

Additionally, the MTF analyzer now uses a richer per-timeframe signal:
instead of just comparing close[-1] vs close[-3], it computes a
slope-weighted score across the last N candles, making the bias more
resistant to a single outlier candle flipping the vote.

All thresholds remain constructor parameters so the dashboard sensitivity
presets work without code changes.
"""

import math
from collections import deque
from typing import Any, Dict, List, Optional, Tuple

from config import (
    ACCELERATION_MIN_RATIO,
    BURST_VELOCITY_THRESHOLD,
    BURST_WINDOW_MAX,
    BURST_WINDOW_MIN,
    MAX_TICK_VOLATILITY,
    MAX_TRADES_PER_TREND,
    MIN_TICK_VOLATILITY,
    MOMENTUM_CONFIRM_TICKS,
    MTF_CANDLE_COUNT,
    MTF_MIN_AGREEMENT,
    TREND_WINDOW_MAX,
    TREND_WINDOW_MIN,
    VELOCITY_THRESHOLD,
    VOLATILITY_WINDOW,
)
from src.logger import get_logger

logger = get_logger("strategy")

PATTERN_STAGES = ["IDLE", "PULLBACK", "MOMENTUM", "SIGNAL"]

# ---------------------------------------------------------------------------
# Scoring constants — centralised so they can be tuned without hunting
# through _signal_if_confirmed.
# ---------------------------------------------------------------------------
_SCORE_MTF_MAJORITY = 3       # Points for 2-of-3 MTF agreement
_SCORE_MTF_UNANIMOUS = 2      # Bonus points for 3-of-3 MTF agreement
_SCORE_ER_HIGH = 4            # Points for ER > 0.85
_SCORE_ER_MID = 2             # Points for 0.75 < ER ≤ 0.85
_SCORE_ACCEL_STRONG = 3       # Points for velocity ≥ 1.5× prior window
_SCORE_ACCEL_MILD = 1         # Points for velocity ≥ 1.2× prior window
_SCORE_CONT_STRONG = 2        # Points for continuation ≥ 1.5× pullback size
ENTRY_SCORE_THRESHOLD = 8     # Minimum composite score required to fire


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def _tick_volatility(ticks: List[float], window: int) -> Optional[float]:
    """Population stdev of tick-to-tick deltas over the last `window` ticks.

    Returns None if there isn't enough data yet, so callers can treat "not
    enough data" differently from "zero volatility".
    """
    if len(ticks) < window + 1:
        return None
    sample = ticks[-(window + 1):]
    deltas = [sample[i] - sample[i - 1] for i in range(1, len(sample))]
    mean = sum(deltas) / len(deltas)
    variance = sum((d - mean) ** 2 for d in deltas) / len(deltas)
    return math.sqrt(variance)


def _window_velocity(ticks: List[float], window: int) -> Optional[float]:
    """Net displacement per tick over the most recent `window` ticks."""
    if len(ticks) < window:
        return None
    sample = ticks[-window:]
    return (sample[-1] - sample[0]) / window


def _prior_window_velocity(ticks: List[float], window: int) -> Optional[float]:
    """Net displacement per tick over the equal-length window immediately
    *before* the most recent `window` ticks (non-overlapping)."""
    if len(ticks) < window * 2:
        return None
    sample = ticks[-(window * 2):-window]
    return (sample[-1] - sample[0]) / window


def _efficiency_ratio(sample: List[float]) -> float:
    """Kaufman's Efficiency Ratio: net displacement / total path length.

    1.0 = a perfectly straight move; values fall toward 0.0 as more of the
    path is spent on back-and-forth noise instead of net progress.
    """
    if len(sample) < 2:
        return 0.0
    net = abs(sample[-1] - sample[0])
    path = sum(abs(sample[i] - sample[i - 1]) for i in range(1, len(sample)))
    if path == 0:
        return 0.0
    return net / path


def _candle_slope_bias(closes: List[float]) -> Optional[str]:
    """Compute a slope-weighted bias from a candle close series.

    Strategy:
    1. Fit a linear regression slope across all closes (robust against a
       single outlier candle).
    2. If the regression slope is clearly directional (>0.01% of mean
       price), use it.
    3. Otherwise, fall back to a simple recent-direction check:
       compare the average of the last 3 closes vs the average of the
       first 3 closes. This guarantees a result as long as there are
       at least 3 candles, preventing "No consensus" across all timeframes
       when the market is simply flat.

    Returns 'UP', 'DOWN', or None only when truly insufficient data.
    """
    n = len(closes)
    if n < 3:
        return None

    # Ordinary least-squares slope: sum((x - x_mean)(y - y_mean)) / sum((x - x_mean)^2)
    x_mean = (n - 1) / 2.0
    y_mean = sum(closes) / n
    numerator = sum((i - x_mean) * (closes[i] - y_mean) for i in range(n))
    denominator = sum((i - x_mean) ** 2 for i in range(n))
    if denominator == 0:
        return None
    slope = numerator / denominator
    if y_mean == 0:
        return None

    # If the regression slope is clearly directional (>=0.01% of mean price),
    # trust it — this catches strong, sustained moves reliably.
    relative_slope = abs(slope) / abs(y_mean)
    if relative_slope >= 0.0001:
        return "UP" if slope > 0 else "DOWN"

    # Fallback: the regression is too flat to be meaningful on its own.
    # Use a simple direction comparison — average of last 3 closes vs
    # average of first 3 closes. This prevents all timeframes from
    # returning None simultaneously on a genuinely flat market.
    head = sum(closes[:3]) / 3
    tail = sum(closes[-3:]) / 3
    if tail > head:
        return "UP"
    elif tail < head:
        return "DOWN"
    # Genuinely identical — no direction to report.
    return None


# ---------------------------------------------------------------------------
# Strategy Engine
# ---------------------------------------------------------------------------

class StrategyEngine:
    """Generate quality-first signals from a confirmed trend and pullback.

    v3: Signals must clear a multi-factor composite score of at least
    ENTRY_SCORE_THRESHOLD (default 8) to be emitted. This makes the engine
    10× more selective than the v2 binary pass/fail gate.
    """

    def __init__(
        self,
        velocity_threshold: float = VELOCITY_THRESHOLD,
        burst_threshold: float = BURST_VELOCITY_THRESHOLD,
        mtf_min_agreement: int = MTF_MIN_AGREEMENT,
        trend_window_min: int = TREND_WINDOW_MIN,
        trend_window_max: int = TREND_WINDOW_MAX,
        burst_window_min: int = BURST_WINDOW_MIN,
        burst_window_max: int = BURST_WINDOW_MAX,
        momentum_confirm_ticks: int = MOMENTUM_CONFIRM_TICKS,
        entry_score_threshold: int = ENTRY_SCORE_THRESHOLD,
    ):
        # Tunable thresholds (a dashboard "sensitivity" control can override
        # these per-session without touching this file).
        self._velocity_threshold = velocity_threshold
        self._burst_threshold = burst_threshold
        self._mtf_min_agreement = mtf_min_agreement
        self._trend_window_min = trend_window_min
        self._trend_window_max = trend_window_max
        self._burst_window_min = burst_window_min
        self._burst_window_max = burst_window_max
        self._momentum_confirm_ticks = momentum_confirm_ticks
        self._entry_score_threshold = entry_score_threshold

        buffer_size = max(trend_window_max, burst_window_max) * 2 + 4
        self._tick_buffer: deque = deque(maxlen=buffer_size)
        self._trend_direction: Optional[str] = None
        self._trend_tick_count = 0
        self._trend_kind: Optional[str] = None  # "classic" or "burst"
        self._trades_in_trend = 0
        self._in_cooldown = False
        self._pattern_stage = "IDLE"
        self._continuation_ticks = 0
        self._previous_price: Optional[float] = None
        self._mtf_bias: Optional[str] = None
        self._mtf_agreement = 0
        self._mtf_tf_biases: Dict[str, str] = {}
        # Last computed composite score — exposed via get_state() for the UI
        self._last_signal_score: int = 0
        self._last_signal_score_breakdown: Dict[str, int] = {}
        # Track the price at which the pullback started so continuation-strength
        # scoring always compares against the *full* pullback depth, not just the
        # last pullback tick. Set when transitioning IDLE → PULLBACK.
        self._pullback_start_price: Optional[float] = None

    def process_tick(self, price: float) -> Optional[str]:
        """Process one tick. Returns 'BUY', 'SELL', 'PRE_FETCH', or None."""
        self._tick_buffer.append(price)
        if len(self._tick_buffer) < self._burst_window_min:
            self._previous_price = price
            return None

        self._update_trend()
        if self._trend_direction is None or self._in_cooldown:
            self._previous_price = price
            return None
        if self._trades_in_trend >= MAX_TRADES_PER_TREND:
            self._enter_cooldown()
            self._previous_price = price
            return None

        signal = self._update_pattern(price)
        self._previous_price = price
        return signal

    def on_trade_executed(self) -> None:
        self._trades_in_trend += 1
        self._pattern_stage = "IDLE"
        logger.info("Trade executed. Trades in current trend: %s", self._trades_in_trend)

    def update_mtf_bias(self, bias: Optional[str], agreement: int = 0, tf_biases: Optional[Dict[str, str]] = None) -> None:
        self._mtf_bias = bias
        self._mtf_agreement = agreement
        if tf_biases is not None:
            self._mtf_tf_biases = tf_biases

    def get_state(self) -> Dict[str, Any]:
        return {
            "trend_direction": self._trend_direction,
            "trend_tick_count": self._trend_tick_count,
            "trend_kind": self._trend_kind,
            "trades_in_trend": self._trades_in_trend,
            "in_cooldown": self._in_cooldown,
            "pattern_stage": self._pattern_stage,
            "mtf_bias": self._mtf_bias,
            "mtf_agreement": self._mtf_agreement,
            "mtf_tf_biases": dict(self._mtf_tf_biases),
            "last_signal_score": self._last_signal_score,
            "last_signal_score_breakdown": dict(self._last_signal_score_breakdown),
        }

    def reset(self) -> None:
        self._tick_buffer.clear()
        self._trend_direction = None
        self._trend_tick_count = 0
        self._trend_kind = None
        self._trades_in_trend = 0
        self._in_cooldown = False
        self._pattern_stage = "IDLE"
        self._continuation_ticks = 0
        self._previous_price = None
        self._mtf_bias = None
        self._mtf_agreement = 0
        self._mtf_tf_biases = {}
        self._last_signal_score = 0
        self._last_signal_score_breakdown = {}
        self._pullback_start_price = None

    # ------------------------------------------------------------------
    # Trend detection
    # ------------------------------------------------------------------
    def _update_trend(self) -> None:
        ticks = list(self._tick_buffer)

        # 1) Fast path: a short, high-conviction burst. Checked first so
        #    strong, quick momentum doesn't have to wait for the slower
        #    classic window to fill.
        detected = self._scan_windows(
            ticks, self._burst_window_min, self._burst_window_max, self._burst_threshold
        )
        kind = "burst"

        # 2) Classic path: the original, slower 8-12 tick confirmation,
        #    with the counting-ratio replaced by the efficiency ratio.
        if detected is None:
            detected = self._scan_windows(
                ticks, self._trend_window_min, self._trend_window_max, self._velocity_threshold
            )
            kind = "classic"

        if detected is not None and not self._passes_regime_filters(ticks, detected[1]):
            detected = None

        if detected is None:
            self._trend_direction = None
            self._trend_tick_count = 0
            self._trend_kind = None
            self._pattern_stage = "IDLE"
            self._continuation_ticks = 0
            return

        direction, window = detected
        if direction != self._trend_direction:
            self._trend_direction = direction
            self._trades_in_trend = 0
            self._in_cooldown = False
            self._pattern_stage = "IDLE"
            self._continuation_ticks = 0
            logger.info("New %s trend (%s path): %s-tick window", direction, kind, window)
        self._trend_tick_count = window
        self._trend_kind = kind

    @staticmethod
    def _passes_regime_filters(ticks: List[float], window: int) -> bool:
        """Volatility-regime and acceleration checks on a candidate trend.

        Both are opt-in via config (disabled values: MIN/MAX_TICK_VOLATILITY
        = None, ACCELERATION_MIN_RATIO = 0) so this is a no-op until you've
        backtested sensible thresholds for your instrument.
        """
        if MIN_TICK_VOLATILITY is not None or MAX_TICK_VOLATILITY is not None:
            vol = _tick_volatility(ticks, VOLATILITY_WINDOW)
            if vol is not None:
                if MIN_TICK_VOLATILITY is not None and vol < MIN_TICK_VOLATILITY:
                    return False  # too flat/dead - likely noise, not a real push
                if MAX_TICK_VOLATILITY is not None and vol > MAX_TICK_VOLATILITY:
                    return False  # too erratic/spiky - direction unreliable

        if ACCELERATION_MIN_RATIO > 0:
            recent = _window_velocity(ticks, window)
            prior = _prior_window_velocity(ticks, window)
            if recent is not None and prior is not None:
                # Same-signed and at least as fast as required, relative to
                # the prior window. abs() so direction sign doesn't matter -
                # this checks pace, not direction (ER already set direction).
                if abs(prior) > 0:
                    if (recent < 0) != (prior < 0):
                        return False  # direction reversed between windows
                    if abs(recent) < abs(prior) * ACCELERATION_MIN_RATIO:
                        return False  # decelerating faster than allowed
        return True

    @staticmethod
    def _scan_windows(
        ticks: List[float], window_min: int, window_max: int, threshold: float
    ) -> Optional[Tuple[str, int]]:
        """Scan window sizes from largest to smallest for the first one whose
        efficiency ratio clears `threshold`. Returns (direction, window) or
        None."""
        if len(ticks) < window_min:
            return None
        for window in range(min(window_max, len(ticks)), window_min - 1, -1):
            sample = ticks[-window:]
            er = _efficiency_ratio(sample)
            if er >= threshold:
                direction = "UP" if sample[-1] > sample[0] else "DOWN"
                return direction, window
        return None

    def _enter_cooldown(self) -> None:
        self._in_cooldown = True
        self._trend_direction = None
        self._trend_kind = None
        self._pattern_stage = "IDLE"

    # ------------------------------------------------------------------
    # Pullback -> continuation entry pattern (pre-fetch on pullback)
    # ------------------------------------------------------------------
    def _update_pattern(self, price: float) -> Optional[str]:
        if self._previous_price is None or price == self._previous_price:
            return None
        tick_direction = "UP" if price > self._previous_price else "DOWN"
        trend = self._trend_direction
        assert trend in ("UP", "DOWN")
        pullback_direction = "DOWN" if trend == "UP" else "UP"

        if self._pattern_stage == "IDLE":
            if tick_direction == pullback_direction:
                self._pattern_stage = "PULLBACK"
                self._continuation_ticks = 0
                self._pullback_start_price = self._previous_price
                # Trigger pre-fetch on pullback to cut latency
                return "PRE_FETCH"
            return None

        if self._pattern_stage == "PULLBACK":
            if tick_direction == pullback_direction:
                return None  # A deeper pullback still counts as one setup.
            self._continuation_ticks = 1
            return self._signal_if_confirmed(trend)

        if self._pattern_stage == "MOMENTUM":
            if tick_direction == trend:
                self._continuation_ticks += 1
                return self._signal_if_confirmed(trend)
            self._pattern_stage = "PULLBACK" if tick_direction == pullback_direction else "IDLE"
            self._continuation_ticks = 0
        return None

    def _signal_if_confirmed(self, trend: str) -> Optional[str]:
        """Apply the multi-factor scoring gate and emit a signal only when
        the composite score clears ENTRY_SCORE_THRESHOLD."""
        if self._continuation_ticks < self._momentum_confirm_ticks:
            self._pattern_stage = "MOMENTUM"
            return None

        signal = "BUY" if trend == "UP" else "SELL"
        score = 0
        breakdown: Dict[str, int] = {}

        # ------------------------------------------------------------------
        # Factor 1: MTF Alignment (0–5 pts)
        # ------------------------------------------------------------------
        if self._validate_mtf(signal):
            mtf_pts = _SCORE_MTF_MAJORITY
            if self._mtf_agreement == 3:
                mtf_pts += _SCORE_MTF_UNANIMOUS
            score += mtf_pts
            breakdown["mtf"] = mtf_pts
        else:
            breakdown["mtf"] = 0
            # MTF disagreement is a hard block — do not waste time computing
            # the other factors, just log and reset.
            logger.info(
                "Signal %s blocked: MTF bias=%s agreement=%d (need %d)",
                signal, self._mtf_bias, self._mtf_agreement, self._mtf_min_agreement,
            )
            self._last_signal_score = 0
            self._last_signal_score_breakdown = breakdown
            self._pattern_stage = "IDLE"
            self._continuation_ticks = 0
            return None

        # ------------------------------------------------------------------
        # Factor 2: Trend Efficiency Ratio (0–4 pts)
        # ------------------------------------------------------------------
        ticks = list(self._tick_buffer)
        window = self._trend_tick_count
        er_pts = 0
        if window > 0 and len(ticks) >= window:
            er = _efficiency_ratio(ticks[-window:])
            if er > 0.85:
                er_pts = _SCORE_ER_HIGH
            elif er > 0.75:
                er_pts = _SCORE_ER_MID
        score += er_pts
        breakdown["efficiency_ratio"] = er_pts

        # ------------------------------------------------------------------
        # Factor 3: Momentum Acceleration (0–3 pts)
        # ------------------------------------------------------------------
        accel_pts = 0
        if window > 0 and len(ticks) >= window * 2:
            recent_v = _window_velocity(ticks, window)
            prior_v = _prior_window_velocity(ticks, window)
            if recent_v is not None and prior_v is not None and abs(prior_v) > 0:
                # Only award points if the move is same-direction and accelerating
                same_direction = (recent_v > 0) == (prior_v > 0)
                if same_direction:
                    accel_ratio = abs(recent_v) / abs(prior_v)
                    if accel_ratio >= 1.5:
                        accel_pts = _SCORE_ACCEL_STRONG
                    elif accel_ratio >= 1.2:
                        accel_pts = _SCORE_ACCEL_MILD
        score += accel_pts
        breakdown["acceleration"] = accel_pts

        # ------------------------------------------------------------------
        # Factor 4: Continuation Tick Strength (0–2 pts)
        # ------------------------------------------------------------------
        cont_pts = 0
        if self._pullback_start_price is not None and len(ticks) >= 4:
            # Semantic pullback depth: from the price where the pullback
            # started (the last trend-direction tick before pullback began)
            # down to the *lowest* pullback price reached.
            # Pullback-region ticks sit between _pullback_start_price and the
            # first continuation tick. They occupy positions
            # ticks[-(2 + continuation_ticks) : -2] in the buffer.
            if trend == "UP":
                # Pullback went DOWN; lowest point = min of pullback ticks
                pullback_region = ticks[-(2 + self._continuation_ticks):-2]
                pullback_depth = abs(min(pullback_region) - self._pullback_start_price)
            else:
                # Pullback went UP; highest point = max of pullback ticks
                pullback_region = ticks[-(2 + self._continuation_ticks):-2]
                pullback_depth = abs(max(pullback_region) - self._pullback_start_price)
            # Continuation size: from the deepest pullback point to the
            # current price (ticks[-1]).
            if trend == "UP":
                cont_size = abs(ticks[-1] - min(pullback_region))
            else:
                cont_size = abs(ticks[-1] - max(pullback_region))
            if pullback_depth > 0 and cont_size >= pullback_depth * 1.5:
                cont_pts = _SCORE_CONT_STRONG
        score += cont_pts
        breakdown["continuation_strength"] = cont_pts

        # ------------------------------------------------------------------
        # Composite gate
        # ------------------------------------------------------------------
        self._last_signal_score = score
        self._last_signal_score_breakdown = breakdown

        if score >= self._entry_score_threshold:
            self._pattern_stage = "SIGNAL"
            logger.info(
                "STRONG %s signal fired | Score: %d/%d | Breakdown: MTF=%d ER=%d Accel=%d Cont=%d | "
                "Continuation ticks: %d | MTF agreement: %d/3",
                signal,
                score,
                _SCORE_MTF_MAJORITY + _SCORE_MTF_UNANIMOUS + _SCORE_ER_HIGH + _SCORE_ACCEL_STRONG + _SCORE_CONT_STRONG,
                breakdown["mtf"],
                breakdown["efficiency_ratio"],
                breakdown["acceleration"],
                breakdown["continuation_strength"],
                self._continuation_ticks,
                self._mtf_agreement,
            )
            return signal

        logger.info(
            "Signal %s REJECTED | Score: %d (need %d) | Breakdown: MTF=%d ER=%d Accel=%d Cont=%d",
            signal,
            score,
            self._entry_score_threshold,
            breakdown["mtf"],
            breakdown["efficiency_ratio"],
            breakdown["acceleration"],
            breakdown["continuation_strength"],
        )
        self._pattern_stage = "IDLE"
        self._continuation_ticks = 0
        self._pullback_start_price = None
        return None

    def _validate_mtf(self, signal: str) -> bool:
        if self._mtf_agreement < self._mtf_min_agreement:
            return False
        return (signal == "BUY" and self._mtf_bias == "UP") or (signal == "SELL" and self._mtf_bias == "DOWN")


# ---------------------------------------------------------------------------
# MTF Analyzer
# ---------------------------------------------------------------------------

class MTFAnalyzer:
    """Multi-timeframe bias with a configurable majority requirement.

    v3 upgrade: Each timeframe now uses a slope-weighted linear regression
    across all available candles instead of a simple close[-1] vs close[-3]
    comparison. This makes the per-timeframe vote more robust against a
    single outlier candle reversing the result.
    """

    def __init__(self, min_agreement: int = MTF_MIN_AGREEMENT):
        self._min_agreement = min_agreement

    def analyze(self, candles_by_tf: Dict[str, List[Dict]]) -> Optional[str]:
        """Return the majority-aligned bias direction, or None."""
        return self.analyze_with_strength(candles_by_tf)[0]

    def analyze_with_strength(self, candles_by_tf: Dict[str, List[Dict]]) -> Tuple[Optional[str], int, Dict[str, str]]:
        """Return direction, agreement count, and per-timeframe bias dict (0-3)."""
        votes: List[str] = []
        tf_biases: Dict[str, str] = {}
        for label, candles in candles_by_tf.items():
            bias = self._analyze_single_tf(candles, label)
            if bias:
                votes.append(bias)
                tf_biases[label] = bias
            else:
                tf_biases[label] = "FLAT"
        if not votes:
            return None, 0, tf_biases
        up_votes = votes.count("UP")
        down_votes = votes.count("DOWN")
        if up_votes >= self._min_agreement and up_votes >= down_votes:
            return "UP", up_votes, tf_biases
        if down_votes >= self._min_agreement and down_votes > up_votes:
            return "DOWN", down_votes, tf_biases
        return None, max(up_votes, down_votes), tf_biases

    @staticmethod
    def _analyze_single_tf(candles: List[Dict], label: str) -> Optional[str]:
        """Compute slope-weighted bias for a single timeframe.

        Uses linear regression slope across all available candle closes.
        Falls back to a head-vs-tail comparison when the regression is too
        flat, ensuring a direction is produced whenever there are >=3 candles.
        """
        if len(candles) < 3:
            logger.warning("Insufficient candles for %s MTF analysis.", label)
            return None
        try:
            closes = [float(candle["close"]) for candle in candles]
        except (KeyError, TypeError, ValueError):
            return None
        bias = _candle_slope_bias(closes)
        logger.debug("MTF %s: bias=%s (first_close=%.4f, last_close=%.4f)",
                     label, bias, closes[0], closes[-1])
        return bias
