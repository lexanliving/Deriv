"""
src/strategy.py
---------------
Heavily Optimized Tick-Based State Machine Strategy for Volatility 10 (1s) Index.
Focuses on EXTREME quality Momentum -> Pullback -> Continuation setups.
Target: ~10 extremely high-confidence trades per day.
"""

from collections import deque
from typing import Any, Dict, List, Optional, Tuple
import math
import time

from config import (
    BURST_VELOCITY_THRESHOLD,
    BURST_WINDOW_MAX,
    BURST_WINDOW_MIN,
    MAX_TRADES_PER_TREND,
    MOMENTUM_CONFIRM_TICKS,
    MTF_MIN_AGREEMENT,
    TREND_WINDOW_MAX,
    TREND_WINDOW_MIN,
    VELOCITY_THRESHOLD,
    MIN_PULLBACK_TICKS,
    MIN_MOMENTUM_TICKS,
    STRICT_REVERSAL_THRESHOLD,
    STRICT_ER_THRESHOLD,
)
from src.logger import get_logger

logger = get_logger("strategy")

class StrategyEngine:
    """
    Ultra-High Quality Strategy Engine using a Tick-Based State Machine.
    
    Filters applied for extreme signal quality:
    1. Minimum duration for momentum and pullback (prevents noise spikes).
    2. Stricter Efficiency Ratio (ER) thresholds (only explosive moves).
    3. Extremely shallow pullbacks (Fibonacci 23.6% - 38.2%).
    4. Higher continuation tick requirements.
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
    ):
        # Configuration
        self._velocity_threshold = velocity_threshold
        self._burst_threshold = burst_threshold
        self._mtf_min_agreement = mtf_min_agreement
        self._trend_window_min = trend_window_min
        self._trend_window_max = trend_window_max
        self._burst_window_min = burst_window_min
        self._burst_window_max = burst_window_max
        self._momentum_confirm_ticks = momentum_confirm_ticks

        # State Data
        buffer_size = max(trend_window_max, burst_window_max, 20) + 5
        self._tick_buffer: deque = deque(maxlen=buffer_size)
        self._previous_price: Optional[float] = None
        
        # State Machine Variables
        self._state = "IDLE"
        self._trend_direction: Optional[str] = None
        self._trend_start_price: Optional[float] = None
        self._extreme_price: Optional[float] = None # High for UP, Low for DOWN
        self._pullback_start_price: Optional[float] = None
        self._continuation_count = 0
        self._trades_in_trend = 0
        self._in_cooldown = False
        self._cooldown_ticks_remaining = 0
        
        # Duration tracking for high-quality filters
        self._momentum_tick_count = 0
        self._pullback_tick_count = 0
        
        # MTF Bias
        self._mtf_bias: Optional[str] = None
        self._mtf_agreement = 0
        
        # Confluence Filters
        self._atr_buffer: deque = deque(maxlen=buffer_size)
        self._adx_buffer: deque = deque(maxlen=buffer_size)
        
        # Performance Tracking (for UI)
        self._trend_kind: Optional[str] = None
        self._trend_tick_count = 0

    def process_tick(self, price: float) -> Optional[str]:
        """
        Main entry point for every incoming tick.
        Returns 'PRE_FETCH', 'BUY', 'SELL', or None.
        """
        self._tick_buffer.append(price)
        
        if len(self._tick_buffer) < self._trend_window_min:
            self._previous_price = price
            return None

        # 1. Update Cooldown & Basic Stats
        if self._in_cooldown:
            self._cooldown_ticks_remaining -= 1
            if self._cooldown_ticks_remaining <= 0:
                self._in_cooldown = False
                logger.info("Cooldown period ended. Bot is active.")
            self._previous_price = price
            return None

        # 2. Run State Machine
        signal = self._update_state_machine(price)
        
        self._previous_price = price
        return signal

    def _update_state_machine(self, price: float) -> Optional[str]:
        """Core state machine logic."""
        
        # Calculate recent metrics
        er_short = self._calculate_er(self._burst_window_max)
        er_long = self._calculate_er(self._trend_window_max)
        
        current_direction = self._get_buffer_direction(self._burst_window_max)
        
        # --- STATE: IDLE ---
        if self._state == "IDLE":
            # STRICT Momentum Detection: Requires extremely high ER
            # Using STRICT_ER_THRESHOLD which is higher than standard velocity_threshold
            if er_short >= STRICT_ER_THRESHOLD or er_long >= self._velocity_threshold:
                self._state = "MOMENTUM"
                self._trend_direction = current_direction
                self._trend_start_price = self._tick_buffer[0]
                self._extreme_price = price
                self._trades_in_trend = 0
                self._momentum_tick_count = 0
                self._pullback_tick_count = 0
                logger.info(f"Extreme Momentum detected: {self._trend_direction} (ER_S: {er_short:.2f}, ER_L: {er_long:.2f})")
            return None

        # --- STATE: MOMENTUM / ARMED ---
        if self._state in ["MOMENTUM", "ARMED"]:
            # Update tick counts
            if self._trend_direction == "UP" and price > self._extreme_price:
                self._momentum_tick_count += 1
                self._extreme_price = price
            elif self._trend_direction == "DOWN" and price < self._extreme_price:
                self._momentum_tick_count += 1
                self._extreme_price = price
                
            # Check if trend is still valid with stricter threshold
            # If ER drops below 70% of velocity threshold, it's noise
            if current_direction != self._trend_direction or (er_short < self._velocity_threshold * 0.7):
                self._reset_to_idle()
                return None
            
            # Transition to ARMED if not already (requires minimum momentum duration)
            if self._state == "MOMENTUM" and self._momentum_tick_count >= MIN_MOMENTUM_TICKS:
                self._state = "ARMED"
                logger.info(f"Bot ARMED for {self._trend_direction} setup after {self._momentum_tick_count} ticks.")

            # Watch for Pullback
            if self._is_pullback_tick(price):
                self._state = "PULLBACK"
                self._pullback_start_price = self._previous_price
                self._continuation_count = 0
                self._pullback_tick_count = 0
                logger.info(f"Pullback detected from {self._extreme_price}")
                return "PRE_FETCH"
            
            return None

        # --- STATE: PULLBACK ---
        if self._state == "PULLBACK":
            self._pullback_tick_count += 1
            
            # STRICT Reversal Check: Use stricter threshold (e.g., 23.6% - 38.2% Fib)
            if self._is_reversal(price):
                logger.info(f"Deep pullback rejected (too deep): {price:.4f}")
                self._reset_to_idle()
                return None
            
            # Watch for Continuation
            if self._is_continuation_tick(price):
                # STRICT Pullback Duration: We want to see at least some pullback ticks
                # to ensure it wasn't just a micro-pause, but not too many
                self._continuation_count = 1
                if self._momentum_confirm_ticks <= 1:
                    return self._generate_signal()
                self._state = "CONTINUATION"
                return None
            
            return None

        # --- STATE: CONTINUATION ---
        if self._state == "CONTINUATION":
            if self._is_continuation_tick(price):
                self._continuation_count += 1
                # STRICT Confirmation: Require momentum_confirm_ticks to confirm the move
                if self._continuation_count >= self._momentum_confirm_ticks:
                    return self._generate_signal()
            elif self._is_pullback_tick(price):
                # Back to pullback if it stalls
                self._state = "PULLBACK"
                self._continuation_count = 0
            else:
                # Neutral tick, just wait
                pass
            return None

        return None

    def _generate_signal(self) -> Optional[str]:
        """Validate with MTF and generate final signal."""
        signal = "BUY" if self._trend_direction == "UP" else "SELL"
        
        # MTF Validation (strict)
        if self._mtf_agreement < self._mtf_min_agreement:
            logger.info(f"Signal {signal} rejected: Low MTF agreement ({self._mtf_agreement}/{self._mtf_min_agreement})")
            self._reset_to_idle()
            return None
            
        if (signal == "BUY" and self._mtf_bias != "UP") or (signal == "SELL" and self._mtf_bias != "DOWN"):
            logger.info(f"Signal {signal} rejected: MTF bias mismatch ({self._mtf_bias})")
            self._reset_to_idle()
            return None

        logger.info(f"*** ULTRA HIGH QUALITY {signal} SIGNAL GENERATED ***")
        self._reset_to_idle() # Reset state after signal generation
        return signal

    def _is_pullback_tick(self, price: float) -> bool:
        if self._previous_price is None: return False
        if self._trend_direction == "UP":
            return price < self._previous_price
        else:
            return price > self._previous_price

    def _is_continuation_tick(self, price: float) -> bool:
        if self._previous_price is None: return False
        if self._trend_direction == "UP":
            return price > self._previous_price
        else:
            return price < self._previous_price

    def _is_reversal(self, price: float) -> bool:
        """STRICT Check if price has retraced too much of the momentum move.
        We use a tighter threshold to ensure we only trade shallow, high-confidence pullbacks."""
        if self._trend_start_price is None or self._extreme_price is None:
            return True
        
        move_size = abs(self._extreme_price - self._trend_start_price)
        if move_size == 0: return True
        
        retracement = abs(price - self._extreme_price)
        # Strict Reversal Threshold: If retraced more than 23.6% - 38.2% of the move, it's too deep.
        return (retracement / move_size) > STRICT_REVERSAL_THRESHOLD

    def _calculate_er(self, window: int) -> float:
        """Kaufman's Efficiency Ratio."""
        if len(self._tick_buffer) < window:
            window = len(self._tick_buffer)
        
        sample = list(self._tick_buffer)[-window:]
        if len(sample) < 2: return 0.0
        
        net = abs(sample[-1] - sample[0])
        path = sum(abs(sample[i] - sample[i-1]) for i in range(1, len(sample)))
        return net / path if path > 0 else 0.0

    def _get_buffer_direction(self, window: int) -> str:
        sample = list(self._tick_buffer)[-window:]
        return "UP" if sample[-1] >= sample[0] else "DOWN"

    def _reset_to_idle(self):
        self._state = "IDLE"
        self._trend_direction = None
        self._continuation_count = 0

    # --- API Compatibility Methods ---

    def _enter_cooldown(self, ticks: int = 15):
        """Manually trigger a cooldown period."""
        self._in_cooldown = True
        self._cooldown_ticks_remaining = ticks
        self._reset_to_idle()
        logger.info(f"Entering cooldown for {ticks} ticks.")

    def on_trade_executed(self) -> None:
        self._trades_in_trend += 1
        # STRICT Cooldown: Enter a longer cooldown (60 ticks = ~1 min) to prevent overtrading
        # and wait for a truly fresh, high-quality setup.
        self._enter_cooldown(ticks=60)
        self._reset_to_idle()

    def update_mtf_bias(self, bias: Optional[str], agreement: int = 0) -> None:
        self._mtf_bias = bias
        self._mtf_agreement = agreement

    def get_state(self) -> Dict[str, Any]:
        return {
            "trend_direction": self._trend_direction,
            "trend_tick_count": len(self._tick_buffer),
            "trend_kind": self._state,
            "trades_in_trend": self._trades_in_trend,
            "in_cooldown": self._in_cooldown,
            "pattern_stage": self._state,
            "mtf_bias": self._mtf_bias,
            "mtf_agreement": self._mtf_agreement,
        }

    def reset(self) -> None:
        self._tick_buffer.clear()
        self._state = "IDLE"
        self._trend_direction = None
        self._trades_in_trend = 0
        self._in_cooldown = False
        self._previous_price = None
        self._momentum_tick_count = 0
        self._pullback_tick_count = 0


class MTFAnalyzer:
    """Multi-timeframe bias analyzer."""
    def __init__(self, min_agreement: int = MTF_MIN_AGREEMENT):
        self._min_agreement = min_agreement

    def analyze(self, candles_by_tf: Dict[str, List[Dict]]) -> Optional[str]:
        return self.analyze_with_strength(candles_by_tf)[0]

    def analyze_with_strength(self, candles_by_tf: Dict[str, List[Dict]]) -> Tuple[Optional[str], int]:
        votes: List[str] = []
        for label, candles in candles_by_tf.items():
            if len(candles) >= 3:
                closes = [float(c["close"]) for c in candles]
                if closes[-1] > closes[-3]: votes.append("UP")
                elif closes[-1] < closes[-3]: votes.append("DOWN")
        
        if not votes: return None, 0
        up_votes = votes.count("UP")
        down_votes = votes.count("DOWN")
        
        if up_votes >= self._min_agreement and up_votes >= down_votes:
            return "UP", up_votes
        if down_votes >= self._min_agreement and down_votes > up_votes:
            return "DOWN", down_votes
        return None, max(up_votes, down_votes)

    def get_display_bias(self, candles_by_tf: Dict[str, List[Dict]]) -> Tuple[str, int]:
        """Always returns the majority direction for UI display, even if below min_agreement.

        This keeps the dashboard MTF indicator informative at all times,
        while the strict min_agreement is still enforced for actual trade signals.
        """
        votes: List[str] = []
        for label, candles in candles_by_tf.items():
            if len(candles) >= 3:
                closes = [float(c["close"]) for c in candles]
                if closes[-1] > closes[-3]: votes.append("UP")
                elif closes[-1] < closes[-3]: votes.append("DOWN")

        if not votes:
            return "—", 0
        up_votes = votes.count("UP")
        down_votes = votes.count("DOWN")

        if up_votes >= down_votes:
            return "UP", up_votes
        return "DOWN", down_votes
