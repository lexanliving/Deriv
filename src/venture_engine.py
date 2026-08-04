"""src/venture_engine.py — thin adapter exposing the Council to the engine."""

from __future__ import annotations

from src.logger import get_logger

logger = get_logger("venture")

try:
    from src.council import council as _council
except Exception:
    _council = None

_enabled = True
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
                "reasons": ["council disabled"],
                "reasoning": "council disabled",
                "thinking_ms": 0.0,
            }

        if _council is None:
            return {
                "approved": True,
                "outcome": "APPROVE",
                "confidence": 100,
                "reasons": ["council unavailable"],
                "reasoning": "council unavailable",
                "thinking_ms": 0.0,
            }

        return _council.review(setup, _state)


_singleton = None


def get_venture_engine():
    global _singleton

    if _singleton is None:
        _singleton = VentureEngine()

    return _singleton


def review_entry(setup):
    return get_venture_engine().review(setup)
