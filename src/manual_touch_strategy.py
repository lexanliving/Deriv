"""src/manual_touch_strategy.py — fast manual reversal trigger for Touch/No Touch.

Logic:
- For + barrier:
  wait for N up ticks, then enter immediately on the first down tick.
- For - barrier:
  wait for N down ticks, then enter immediately on the first up tick.

This strategy is intentionally lightweight and deterministic so the engine
can fire an order on the exact tick where the reversal is detected.
"""

from __future__ import annotations

import time
from typing import Any, Dict, Iterable, Optional


class ManualTouchStrategy:
    def __init__(
        self,
        barrier_sign: str = "+",
        required_trend_ticks: int = 3,
        ignore_flat_ticks: bool = True,
        exact_streak_only: bool = False,
    ) -> None:
        raw_sign = str(barrier_sign or "+").strip().upper()
        if raw_sign.startswith("-") or raw_sign.startswith("DOWN") or raw_sign.startswith("NEG"):
            self.barrier_sign = "-"
        else:
            self.barrier_sign = "+"

        self.required_trend_ticks = max(1, int(required_trend_ticks))
        self.ignore_flat_ticks = bool(ignore_flat_ticks)
        self.exact_streak_only = bool(exact_streak_only)

        self.current_price = 0.0
        self.previous_price: Optional[float] = None
        self.up_streak = 0
        self.down_streak = 0
        self.armed = False
        self.triggered = False
        self.last_move = 0.0
        self.last_rejection = "Waiting for first tick."
        self.last_tick_epoch: Optional[float] = None
        self.last_signal_epoch: Optional[float] = None

    def reset(self) -> None:
        self.current_price = 0.0
        self.previous_price = None
        self.up_streak = 0
        self.down_streak = 0
        self.armed = False
        self.triggered = False
        self.last_move = 0.0
        self.last_rejection = "Waiting for first tick."
        self.last_tick_epoch = None
        self.last_signal_epoch = None

    def seed_ticks(self, prices: Iterable[Any], epochs: Optional[Iterable[Any]] = None) -> None:
        epoch_list = list(epochs or [])
        for index, price in enumerate(prices):
            try:
                epoch = float(epoch_list[index]) if index < len(epoch_list) else time.time()
                self.process_tick(price, epoch, allow_signal=False)
            except (TypeError, ValueError):
                continue

    def process_tick(
        self,
        price: float,
        epoch: Optional[float] = None,
        allow_signal: bool = True,
    ) -> Optional[str]:
        try:
            price = float(price)
        except (TypeError, ValueError):
            return None

        if price <= 0:
            return None

        tick_epoch = float(epoch if epoch is not None else time.time())
        self.current_price = price
        self.last_tick_epoch = tick_epoch

        if self.triggered:
            return None

        if self.previous_price is None:
            self.previous_price = price
            self.last_rejection = "Waiting for next tick to compare."
            return None

        delta = price - self.previous_price

        if delta == 0.0:
            self.last_move = 0.0
            if not self.ignore_flat_ticks:
                self.up_streak = 0
                self.down_streak = 0
                self.armed = False
                self.previous_price = price
                self.last_rejection = "Flat tick reset the streak."
            else:
                self.last_rejection = self.last_rejection or "Flat tick ignored."
            return None

        self.last_move = delta
        is_up = delta > 0.0
        signal = self._handle_move(is_up=is_up, epoch=tick_epoch, can_trigger=bool(allow_signal))

        self.previous_price = price

        if signal and allow_signal:
            return "ENTRY"

        return None

    def _handle_move(self, is_up: bool, epoch: float, can_trigger: bool) -> bool:
        if self.barrier_sign == "+":
            if is_up:
                self.down_streak = 0
                self.up_streak += 1

                if self.exact_streak_only and self.up_streak > self.required_trend_ticks:
                    self.armed = False
                    self.last_rejection = (
                        f"Exact mode: moved past {self.required_trend_ticks} up ticks "
                        f"to {self.up_streak}, so no entry is allowed."
                    )
                    return False

                if self.up_streak >= self.required_trend_ticks:
                    self.armed = True
                    self.last_rejection = (
                        f"{self.up_streak} up ticks complete — waiting for one down reversal tick."
                    )
                else:
                    self.last_rejection = (
                        f"Up tick {self.up_streak}/{self.required_trend_ticks} — still building trend."
                    )

                return False

            # Down tick in + mode
            if (
                self.armed
                and self.up_streak >= self.required_trend_ticks
                and (not self.exact_streak_only or self.up_streak == self.required_trend_ticks)
            ):
                if can_trigger:
                    self.triggered = True
                    self.last_signal_epoch = epoch
                    self.last_rejection = "Down reversal detected — entry triggered immediately."
                    return True

                self.up_streak = 0
                self.down_streak = 1
                self.armed = False
                self.last_rejection = "Historical down reversal ignored during seeding."
                return False

            self.up_streak = 0
            self.down_streak = 1
            self.armed = False
            self.last_rejection = "Down tick before a valid up trend was armed."
            return False

        # Barrier sign is "-"
        if not is_up:
            self.up_streak = 0
            self.down_streak += 1

            if self.exact_streak_only and self.down_streak > self.required_trend_ticks:
                self.armed = False
                self.last_rejection = (
                    f"Exact mode: moved past {self.required_trend_ticks} down ticks "
                    f"to {self.down_streak}, so no entry is allowed."
                )
                return False

            if self.down_streak >= self.required_trend_ticks:
                self.armed = True
                self.last_rejection = (
                    f"{self.down_streak} down ticks complete — waiting for one up reversal tick."
                )
            else:
                self.last_rejection = (
                    f"Down tick {self.down_streak}/{self.required_trend_ticks} — still building trend."
                )

            return False

        # Up tick in - mode
        if (
            self.armed
            and self.down_streak >= self.required_trend_ticks
            and (not self.exact_streak_only or self.down_streak == self.required_trend_ticks)
        ):
            if can_trigger:
                self.triggered = True
                self.last_signal_epoch = epoch
                self.last_rejection = "Up reversal detected — entry triggered immediately."
                return True

            self.down_streak = 0
            self.up_streak = 1
            self.armed = False
            self.last_rejection = "Historical up reversal ignored during seeding."
            return False

        self.down_streak = 0
        self.up_streak = 1
        self.armed = False
        self.last_rejection = "Up tick before a valid down trend was armed."
        return False

    def get_state(self) -> Dict[str, Any]:
        trend_ticks = self.up_streak if self.barrier_sign == "+" else self.down_streak
        return {
            "manual_mode": self.barrier_sign,
            "manual_required_ticks": self.required_trend_ticks,
            "manual_trend_ticks": int(trend_ticks),
            "manual_armed": bool(self.armed),
            "manual_triggered": bool(self.triggered),
            "manual_last_move": float(self.last_move),
            "manual_last_rejection": str(self.last_rejection),
        }
