"""src/manual_touch_strategy.py — one-shot manual reversal trigger for ONE TOUCH.

This is built as a one-shot sequence:

For + barrier:
    wait for exactly N up ticks,
    then the next tick must be down,
    then entry fires immediately.

For - barrier:
    wait for exactly N down ticks,
    then the next tick must be up,
    then entry fires immediately.

If the sequence breaks, the strategy returns INVALID and the engine stops.
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional


class ManualTouchStrategy:
    def __init__(
        self,
        barrier_sign: str = "+",
        required_trend_ticks: int = 3,
        flat_invalid: bool = True,
        exact_one_shot: bool = True,
    ) -> None:
        raw_sign = str(barrier_sign or "+").strip().upper()
        if raw_sign.startswith("-") or raw_sign.startswith("DOWN") or raw_sign.startswith("NEG"):
            self.barrier_sign = "-"
        else:
            self.barrier_sign = "+"

        self.required_trend_ticks = max(1, int(required_trend_ticks))
        self.flat_invalid = bool(flat_invalid)
        self.exact_one_shot = bool(exact_one_shot)

        self.current_price = 0.0
        self.previous_price: Optional[float] = None
        self.trend_ticks = 0
        self.armed = False
        self.triggered = False
        self.invalid = False
        self.finished = False
        self.last_move = 0.0
        self.last_rejection = "Waiting for reference tick."
        self.invalid_reason = ""

    # ------------------------------------------------------------- utilities

    def reset(self) -> None:
        self.current_price = 0.0
        self.previous_price = None
        self.trend_ticks = 0
        self.armed = False
        self.triggered = False
        self.invalid = False
        self.finished = False
        self.last_move = 0.0
        self.last_rejection = "Waiting for reference tick."
        self.invalid_reason = ""

    def initialize_reference(self, price: Any) -> None:
        """Load the last known price before live tick counting starts."""
        if self.finished:
            return

        try:
            price = float(price)
        except (TypeError, ValueError):
            return

        if price <= 0:
            return

        self.current_price = price
        if self.previous_price is None:
            self.previous_price = price
            self.last_rejection = "Reference tick loaded. Waiting for tick 1."

    def _invalidate(self, reason: str) -> None:
        self.invalid = True
        self.finished = True
        self.armed = False
        self.invalid_reason = reason
        self.last_rejection = reason

    def _enter(self, reason: str) -> None:
        self.triggered = True
        self.finished = True
        self.armed = False
        self.invalid_reason = ""
        self.last_rejection = reason

    # ------------------------------------------------------------- core tick

    def process_tick(
        self,
        price: float,
        epoch: Optional[float] = None,
        allow_signal: bool = True,
    ) -> Optional[str]:
        """Returns:
        - "ENTRY"
        - "INVALID"
        - None
        """

        if self.finished:
            return None

        if not allow_signal:
            # One-shot mode intentionally ignores seeding.
            return None

        try:
            price = float(price)
        except (TypeError, ValueError):
            return None

        if price <= 0:
            return None

        tick_epoch = float(epoch if epoch is not None else time.time())
        self.current_price = price

        if self.previous_price is None:
            self.previous_price = price
            self.last_rejection = "Waiting for tick 1."
            return None

        delta = price - self.previous_price

        if delta == 0.0:
            self.last_move = 0.0
            if self.flat_invalid:
                self._invalidate("Flat tick invalidated the one-shot sequence.")
                return "INVALID"

            self.last_rejection = "Flat tick ignored."
            return None

        self.last_move = delta
        is_up = delta > 0.0
        self.previous_price = price

        # --------------------------------------------------------- plus mode

        if self.barrier_sign == "+":
            if is_up:
                if self.trend_ticks >= self.required_trend_ticks:
                    if self.exact_one_shot:
                        self._invalidate(
                            "The 4th tick was not a reversal. One-shot setup invalidated."
                        )
                        return "INVALID"

                    # Minimum-mode behavior, not the default.
                    self.armed = True
                    self.last_rejection = "Trend continuing. Waiting for reversal."
                    return None

                self.trend_ticks += 1

                if self.trend_ticks >= self.required_trend_ticks:
                    self.armed = True
                    self.last_rejection = (
                        f"{self.trend_ticks} up ticks complete — waiting for one down reversal tick."
                    )
                else:
                    self.last_rejection = (
                        f"Up tick {self.trend_ticks}/{self.required_trend_ticks}."
                    )

                return None

            # Down tick in + mode
            if (
                self.armed
                and self.trend_ticks >= self.required_trend_ticks
                and (not self.exact_one_shot or self.trend_ticks == self.required_trend_ticks)
            ):
                self._enter("Down reversal detected — ONE TOUCH entry triggered.")
                return "ENTRY"

            self._invalidate("Down tick occurred before the required up-trend sequence completed.")
            return "INVALID"

        # -------------------------------------------------------- minus mode

        if not is_up:
            if self.trend_ticks >= self.required_trend_ticks:
                if self.exact_one_shot:
                    self._invalidate(
                        "The 4th tick was not a reversal. One-shot setup invalidated."
                    )
                    return "INVALID"

                # Minimum-mode behavior, not the default.
                self.armed = True
                self.last_rejection = "Trend continuing. Waiting for reversal."
                return None

            self.trend_ticks += 1

            if self.trend_ticks >= self.required_trend_ticks:
                self.armed = True
                self.last_rejection = (
                    f"{self.trend_ticks} down ticks complete — waiting for one up reversal tick."
                )
            else:
                self.last_rejection = (
                    f"Down tick {self.trend_ticks}/{self.required_trend_ticks}."
                )

            return None

        # Up tick in - mode
        if (
            self.armed
            and self.trend_ticks >= self.required_trend_ticks
            and (not self.exact_one_shot or self.trend_ticks == self.required_trend_ticks)
        ):
            self._enter("Up reversal detected — ONE TOUCH entry triggered.")
            return "ENTRY"

        self._invalidate("Up tick occurred before the required down-trend sequence completed.")
        return "INVALID"

    # ----------------------------------------------------------------- state

    def get_state(self) -> Dict[str, Any]:
        return {
            "manual_mode": self.barrier_sign,
            "manual_required_ticks": self.required_trend_ticks,
            "manual_trend_ticks": int(self.trend_ticks),
            "manual_armed": bool(self.armed),
            "manual_triggered": bool(self.triggered),
            "manual_invalid": bool(self.invalid),
            "manual_last_move": float(self.last_move),
            "manual_last_rejection": str(self.last_rejection),
        }
