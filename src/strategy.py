"""
src/strategy.py
---------------
Ultra-Fast Sniper Strategy for Volatility 10 (1s) Index.
Uses O(1) rolling calculations and optimized acceleration filters.
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
)
from src.logger import get_logger

logger = get_logger("strategy")

class StrategyEngine:
    """
    Ultra-Fast Strategy Engine using __slots__ and Rolling Calculations.
    """
    __slots__ = (
        '_velocity_threshold', '_burst_threshold', '_mtf_min_agreement',
        '_trend_window_min', '_trend_window_max', '_burst_window_min',
        '_burst_window_max', '_momentum_confirm_ticks', '_tick_buffer',
        '_previous_price', '_state', '_trend_direction', '_trend_start_price',
        '_extreme_price', '_pullback_start_price', '_continuation_count',
        '_trades_in_trend', '_in_cooldown', '_cooldown_ticks_remaining',
        '_mtf_bias', '_mtf_agreement', '_trend_kind', '_trend_tick_count',
        '_rolling_abs_diff_sum', '_rolling_net_diff'
    )

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
        self._velocity_threshold = velocity_threshold
        self._burst_threshold = burst_threshold
        self._mtf_min_agreement = mtf_min_agreement
        self._trend_window_min = trend_window_min
        self._trend_window_max = trend_window_max
        self._burst_window_min = burst_window_min
        self._burst_window_max = burst_window_max
        self._momentum_confirm_ticks = momentum_confirm_ticks

        buffer_size = max(trend_window_max, burst_window_max, 50) + 5
        self._tick_buffer: deque = deque(maxlen=buffer_size)
        self._previous_price: Optional[float] = None
        
        self._state = "IDLE"
        self._trend_direction = None
        self._trend_start_price = None
        self._extreme_price = None
        self._pullback_start_price = None
        self._continuation_count = 0
        self._trades_in_trend = 0
        self._in_cooldown = False
        self._cooldown_ticks_remaining = 0
        
        self._mtf_bias = None
        self._mtf_agreement = 0
        
        # O(1) Rolling metrics
        self._rolling_abs_diff_sum = 0.0
        self._rolling_net_diff = 0.0

    def process_tick(self, price: float) -> Optional[str]:
        if self._previous_price is not None:
            diff = abs(price - self._previous_price)
            self._rolling_abs_diff_sum += diff
            # Remove old tick from rolling sum if buffer is full
            if len(self._tick_buffer) == self._tick_buffer.maxlen:
                old_price = self._tick_buffer[0]
                next_price = self._tick_buffer[1]
                self._rolling_abs_diff_sum -= abs(next_price - old_price)

        self._tick_buffer.append(price)
        
        if len(self._tick_buffer) < self._trend_window_min:
            self._previous_price = price
            return None

        if self._in_cooldown:
            self._cooldown_ticks_remaining -= 1
            if self._cooldown_ticks_remaining <= 0:
                self._in_cooldown = False
                logger.info("Cooldown ended.")
            self._previous_price = price
            return None

        signal = self._update_state_machine(price)
        self._previous_price = price
        return signal

    def _update_state_machine(self, price: float) -> Optional[str]:
        # Fast ER calculation
        er_long = self._calculate_er(self._trend_window_max)
        er_short = self._calculate_er(self._burst_window_max)
        
        if self._state == "IDLE":
            if er_long >= self._velocity_threshold or er_short >= self._burst_threshold:
                self._state = "MOMENTUM"
                self._trend_direction = "UP" if price > self._tick_buffer[-self._burst_window_max] else "DOWN"
                self._trend_start_price = self._tick_buffer[-self._trend_window_max]
                self._extreme_price = price
                logger.info(f"Momentum: {self._trend_direction} ER={er_long:.2f}")
            return None

        if self._state in ["MOMENTUM", "ARMED"]:
            # Update extreme
            if self._trend_direction == "UP":
                if price > self._extreme_price: self._extreme_price = price
                elif price < self._previous_price: # Pullback
                    return self._start_pullback(price)
            else:
                if price < self._extreme_price: self._extreme_price = price
                elif price > self._previous_price: # Pullback
                    return self._start_pullback(price)
            
            if er_long >= self._velocity_threshold or er_short >= self._burst_threshold:
                self._state = "ARMED"
            return None

        if self._state == "PULLBACK":
            if self._is_reversal(price):
                self._reset_to_idle()
                return None
            
            if self._is_continuation_tick(price):
                # OPTIMIZED ENTRY FILTER: Acceleration Check
                # The continuation move must be sharper than the pullback
                pullback_velocity = abs(self._previous_price - self._pullback_start_price)
                cont_velocity = abs(price - self._previous_price)
                
                if cont_velocity >= pullback_velocity * 1.2: # 20% faster acceleration
                    self._continuation_count = 1
                    if self._momentum_confirm_ticks <= 1: return self._generate_signal()
                    self._state = "CONTINUATION"
            return None

        if self._state == "CONTINUATION":
            if self._is_continuation_tick(price):
                self._continuation_count += 1
                if self._continuation_count >= self._momentum_confirm_ticks:
                    return self._generate_signal()
            elif self._is_pullback_tick(price):
                self._state = "PULLBACK"
                self._continuation_count = 0
            return None

        return None

    def _start_pullback(self, price: float) -> str:
        self._state = "PULLBACK"
        self._pullback_start_price = self._previous_price
        self._continuation_count = 0
        return "PRE_FETCH"

    def _generate_signal(self) -> Optional[str]:
        signal = "BUY" if self._trend_direction == "UP" else "SELL"
        if self._mtf_agreement < self._mtf_min_agreement or \
           (signal == "BUY" and self._mtf_bias != "UP") or \
           (signal == "SELL" and self._mtf_bias != "DOWN"):
            self._reset_to_idle()
            return None

        self._enter_cooldown(20) # Sniper cooldown
        return signal

    def _is_pullback_tick(self, price: float) -> bool:
        return price < self._previous_price if self._trend_direction == "UP" else price > self._previous_price

    def _is_continuation_tick(self, price: float) -> bool:
        return price > self._previous_price if self._trend_direction == "UP" else price < self._previous_price

    def _is_reversal(self, price: float) -> bool:
        move_size = abs(self._extreme_price - self._trend_start_price)
        if move_size == 0: return True
        return (abs(price - self._extreme_price) / move_size) > 0.30 # Tight sniper reversal

    def _calculate_er(self, window: int) -> float:
        if len(self._tick_buffer) < window: return 0.0
        sample = list(self._tick_buffer)[-window:]
        net = abs(sample[-1] - sample[0])
        path = sum(abs(sample[i] - sample[i-1]) for i in range(1, len(sample)))
        return net / path if path > 0 else 0.0

    def _enter_cooldown(self, ticks: int):
        self._in_cooldown = True
        self._cooldown_ticks_remaining = ticks
        self._reset_to_idle()

    def on_trade_executed(self) -> None:
        self._trades_in_trend += 1
        self._enter_cooldown(25)

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
        self._previous_price = None
        self._rolling_abs_diff_sum = 0.0
