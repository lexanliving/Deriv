"""src/venture_engine.py — Council adapter with safe reason normalisation."""

from __future__ import annotations

from src.logger import get_logger

try:
    from src.council import council as _council
except Exception:
    _council = None

logger = get_logger("venture")

_enabled = True
_state = None


def set_venture_enabled(on: bool) -> None:
    global _enabled
    _enabled = bool(on)


def is_venture_enabled() -> bool:
    return _enabled


def _with_reason(result):
    reason = result.get("reason") or result.get("reasoning") or ""

    if not reason:
        reason = "; ".join(str(x) for x in result.get("reasons", []) if x)

    result["reason"] = reason or "no reason returned"
    return result


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
                "reason": "council disabled",
                "reasoning": "council disabled",
                "thinking_ms": 0.0,
                "wait_seconds": 0.0,
            }

        if _council is None:
            return {
                "approved": True,
                "outcome": "APPROVE",
                "confidence": 100,
                "reasons": ["council unavailable"],
                "reason": "council unavailable",
                "reasoning": "council unavailable",
                "thinking_ms": 0.0,
                "wait_seconds": 0.0,
            }

        try:
            result = _council.review(setup, _state)
        except Exception as exc:
            logger.exception("Council failed; using fallback approval.")

            return {
                "approved": True,
                "outcome": "APPROVE",
                "confidence": 0,
                "reasons": [f"council error fallback: {exc}"],
                "reason": f"council error fallback: {exc}",
                "reasoning": f"council error fallback: {exc}",
                "thinking_ms": 0.0,
                "wait_seconds": 0.0,
            }

        result = _with_reason(result)
        return result


_singleton = None


def get_venture_engine():
    global _singleton

    if _singleton is None:
        _singleton = VentureEngine()

    return _singleton


def review_entry(setup):
    return get_venture_engine().review(setup)
