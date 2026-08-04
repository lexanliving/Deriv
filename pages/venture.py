"""src/venture_engine.py — Council adapter with safe fallback and deliberation."""

from __future__ import annotations

import time

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


def _deliberate(result):
    if not result.get("approved"):
        return result

    wait = float(result.get("wait_seconds", 0.0) or 0.0)

    if wait <= 0:
        return result

    if _state is not None:
        try:
            _state.set_status(f"Council deliberating {wait:.1f}s before execution…")
        except Exception:
            pass

    deadline = time.time() + wait

    while time.time() < deadline:
        if _state is not None and getattr(_state, "stop_requested", False):
            result.update({
                "approved": False,
                "outcome": "REJECT",
                "reason": "stop requested during council deliberation",
                "reasoning": "stop requested during council deliberation",
                "wait_seconds": 0.0,
            })
            return result

        time.sleep(0.1)

    if _state is not None:
        try:
            _state.set_status(result.get("reasoning") or "Council decision complete.")
        except Exception:
            pass

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
            logger.exception("Council review failed; using safe fallback approval.")

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
        result = _deliberate(result)

        return result


_singleton = None


def get_venture_engine():
    global _singleton

    if _singleton is None:
        _singleton = VentureEngine()

    return _singleton


def review_entry(setup):
    return get_venture_engine().review(setup)
