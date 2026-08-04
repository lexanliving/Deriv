"""src/venture_engine.py — minimal safety guard only.

This is intentionally weak. It does not give AI control over trading.
It only blocks obviously invalid setups.
"""

from __future__ import annotations

from src.logger import get_logger

logger = get_logger("venture")

_enabled = False
_state = None


def set_venture_enabled(on: bool) -> None:
    global _enabled
    _enabled = bool(on)


def is_venture_enabled() -> bool:
    return _enabled


class VentureEngine:
    def attach(self, state):
        global _state
        _state = state

    def review(self, setup):
        if not _enabled:
            return {
                "approved": True,
                "outcome": "APPROVE",
                "confidence": 100,
                "reasons": ["guard disabled"],
                "reason": "guard disabled",
                "reasoning": "guard disabled",
                "thinking_ms": 0.0,
                "wait_seconds": 0.0,
            }

        direction = setup.get("direction")
        entry_price = float(setup.get("entry_price") or 0.0)
        duration = int(setup.get("duration") or 0)

        if direction not in ("BUY", "SELL"):
            return {
                "approved": False,
                "outcome": "REJECT",
                "confidence": 100,
                "reasons": ["invalid direction"],
                "reason": "invalid direction",
                "reasoning": "invalid direction",
                "thinking_ms": 0.0,
                "wait_seconds": 0.0,
            }

        if entry_price <= 0:
            return {
                "approved": False,
                "outcome": "REJECT",
                "confidence": 100,
                "reasons": ["invalid entry price"],
                "reason": "invalid entry price",
                "reasoning": "invalid entry price",
                "thinking_ms": 0.0,
                "wait_seconds": 0.0,
            }

        if duration <= 0:
            return {
                "approved": False,
                "outcome": "REJECT",
                "confidence": 100,
                "reasons": ["invalid duration"],
                "reason": "invalid duration",
                "reasoning": "invalid duration",
                "thinking_ms": 0.0,
                "wait_seconds": 0.0,
            }

        return {
            "approved": True,
            "outcome": "APPROVE",
            "confidence": 100,
            "reasons": ["basic guard passed"],
            "reason": "basic guard passed",
            "reasoning": "basic guard passed",
            "thinking_ms": 0.0,
            "wait_seconds": 0.0,
        }


_singleton = None


def get_venture_engine():
    global _singleton

    if _singleton is None:
        _singleton = VentureEngine()

    return _singleton


def review_entry(setup):
    return get_venture_engine().review(setup)
