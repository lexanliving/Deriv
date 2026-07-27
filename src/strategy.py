"""Early-capture trend-following strategy for synthetic-index tick data.

v4 upgrade — Early Trend Capture & Non-Blocking Higher-Timeframe Context

v3 was so selective that it produced zero trades during textbook
directional markets. Diagnosis of live sessions showed four compounding
causes:

- The candle-based MTF bias (5m/15m/30m regression) lags a fresh
  intraday move by many minutes. During a strong live DOWN move the
  MTF still voted UP, and v3 treated any MTF disagreement as a hard
  block — every SELL signal in the whole move was rejected.

- MTF only refreshed every 60s (15s in-setup), so the vote flipped
  long after the move had finished.

- The scoring gate demanded momentum acceleration (>=1.2x the prior
  window). A clean constant-velocity trend — precisely the market that
  works best for this trade type — has an acceleration ratio near 1.0
  and scored 0 on that factor, so it almost never reached 8/14.

- The entry pattern required an exact pullback -> 2-continuation-tick
  sequence. Trends that grind directionally without that exact
  alternation never even produced a candidate signal.

v4 design

- Direction comes from the live tick flow; higher timeframes are context,
  not a veto.

- A tick-derived micro-bias ("1m" pseudo-timeframe) is computed on
  every tick from the last MICRO_BIAS_WINDOW ticks: EMA-smoothed slope
  plus efficiency-ratio direction. It reacts within seconds instead of
  minutes, so the bot sees the move in the first leg — the "early
  stage" capture requirement.

- Candle MTF (5m/15m/30m) contributes score: aligned = bonus points,
  mixed = neutral, and only a unanimous 3-of-3 vote against the
  signal direction is a hard block (fighting all higher timeframes at
  once is the one trade this strategy must never take).

- Two entry modes, whichever comes first:

  a) IMMEDIATE — a young trend (detected within EARLY_TREND_MAX_AGE
     ticks of birth) whose composite score clears the gate fires at once.
     This is what catches the markets in the reference screenshots: a
     strong efficient push is entered during its first leg, without
     waiting for a pullback that may never come.

  b) PULLBACK — the classic pullback -> continuation entry, kept for
     mature trends, loosened to require MOMENTUM_CONFIRM_TICKS
     (default 1) continuation ticks.

Scoring v4 (0-14, default threshold 7):

Factor 1 — Trend Quality / Efficiency Ratio (0-5)
  5 pts ER >= 0.80 (near-straight push)
  4 pts ER >= 0.70
  2 pts ER >= 0.60
  0 pts below

Factor 2 — Higher-Timeframe Context (0-4, hard block only on 3-of-3 against)
  4 pts unanimous 3-of-3 candle MTF agrees with signal
  3 pts 2-of-3 agrees
  1 pt  mixed / no consensus (the live move outranks stale candles)
  0 pts 2-of-3 against
  HARD BLOCK if 3-of-3 against

Factor 3 — Momentum Consistency (0-3)  [replaces v3 acceleration]
  3 pts >= 75% of ticks in the window move with the trend
  2 pts >= 60%
  0 pts below — a steady constant-velocity trend earns full points
  here; no acceleration spike is required.

Factor 4 — Early Capture (0-2)
  2 pts trend age <= EARLY_TREND_MAX_AGE ticks (entering the first leg)
  1 pt  trend age <= 2x EARLY_TREND_MAX_AGE
  0 pts late in the move

Micro-bias agreement (+0 / -2): if the tick micro-bias disagrees with
the signal direction the score is penalised by 2 — the very-short-term
flow should support the entry.

Presets: Aggressive 5, Balanced 7, Conservative 9.

All thresholds remain constructor parameters so the dashboard sensitivity
presets work without code changes. The engine keeps the exact public
interface used by TradingEngine and the regression tests:
process_tick / get_state / on_trade_executed / on_signal_skipped /
update_mtf_bias / reset, and emits "PRE_FETCH" so the proposal-prefetch
loop can keep a quote warm (now emitted as soon as a trend is detected,
not only on a pullback, so the buy path is a single WebSocket round trip).
"""

import math
from collections import deque
from typing import Any, Dict, List, Optional, Tuple

from config import (
    ACCELERATION_MIN_RATIO,
    BURST_VELOCITY_THRESHOLD,
    BURST_WINDOW_MAX,
    BURST_WINDOW_MIN,
    EARLY_TREND_MAX_AGE,
    MAX_TICK_VOLATILITY,
    MAX_TRADES_PER_TREND,
    MICRO_BIAS_WINDOW,
    MIN_TICK_VOLATILITY,
    MOMENTUM_CONFIRM_TICKS,
    MTF_MIN_AGREEMENT,
    TREND_WINDOW_MAX,
    TREND_WINDOW_MIN,
    VELOCITY_THRESHOLD,
    VOLATILITY_WINDOW,
)
from src.logger import get_logger

logger = get_logger("strategy")

PATTERN_STAGES = ["IDLE", "TREND", "PULLBACK", "MOMENTUM", "SIGNAL"]

# ---------------------------------------------------------------------------
# Scoring constants (v4)
# ---------------------------------------------------------------------------
_SCORE_ER_HIGH = 5        # ER >= 0.80
_SCORE_ER_GOOD = 4        # ER >= 0.70
_SCORE_ER_OK = 2          # ER >= 0.60

_SCORE_MTF_UNANIMOUS = 4  # 3-of-3 candle MTF agrees
_SCORE_MTF_MAJORITY = 3   # 2-of-3 agrees
_SCORE_MTF_NEUTRAL = 1    # mixed / no consensus

_SCORE_CONSISTENCY_HIGH = 3   # >= 75% of ticks with the trend
_SCORE_CONSISTENCY_GOOD = 2   # >= 60%

_SCORE_EARLY_STRONG = 2   # trend age <= EARLY_TREND_MAX_AGE
_SCORE_EARLY_MILD = 1     # trend age <= 2x EARLY_TREND_MAX_AGE

_PENALTY_MICRO_DISAGREE = 2   # micro-bias opposes the signal

ENTRY_SCORE_THRESHOLD = 7     # Balanced default (max possible 14)
SCORE_MAX = 14


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
    before the most recent `window` ticks (non-overlapping)."""
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


def _tick_consistency(sample: List[float], direction: str) -> float:
    """Fraction of tick-to-tick moves that agree with `direction`.

    Flat (zero-delta) ticks are ignored. Returns 0.0 when there are no
    non-flat deltas.
    """
    if len(sample) < 2:
        return 0.0

    with_trend = 0
    against = 0

    for i in range(1, len(sample)):
        delta = sample[i] - sample[i - 1]

        if delta == 0:
            continue

        if (delta > 0) == (direction == "UP"):
            with_trend += 1
        else:
            against += 1

    total = with_trend + against

    if total == 0:
        return 0.0

    return with_trend / total


def _micro_bias(ticks: List[float], window: int) -> Optional[str]:
    """Very-short-term (seconds-scale) direction from the live tick flow.

    Combines net displacement direction with a minimum efficiency so that
    pure noise doesn't produce a bias. Returns 'UP', 'DOWN', or None.
    """
    if len(ticks) < max(4, window // 4):
        return None

    sample = ticks[-window:] if len(ticks) >= window else list(ticks)

    net = sample[-1] - sample[0]
    if net == 0:
        return None

    er = _efficiency_ratio(sample)
    if er < 0.25:
        return None  # churning, no meaningful short-term direction

    return "UP" if net > 0 else "DOWN"


def _candle_slope_bias(closes: List[float]) -> Optional[str]:
    """Compute a slope-weighted bias from a candle close series.

    1. Fit a linear regression slope across all closes (robust against a
       single outlier candle).
    2. If the regression slope is clearly directional (>0.01% of mean
       price), use it.
    3. Otherwise, fall back to comparing the average of the last 3 closes
       vs the average of the first 3 closes.

    Returns 'UP', 'DOWN', or None only when truly insufficient data.
    """
    n = len(closes)

    if n < 3:
        return None

    x_mean = (n - 1) / 2.0
    y_mean = sum(closes) / n

    numerator = sum((i - x_mean) * (closes[i] - y_mean) for i in range(n))
    denominator = sum((i - x_mean) ** 2 for i in range(n))

    if denominator == 0:
        return None

    slope = numerator / denominator

    if y_mean == 0:
        return None

    relative_slope = abs(slope) / abs(y_mean)

    if relative_slope >= 0.0001:
        return "UP" if slope > 0 else "DOWN"

    head = sum(closes[:3]) / 3
    tail = sum(closes[-3:]) / 3

    if tail > head:
        return "UP"
    elif tail < head:
        return "DOWN"

    return None


# ---------------------------------------------------------------------------
# Strategy Engine
# ---------------------------------------------------------------------------
class StrategyEngine:
    """Generate early-capture trend signals scored across four factors.

    v4: direction is driven by the live tick flow; higher-timeframe candle
    context adds/subtracts score but only a unanimous vote against blocks.
    Two entry modes: IMMEDIATE (young, high-quality trend) and PULLBACK
    (continuation after a counter-move).
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
        early_trend_max_age: int = EARLY_TREND_MAX_AGE,
        micro_bias_window: int = MICRO_BIAS_WINDOW,
    ):
        self._velocity_threshold = velocity_threshold
        self._burst_threshold = burst_threshold
        self._mtf_min_agreement = mtf_min_agreement
        self._trend_window_min = trend_window_min
        self._trend_window_max = trend_window_max
        self._burst_window_min = burst_window_min
        self._burst_window_max = burst_window_max
        self._momentum_confirm_ticks = momentum_confirm_ticks
        self._entry_score_threshold = entry_score_threshold
        self._early_trend_max_age = early_trend_max_age
        self._micro_bias_window = micro_bias_window

        buffer_size = max(trend_window_max, burst_window_max, micro_bias_window) * 2 + 4
        self._tick_buffer: deque = deque(maxlen=buffer_size)

        self._trend_direction: Optional[str] = None
        self._trend_tick_count = 0
        self._trend_kind: Optional[str] = None  # "classic" or "burst"
        self._trend_age = 0            # ticks since this trend direction was born
        self._trades_in_trend = 0
        self._in_cooldown = False
        self._cooldown_direction: Optional[str] = None

        self._pattern_stage = "IDLE"
        self._continuation_ticks = 0
        self._previous_price: Optional[float] = None

        self._mtf_bias: Optional[str] = None
        self._mtf_agreement = 0
        self._mtf_tf_biases: Dict[str, str] = {}
        self._micro: Optional[str] = None

        self._last_signal_score: int = 0
        self._last_signal_score_breakdown: Dict[str, int] = {}
        self._last_entry_mode: Optional[str] = None
        self._pullback_start_price: Optional[float] = None

        # One PRE_FETCH per trend birth keeps the prefetch loop warm without
        # spamming; the loop itself re-checks staleness continuously.
        self._prefetch_emitted_for_trend = False

        # Once an immediate-mode signal for this trend has been generated
        # and then skipped (e.g. held back by pacing), the immediate
        # fast-path is disabled for the rest of this trend so the engine
        # can't just re-fire the same stale signal the instant pacing
        # opens up - it must earn a fresh pullback/continuation setup.
        self._immediate_disabled_for_trend = False

    # ------------------------------------------------------------------
    # Public interface (used by TradingEngine and tests)
    # ------------------------------------------------------------------
    def process_tick(self, price: float) -> Optional[str]:
        """Process one tick. Returns 'BUY', 'SELL', 'PRE_FETCH', or None."""
        self._tick_buffer.append(price)

        if len(self._tick_buffer) < self._burst_window_min:
            self._previous_price = price
            return None

        ticks = list(self._tick_buffer)
        self._micro = _micro_bias(ticks, self._micro_bias_window)

        self._update_trend(ticks)

        if self._trend_direction is None or self._in_cooldown:
            self._previous_price = price
            return None

        if self._trades_in_trend >= MAX_TRADES_PER_TREND:
            self._enter_cooldown()
            self._previous_price = price
            return None

        signal = self._evaluate_entry(price, ticks)
        self._previous_price = price

        return signal

    def on_trade_executed(self) -> None:
        self._trades_in_trend += 1
        self._pattern_stage = "IDLE"
        self._continuation_ticks = 0
        self._pullback_start_price = None

        # Once an order has been taken in this trend, do not allow the same
        # immediate early-capture condition to re-fire for the rest of this
        # trend. Further entries in the same trend must be fresh pullback/
        # continuation setups; a brand-new trend resets this flag.
        self._immediate_disabled_for_trend = True
        self._last_entry_mode = None

        logger.info(
            "Trade executed. Trades in current trend: %s",
            self._trades_in_trend,
        )

    def on_signal_skipped(self) -> None:
        """Call when a signal cleared the scoring gate but was NOT acted on
        (e.g. held back by an external pacing/cooldown check). Resets the
        pattern state back to IDLE so the engine looks for the next setup,
        without touching trades_in_trend/cooldown — no trade happened, so
        the "1 trade per trend" budget isn't spent.

        If the skipped signal came from the immediate early-capture path,
        that path is disabled for the rest of this trend: otherwise the
        same trend_age/trend_direction would let it re-fire on the very
        next tick (and every tick after) with nothing new having actually
        happened, so the instant pacing opens up it "fires" without ever
        waiting for a genuine new setup. Pullback-mode signals don't need
        this: dropping to IDLE already forces a real pullback and
        continuation to re-form before another signal can fire.
        """
        if self._last_entry_mode == "immediate":
            self._immediate_disabled_for_trend = True

        self._pattern_stage = "IDLE"
        self._continuation_ticks = 0
        self._pullback_start_price = None

    def update_mtf_bias(
        self,
        bias: Optional[str],
        agreement: int = 0,
        tf_biases: Optional[Dict[str, str]] = None,
    ) -> None:
        self._mtf_bias = bias
        self._mtf_agreement = agreement

        if tf_biases is not None:
            self._mtf_tf_biases = tf_biases

    def get_state(self) -> Dict[str, Any]:
        tf_biases = dict(self._mtf_tf_biases)

        if self._micro:
            tf_biases["1m"] = self._micro

        return {
            "trend_direction": self._trend_direction,
            "trend_tick_count": self._trend_tick_count,
            "trend_kind": self._trend_kind,
            "trend_age": self._trend_age,
            "trades_in_trend": self._trades_in_trend,
            "in_cooldown": self._in_cooldown,
            "pattern_stage": self._pattern_stage,
            "mtf_bias": self._mtf_bias,
            "mtf_agreement": self._mtf_agreement,
            "mtf_tf_biases": tf_biases,
            "micro_bias": self._micro,
            "last_signal_score": self._last_signal_score,
            "last_signal_score_breakdown": dict(self._last_signal_score_breakdown),
            "last_entry_mode": self._last_entry_mode,
        }

    def reset(self) -> None:
        self._tick_buffer.clear()

        self._trend_direction = None
        self._trend_tick_count = 0
        self._trend_kind = None
        self._trend_age = 0
        self._trades_in_trend = 0
        self._in_cooldown = False
        self._cooldown_direction = None

        self._pattern_stage = "IDLE"
        self._continuation_ticks = 0
        self._previous_price = None

        self._mtf_bias = None
        self._mtf_agreement = 0
        self._mtf_tf_biases = {}
        self._micro = None

        self._last_signal_score = 0
        self._last_signal_score_breakdown = {}
        self._last_entry_mode = None
        self._pullback_start_price = None

        self._prefetch_emitted_for_trend = False
        self._immediate_disabled_for_trend = False

    # ------------------------------------------------------------------
    # Trend detection
    # ------------------------------------------------------------------
    def _update_trend(self, ticks: List[float]) -> None:
        # 1) Fast path: a short, high-conviction burst. Checked first so
        #    strong, quick momentum doesn't have to wait for the slower
        #    classic window to fill.
        detected = self._scan_windows(
            ticks,
            self._burst_window_min,
            self._burst_window_max,
            self._burst_threshold,
        )
        kind = "burst"

        # 2) Classic path: the slower window with a lower ER bar.
        if detected is None:
            detected = self._scan_windows(
                ticks,
                self._trend_window_min,
                self._trend_window_max,
                self._velocity_threshold,
            )
            kind = "classic"

        if detected is not None and not self._passes_regime_filters(ticks, detected[1]):
            detected = None

        if detected is None:
            if self._trend_direction is not None:
                logger.debug("Trend %s dissolved.", self._trend_direction)

            self._trend_direction = None
            self._trend_tick_count = 0
            self._trend_kind = None
            self._trend_age = 0
            self._pattern_stage = "IDLE"
            self._continuation_ticks = 0
            self._pullback_start_price = None
            self._prefetch_emitted_for_trend = False
            self._immediate_disabled_for_trend = False

            # The move genuinely dissolved: release any budget cooldown so
            # the next fresh trend starts with a full budget.
            self._in_cooldown = False
            self._cooldown_direction = None

            return

        direction, window = detected

        if direction != self._trend_direction:
            # A trend that was budget-capped (cooldown) must not be treated as
            # "new" just because _enter_cooldown cleared the direction: only a
            # genuine direction flip (or dissolution first) resets the budget.
            if self._in_cooldown and direction == self._cooldown_direction:
                return

            self._trend_direction = direction
            self._trend_age = 0
            self._trades_in_trend = 0
            self._in_cooldown = False
            self._cooldown_direction = None
            self._pattern_stage = "TREND"
            self._continuation_ticks = 0
            self._pullback_start_price = None
            self._prefetch_emitted_for_trend = False
            self._immediate_disabled_for_trend = False

            logger.info(
                "New %s trend (%s path): %s-tick window",
                direction,
                kind,
                window,
            )

        else:
            self._trend_age += 1

            if self._pattern_stage == "IDLE":
                self._pattern_stage = "TREND"

        self._trend_tick_count = window
        self._trend_kind = kind

    @staticmethod
    def _passes_regime_filters(ticks: List[float], window: int) -> bool:
        """Volatility-regime and acceleration checks on a candidate trend.

        Both are opt-in via config (disabled values: MIN/MAX_TICK_VOLATILITY
        = None, ACCELERATION_MIN_RATIO = 0). v4 ships with them DISABLED:
        the hand-picked volatility band and the acceleration requirement
        were rejecting clean constant-velocity trends. Calibrate from your
        own tick history before re-enabling.
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
                if abs(prior) > 0:
                    if (recent < 0) != (prior < 0):
                        return False  # direction reversed between windows

                    if abs(recent) < abs(prior) * ACCELERATION_MIN_RATIO:
                        return False  # decelerating faster than allowed

        return True

    @staticmethod
    def _scan_windows(
        ticks: List[float],
        window_min: int,
        window_max: int,
        threshold: float,
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
        self._cooldown_direction = self._trend_direction
        self._trend_direction = None
        self._trend_kind = None
        self._trend_age = 0
        self._pattern_stage = "IDLE"

    # ------------------------------------------------------------------
    # Entry evaluation: immediate early-capture OR pullback continuation
    # ------------------------------------------------------------------
    def _evaluate_entry(self, price: float, ticks: List[float]) -> Optional[str]:
        trend = self._trend_direction
        assert trend in ("UP", "DOWN")

        signal = "BUY" if trend == "UP" else "SELL"

        # Mode (a): IMMEDIATE early-capture entry. A young trend that scores
        # through the gate fires at once — no pullback required. This is what
        # catches a strong move during its first leg.
        if (
            not self._immediate_disabled_for_trend
            and self._trend_age <= self._early_trend_max_age
            and self._pattern_stage in ("TREND", "IDLE")
        ):
            score, breakdown, blocked = self._score_signal(
                signal, ticks, entry_mode="immediate"
            )

            self._last_signal_score = score
            self._last_signal_score_breakdown = breakdown

            if not blocked and score >= self._entry_score_threshold:
                self._pattern_stage = "SIGNAL"
                self._last_entry_mode = "immediate"

                logger.info(
                    "IMMEDIATE %s entry | Score %d/%d | %s | trend_age=%d",
                    signal,
                    score,
                    SCORE_MAX,
                    breakdown,
                    self._trend_age,
                )

                return signal

        # Emit one PRE_FETCH per trend so the background loop warms a quote
        # even before a pullback happens (execution speed: signal -> buy is
        # then a single buy round trip).
        prefetch: Optional[str] = None

        if not self._prefetch_emitted_for_trend:
            self._prefetch_emitted_for_trend = True
            prefetch = "PRE_FETCH"

        # Mode (b): pullback -> continuation for mature trends.
        pattern_signal = self._update_pattern(price, ticks, trend, signal)

        if pattern_signal is not None:
            return pattern_signal

        return prefetch

    def _update_pattern(
        self,
        price: float,
        ticks: List[float],
        trend: str,
        signal: str,
    ) -> Optional[str]:
        if self._previous_price is None or price == self._previous_price:
            return None

        tick_direction = "UP" if price > self._previous_price else "DOWN"
        pullback_direction = "DOWN" if trend == "UP" else "UP"

        if self._pattern_stage in ("IDLE", "TREND"):
            if tick_direction == pullback_direction:
                self._pattern_stage = "PULLBACK"
                self._continuation_ticks = 0
                self._pullback_start_price = self._previous_price

            return None

        if self._pattern_stage == "PULLBACK":
            if tick_direction == pullback_direction:
                return None  # A deeper pullback still counts as one setup.

            self._continuation_ticks = 1
            return self._signal_if_confirmed(trend, signal, ticks)

        if self._pattern_stage == "MOMENTUM":
            if tick_direction == trend:
                self._continuation_ticks += 1
                return self._signal_if_confirmed(trend, signal, ticks)

            if tick_direction == pullback_direction:
                self._pattern_stage = "PULLBACK"
                self._pullback_start_price = self._previous_price
            else:
                self._pattern_stage = "TREND"

            self._continuation_ticks = 0

        return None

    def _signal_if_confirmed(
        self,
        trend: str,
        signal: str,
        ticks: List[float],
    ) -> Optional[str]:
        if self._continuation_ticks < self._momentum_confirm_ticks:
            self._pattern_stage = "MOMENTUM"
            return None

        score, breakdown, blocked = self._score_signal(
            signal, ticks, entry_mode="pullback"
        )

        self._last_signal_score = score
        self._last_signal_score_breakdown = breakdown

        if not blocked and score >= self._entry_score_threshold:
            self._pattern_stage = "SIGNAL"
            self._last_entry_mode = "pullback"

            logger.info(
                "PULLBACK %s entry | Score %d/%d | %s | continuation=%d",
                signal,
                score,
                SCORE_MAX,
                breakdown,
                self._continuation_ticks,
            )

            return signal

        logger.info(
            "Signal %s rejected | Score %d (need %d) | %s%s",
            signal,
            score,
            self._entry_score_threshold,
            breakdown,
            " | HARD BLOCK: unanimous HTF against" if blocked else "",
        )

        self._pattern_stage = "TREND"
        self._continuation_ticks = 0
        self._pullback_start_price = None

        return None

    # ------------------------------------------------------------------
    # v4 composite scoring
    # ------------------------------------------------------------------
    def _score_signal(
        self,
        signal: str,
        ticks: List[float],
        entry_mode: str,
    ) -> Tuple[int, Dict[str, int], bool]:
        """Return (score, breakdown, hard_blocked)."""
        direction = "UP" if signal == "BUY" else "DOWN"
        breakdown: Dict[str, int] = {}

        # Factor 2 first: unanimous higher-timeframe vote AGAINST is the only
        # hard block. Everything else contributes score.
        opposite = "DOWN" if direction == "UP" else "UP"

        candle_votes = [
            v
            for k, v in self._mtf_tf_biases.items()
            if k != "1m" and v in ("UP", "DOWN")
        ]

        against_votes = sum(1 for v in candle_votes if v == opposite)
        with_votes = sum(1 for v in candle_votes if v == direction)

        if len(candle_votes) >= 3 and against_votes == len(candle_votes):
            breakdown["htf"] = 0
            return 0, breakdown, True  # fighting every higher timeframe

        if with_votes >= 3:
            htf_pts = _SCORE_MTF_UNANIMOUS
        elif with_votes == 2 and with_votes > against_votes:
            htf_pts = _SCORE_MTF_MAJORITY
        elif against_votes >= 2 and against_votes > with_votes:
            htf_pts = 0
        else:
            htf_pts = _SCORE_MTF_NEUTRAL

        breakdown["htf"] = htf_pts

        # Factor 1: trend quality (efficiency ratio of the detected window).
        window = self._trend_tick_count
        er_pts = 0
        er = 0.0

        if window > 1 and len(ticks) >= window:
            er = _efficiency_ratio(ticks[-window:])

            if er >= 0.80:
                er_pts = _SCORE_ER_HIGH
            elif er >= 0.70:
                er_pts = _SCORE_ER_GOOD
            elif er >= 0.60:
                er_pts = _SCORE_ER_OK

        breakdown["quality"] = er_pts

        # Factor 3: momentum consistency (fraction of ticks with the trend).
        cons_pts = 0

        if window > 1 and len(ticks) >= window:
            consistency = _tick_consistency(ticks[-window:], direction)

            if consistency >= 0.75:
                cons_pts = _SCORE_CONSISTENCY_HIGH
            elif consistency >= 0.60:
                cons_pts = _SCORE_CONSISTENCY_GOOD

        breakdown["consistency"] = cons_pts

        # Factor 4: early capture.
        if self._trend_age <= self._early_trend_max_age:
            early_pts = _SCORE_EARLY_STRONG
        elif self._trend_age <= 2 * self._early_trend_max_age:
            early_pts = _SCORE_EARLY_MILD
        else:
            early_pts = 0

        breakdown["early"] = early_pts

        # Micro-bias penalty: the seconds-scale flow should not oppose entry.
        micro_penalty = 0

        if self._micro is not None and self._micro != direction:
            micro_penalty = _PENALTY_MICRO_DISAGREE

        breakdown["micro_penalty"] = -micro_penalty

        score = max(0, er_pts + htf_pts + cons_pts + early_pts - micro_penalty)

        return score, breakdown, False

    def _validate_mtf(self, signal: str) -> bool:
        """Legacy helper retained for compatibility with existing tests:
        True when the candle MTF majority agrees with the signal."""
        if self._mtf_agreement < self._mtf_min_agreement:
            return False

        return (signal == "BUY" and self._mtf_bias == "UP") or (
            signal == "SELL" and self._mtf_bias == "DOWN"
        )


# ---------------------------------------------------------------------------
# MTF Analyzer
# ---------------------------------------------------------------------------
class MTFAnalyzer:
    """Multi-timeframe bias with a configurable majority requirement.

    Each timeframe uses a slope-weighted linear regression across all
    available candles (robust against a single outlier candle), with a
    head-vs-tail fallback when the regression is too flat.

    v4 note: the analyzer's output is *context* for the scoring gate, not
    a hard veto — see StrategyEngine._score_signal.
    """

    def __init__(self, min_agreement: int = MTF_MIN_AGREEMENT):
        self._min_agreement = min_agreement

    def analyze(self, candles_by_tf: Dict[str, List[Dict]]) -> Optional[str]:
        """Return the majority-aligned bias direction, or None."""
        return self.analyze_with_strength(candles_by_tf)[0]

    def analyze_with_strength(
        self, candles_by_tf: Dict[str, List[Dict]]
    ) -> Tuple[Optional[str], int, Dict[str, str]]:
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
        """Compute slope-weighted bias for a single timeframe."""
        if len(candles) < 3:
            logger.warning("Insufficient candles for %s MTF analysis.", label)
            return None

        try:
            closes = [float(candle["close"]) for candle in candles]
        except (KeyError, TypeError, ValueError):
            return None

        bias = _candle_slope_bias(closes)

        logger.debug(
            "MTF %s: bias=%s (first_close=%.4f, last_close=%.4f)",
            label,
            bias,
            closes[0],
            closes[-1],
        )

        return bias