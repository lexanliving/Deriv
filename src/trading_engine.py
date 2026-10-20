"""Live execution engine for the manual Touch / No Touch reversal bot.

This engine is built for speed:
- signal is evaluated directly inside the tick callback
- entry is fired immediately on the reversal tick
- one trade per start
- after settlement, the engine stops automatically

Default behavior:
- +0.08 bias -> wait for up ticks, then enter on down reversal
- -0.08 bias -> wait for down ticks, then enter on up reversal
"""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from config import CURRENCY, DEFAULT_INITIAL_STAKE

from src import api_client as api_client_module
from src.api_client import DerivAPIClient, DerivAPIError
from src.journal import get_journal
from src.logger import get_logger
from src.manual_touch_strategy import ManualTouchStrategy
from src.state_manager import StateManager, TradeRecord

logger = get_logger("trading_engine")

MAIN_LOOP_INTERVAL = 0.05

_DEMO_ACCOUNT_TYPES = {"DEMO", "VIRTUAL", "PRACTICE", "VIRTUAL_ACCOUNT", "DEMO_ACCOUNT"}
_REAL_ACCOUNT_TYPES = {"REAL", "LIVE", "REAL_MONEY", "REAL_ACCOUNT"}


def normalize_account_type(account_type: str) -> str:
    normalized = " ".join(str(account_type or "").strip().upper().replace("-", " ").split())
    if normalized in _DEMO_ACCOUNT_TYPES:
        return "DEMO"
    if normalized in _REAL_ACCOUNT_TYPES:
        return "REAL"
    return normalized or "UNKNOWN"


def resolve_execution_mode(account_type: str, real_execution_confirmed: bool) -> str:
    normalized_type = normalize_account_type(account_type)
    if normalized_type == "DEMO":
        return "DEMO"
    if normalized_type == "REAL" and real_execution_confirmed:
        return "REAL"
    return "BLOCKED"


class TradingEngine:
    def __init__(
        self,
        api_token: str,
        app_id: str,
        account_id: str,
        account_currency: str,
        state: StateManager,
        initial_stake: float = DEFAULT_INITIAL_STAKE,
        symbol: str = "1HZ10V",
        symbol_display: str = "Volatility 10 (1s)",
        contract_duration: int = 5,
        contract_duration_unit: str = "t",
        account_type: str = "UNKNOWN",
        real_execution_confirmed: bool = False,
        barrier_sign: str = "+",
        barrier_offset: float = 0.08,
        barrier_decimal_places: int = 2,
        contract_style: str = "NOTOUCH",
        required_trend_ticks: int = 3,
        ignore_flat_ticks: bool = True,
        exact_streak_only: bool = False,
        stop_after_trade: bool = True,
        proposal_timeout: float = 1.5,
        request_timeout: float = 3.0,
        **_: Any,
    ) -> None:
        self.api_token = str(api_token or "").strip()
        self.app_id = str(app_id or "").strip()
        self.account_id = str(account_id or "").strip()
        self.account_currency = str(account_currency or CURRENCY).upper()
        self.account_type = normalize_account_type(account_type)
        self.real_execution_confirmed = bool(real_execution_confirmed)
        self.execution_mode = resolve_execution_mode(self.account_type, self.real_execution_confirmed)

        self.state = state
        self.initial_stake = round(float(initial_stake), 2)
        self.state.set_initial_stake(self.initial_stake)

        self.symbol = str(symbol or "1HZ10V").strip()
        self.symbol_display = str(symbol_display or self.symbol)
        self.contract_duration = max(1, int(contract_duration))
        self.contract_duration_unit = str(contract_duration_unit or "t").lower()

        raw_sign = str(barrier_sign or "+").strip().upper()
        self.barrier_sign = "-" if raw_sign.startswith("-") or raw_sign.startswith("DOWN") else "+"
        self.barrier_offset = float(barrier_offset)
        self.barrier_decimal_places = max(0, int(barrier_decimal_places))
        self.barrier = self._build_barrier()

        raw_contract = str(contract_style or "NOTOUCH").strip().upper().replace(" ", "").replace("_", "")
        if raw_contract in {"TOUCH", "ONETOUCH", "ONETOUCH"}:
            self.contract_type = "ONETOUCH"
        else:
            self.contract_type = "NOTOUCH"

        self.required_trend_ticks = max(1, int(required_trend_ticks))
        self.ignore_flat_ticks = bool(ignore_flat_ticks)
        self.exact_streak_only = bool(exact_streak_only)
        self.stop_after_trade = bool(stop_after_trade)

        self.proposal_timeout = max(0.5, float(proposal_timeout))
        self.request_timeout = max(1.0, float(request_timeout))

        # Tighten request behavior for speed.
        api_client_module.PROPOSAL_REQUEST_TIMEOUT = self.proposal_timeout
        DerivAPIClient.REQUEST_TIMEOUT = self.request_timeout

        self._strategy = ManualTouchStrategy(
            barrier_sign=self.barrier_sign,
            required_trend_ticks=self.required_trend_ticks,
            ignore_flat_ticks=self.ignore_flat_ticks,
            exact_streak_only=self.exact_streak_only,
        )

        try:
            self._journal = get_journal()
        except Exception:
            self._journal = None

        self._client: Optional[DerivAPIClient] = None
        self._reconnect_lock: Optional[asyncio.Lock] = None
        self._trade_in_progress = False
        self._active_contract_id: Optional[int] = None

        self.state.update_strategy_state(
            strategy_mode="MANUAL_TOUCH",
            manual_mode=self.barrier_sign,
            manual_barrier=self.barrier,
            manual_contract_type=self.contract_type,
            manual_duration_ticks=self.contract_duration,
            manual_required_ticks=self.required_trend_ticks,
            manual_trend_ticks=0,
            manual_armed=False,
            manual_triggered=False,
            manual_last_move=0.0,
            manual_last_rejection="Engine initialized.",
        )

    # ------------------------------------------------------------------ helpers

    def _build_barrier(self) -> str:
        offset = abs(float(self.barrier_offset))
        decimals = max(0, int(self.barrier_decimal_places))
        if decimals == 0:
            text = str(int(round(offset)))
        else:
            text = f"{offset:.{decimals}f}"
        return f"{self.barrier_sign}{text}"

    def _push_state(self) -> None:
        try:
            self.state.update_strategy_state(
                strategy_mode="MANUAL_TOUCH",
                manual_mode=self.barrier_sign,
                manual_barrier=self.barrier,
                manual_contract_type=self.contract_type,
                manual_duration_ticks=self.contract_duration,
                manual_required_ticks=self.required_trend_ticks,
                **self._strategy.get_state(),
            )
        except Exception:
            logger.exception("Could not push manual strategy state.")

    def _record_evaluation(self, record: Optional[Dict[str, Any]]) -> None:
        if not self._journal or not record:
            return
        try:
            self._journal.record_evaluation(dict(record))
        except Exception:
            logger.exception("Journal evaluation write failed.")

    def _record_outcome(
        self,
        signal_id: Optional[str],
        outcome: str,
        pnl: float,
        stake: float,
        contract_id: Optional[int],
        note: str = "",
    ) -> None:
        if not self._journal or not signal_id:
            return
        try:
            self._journal.record_outcome(
                signal_id,
                outcome,
                pnl,
                stake,
                contract_id,
                self.execution_mode,
                0,
                note=note,
            )
        except Exception:
            logger.exception("Journal outcome write failed.")

    def _contract_duration_seconds(self) -> float:
        return max(5.0, float(self.contract_duration) + 5.0)

    # ------------------------------------------------------------------ lifecycle

    async def run(self) -> None:
        self._reconnect_lock = asyncio.Lock()

        logger.info(
            "Manual touch engine start | market=%s mode=%s contract=%s barrier=%s duration=%dt required_ticks=%d",
            self.symbol,
            self.execution_mode,
            self.contract_type,
            self.barrier,
            self.contract_duration,
            self.required_trend_ticks,
        )

        self.state.set_execution_context(
            account_id=self.account_id,
            account_type=self.account_type,
            currency=self.account_currency,
            execution_mode=self.execution_mode,
        )

        if self.execution_mode == "BLOCKED":
            self.state.set_error(
                "Trading is blocked. Use a demo account, or type LIVE exactly to enable a real account."
            )
            self.state.set_status("Blocked — no order will be sent.")
            self.state.set_running(False)
            return

        self.state.set_status(f"Connecting to Deriv ({self.execution_mode.lower()})…")
        self._client = DerivAPIClient(self.api_token, self.app_id, self.account_id)

        if not await self._client.connect():
            detail = self._client.last_error if self._client else ""
            message = f"Failed to connect to Deriv API. {detail or 'Check App ID, PAT scopes, and internet connection.'}"
            self.state.set_error(message)
            self.state.set_status("Connection failed.")
            self.state.set_running(False)
            return

        try:
            # Seed a little history so the bot can arm quickly if a trend is already running.
            try:
                history = await self._client.get_ticks_history(
                    self.symbol,
                    count=max(30, self.required_trend_ticks + 20),
                )
                self._strategy.seed_ticks(history.get("prices", []), history.get("times", []))
            except Exception as exc:
                logger.warning("Could not seed tick history: %s", exc)

            await self._client.subscribe_ticks(self.symbol, self._on_tick)
            self._push_state()

            side = "UP" if self.barrier_sign == "+" else "DOWN"
            reversal = "DOWN" if self.barrier_sign == "+" else "UP"
            self.state.set_status(
                f"Armed on {self.symbol_display}: waiting for {self.required_trend_ticks} {side} ticks, "
                f"then a {reversal} tick to buy {self.contract_type} {self.barrier} for {self.contract_duration} ticks."
            )

            while True:
                await asyncio.sleep(MAIN_LOOP_INTERVAL)

                if self.state.stop_requested and not self._trade_in_progress:
                    break

                if self._client and not self._client.connected:
                    if not self._trade_in_progress:
                        self.state.set_status("Connection lost. Reconnecting…")
                    await self._reconnect(max_attempts=3)

                if not self._trade_in_progress:
                    self._push_state()

        except asyncio.CancelledError:
            raise
        except DerivAPIError as exc:
            logger.error("API error in manual touch engine: %s", exc)
            self.state.set_error(str(exc))
            self.state.set_status("Engine stopped due to API error.")
        except Exception as exc:
            logger.exception("Unexpected error in manual touch engine: %s", exc)
            self.state.set_error(f"Unexpected error: {exc}")
            self.state.set_status("Engine stopped due to an unexpected error.")
        finally:
            await self._shutdown()

    async def _shutdown(self) -> None:
        logger.info("Manual touch engine shutting down…")

        if self._trade_in_progress:
            self.state.set_status("Stopping — letting the open contract finish…")
            deadline = time.monotonic() + max(45.0, self._contract_duration_seconds() + 30.0)
            while self._trade_in_progress and time.monotonic() < deadline:
                await asyncio.sleep(0.2)

        if self._client:
            try:
                await self._client.unsubscribe_ticks()
            except Exception:
                logger.debug("Tick unsubscribe failed during shutdown.", exc_info=True)

            try:
                await self._client.disconnect()
            except Exception:
                logger.debug("Deriv disconnect failed during shutdown.", exc_info=True)

        self.state.set_running(False)
        self.state.set_status("Stopped.")

    # ------------------------------------------------------------------ reconnect

    async def _reconnect(self, max_attempts: int = 3) -> bool:
        if self._reconnect_lock is None:
            self._reconnect_lock = asyncio.Lock()

        async with self._reconnect_lock:
            if self._client and self._client.connected:
                return True

            for attempt in range(1, max_attempts + 1):
                self.state.set_status(f"Connection lost. Reconnecting ({attempt}/{max_attempts})…")
                try:
                    if self._client:
                        await self._client.disconnect(cancel_callbacks=False)

                    if self._client and await self._client.connect():
                        if not self._trade_in_progress:
                            await self._client.subscribe_ticks(self.symbol, self._on_tick)
                            self.state.set_status("Reconnected to Deriv.")
                        return True
                except Exception as exc:
                    logger.warning("Reconnect attempt %d failed: %s", attempt, exc)

                if attempt < max_attempts:
                    await asyncio.sleep(min(2 ** attempt, 5))

            self.state.set_error("Could not reconnect to Deriv after repeated attempts.")
            return False

    # ------------------------------------------------------------------ tick logic

    async def _on_tick(self, tick_data: Dict[str, Any]) -> None:
        try:
            price = float(tick_data.get("quote", 0))
            epoch = float(tick_data.get("epoch", time.time()))
        except (TypeError, ValueError):
            return

        if price <= 0:
            return

        self.state.update_tick(price, epoch)

        if self._trade_in_progress or self.state.stop_requested or not self.state.is_running:
            return

        signal = self._strategy.process_tick(price, epoch, allow_signal=True)
        self._push_state()

        if signal == "ENTRY":
            self._trade_in_progress = True
            self.state.set_status("Reversal detected — executing immediately…")
            await self._execute_trade()

    # ------------------------------------------------------------------ execution

    async def _execute_trade(self) -> None:
        trade_id = str(uuid.uuid4())[:8]
        signal_id = str(uuid.uuid4().hex)[:10]

        stake = float(self.state.get_martingale_state().get("stake", self.initial_stake))
        martingale_step = int(self.state.get_martingale_state().get("step", 0))
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        direction = f"{self.contract_type} {self.barrier}"
        entry_price = float(self._strategy.current_price or 0.0)

        trade_record = TradeRecord(
            trade_id=trade_id,
            signal_id=signal_id,
            direction=direction,
            stake=stake,
            barrier=self.barrier,
            entry_price=entry_price,
            timestamp=timestamp,
            status="OPEN",
            martingale_step=martingale_step,
            execution_mode=self.execution_mode,
            account_type=self.account_type,
        )
        self.state.add_trade(trade_record)

        self._record_evaluation(
            {
                "signal_id": signal_id,
                "timestamp_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                "symbol": self.symbol,
                "direction": direction,
                "trend": "UP" if self.barrier_sign == "+" else "DOWN",
                "taken": "TRUE",
                "executed": "FALSE",
                "rejection_reason": "",
                "note": str(self._strategy.last_rejection or "Manual reversal trigger"),
                "score": "",
                "threshold": "",
                "regime": "MANUAL_TOUCH",
                "duration_min": round(self.contract_duration / 60.0, 4),
                "strategy_mode": "MANUAL_TOUCH",
                "barrier": self.barrier,
                "duration_unit": "t",
                "digit_precision": "",
                "last_digit": "",
            }
        )

        try:
            if self.execution_mode == "BLOCKED":
                reason = "Order blocked: use a demo account or type LIVE exactly for a real account."
                self.state.update_trade_outcome(trade_id, "CANCELLED", 0.0, reason)
                self._record_outcome(signal_id, "CANCELLED", 0.0, stake, None, note=reason)
                self.state.set_error(reason)
                self.state.set_status(reason)
                return

            if not self._client or (not self._client.connected and not await self._reconnect(max_attempts=2)):
                reason = "Order not sent: could not establish/restore Deriv connection."
                self.state.update_trade_outcome(trade_id, "CANCELLED", 0.0, reason)
                self._record_outcome(signal_id, "CANCELLED", 0.0, stake, None, note=reason)
                self.state.set_error(reason)
                self.state.set_status(reason)
                return

            if self.state.stop_requested:
                raise DerivAPIError("Stop requested before buy; no order was submitted.", "STOP_REQUESTED")

            self.state.set_status(f"Requesting {self.contract_type} {self.barrier} quote…")

            proposal = await self._client.get_proposal(
                symbol=self.symbol,
                contract_type=self.contract_type,
                stake=stake,
                duration=self.contract_duration,
                duration_unit="t",
                barrier=self.barrier,
                currency=self.account_currency,
            )

            proposal_id = proposal.get("id")
            ask_price = float(proposal.get("ask_price"))
            payout = float(proposal.get("payout"))

            if not proposal_id or ask_price <= 0 or payout <= 0:
                raise DerivAPIError("Deriv returned an invalid touch/no-touch proposal.", "INVALID_RESPONSE")

            if self.state.stop_requested:
                raise DerivAPIError("Stop requested before buy; no order was submitted.", "STOP_REQUESTED")

            self.state.set_status(f"Buying {self.contract_type} {self.barrier} at {ask_price:.2f}…")

            buy = await self._client.buy_contract(proposal_id=proposal_id, price=ask_price)
            contract_id = buy.get("contract_id")
            buy_price = float(buy.get("buy_price", ask_price))
            buy_payout = float(buy.get("payout", payout))

            if not isinstance(contract_id, int) or isinstance(contract_id, bool):
                raise DerivAPIError("No valid contract ID in touch/no-touch buy response.", "NO_CONTRACT_ID")

            self._active_contract_id = contract_id
            trade_record.contract_id = contract_id
            self.state.update_trade_pacing()
            self.state.set_status(
                f"Contract {contract_id} active · {self.contract_type} {self.barrier} · awaiting settlement"
            )

            outcome, pnl = await self._monitor_contract(contract_id, buy_price, buy_payout)

            if outcome == "UNKNOWN":
                reason = f"Contract {contract_id} settlement was not confirmed; check the Deriv statement."
                self.state.update_trade_outcome(trade_id, "UNKNOWN", 0.0, reason)
                self._record_outcome(signal_id, "UNKNOWN", 0.0, stake, contract_id, note=reason)
                self.state.set_error(reason)
                self.state.set_status(reason)
                return

            self.state.update_trade_outcome(trade_id, outcome, pnl)

            if outcome == "WON":
                self.state.on_trade_win()
                self.state.set_status(f"{self.contract_type} {self.barrier} WON · P&L {pnl:+.2f}")
            else:
                self.state.on_trade_loss(1.0, 0)
                self.state.set_status(f"{self.contract_type} {self.barrier} LOST · P&L {pnl:+.2f}")

            self._record_outcome(signal_id, outcome, pnl, stake, contract_id)

        except DerivAPIError as exc:
            reason = f"Manual touch order failed: {exc.message} ({exc.code})."
            if self._active_contract_id is not None:
                self.state.update_trade_outcome(trade_id, "UNKNOWN", 0.0, reason)
                self._record_outcome(signal_id, "UNKNOWN", 0.0, stake, self._active_contract_id, note=reason)
            else:
                self.state.update_trade_outcome(trade_id, "CANCELLED", 0.0, reason)
                self._record_outcome(signal_id, "CANCELLED", 0.0, stake, None, note=reason)
            self.state.set_error(reason)
            self.state.set_status(reason)

        except Exception as exc:
            reason = f"Unexpected manual touch execution failure: {exc}"
            if self._active_contract_id is not None:
                self.state.update_trade_outcome(trade_id, "UNKNOWN", 0.0, reason)
                self._record_outcome(signal_id, "UNKNOWN", 0.0, stake, self._active_contract_id, note=reason)
            else:
                self.state.update_trade_outcome(trade_id, "CANCELLED", 0.0, reason)
                self._record_outcome(signal_id, "CANCELLED", 0.0, stake, None, note=reason)
            self.state.set_error(reason)
            self.state.set_status(reason)

        finally:
            self._active_contract_id = None
            self._trade_in_progress = False
            if self.stop_after_trade:
                self.state.request_stop()

    # ------------------------------------------------------------------ monitoring

    async def _monitor_contract(self, contract_id: int, buy_price: float, payout: float) -> Tuple[str, float]:
        expected = self._contract_duration_seconds()
        deadline = time.time() + max(25.0, expected + 25.0)
        fast_until = time.time() + max(3.0, expected + 3.0)

        while time.time() < deadline:
            if not self._client or not self._client.connected:
                await self._reconnect(max_attempts=2)
                await asyncio.sleep(0.2)
                continue

            try:
                status = await self._client.get_open_contract_status(contract_id)

                name = str(status.get("status", "")).lower()
                expired = bool(status.get("is_expired", 0))
                sold = bool(status.get("is_sold", 0))

                if expired or sold or name in {"won", "lost", "sold"}:
                    raw_profit = status.get("profit")
                    try:
                        if raw_profit is None:
                            sell_price = float(status.get("sell_price", 0) or 0)
                            profit = sell_price - buy_price
                        else:
                            profit = float(raw_profit)
                    except (TypeError, ValueError):
                        profit = 0.0

                    if name == "won" or profit > 0:
                        return "WON", round(profit, 2)

                    if name == "lost" or profit < 0:
                        return "LOST", round(profit, 2)

                    if expired or sold:
                        return ("WON" if profit >= 0 else "LOST"), round(profit, 2)

                await asyncio.sleep(0.1 if time.time() < fast_until else 0.25)

            except (DerivAPIError, TypeError, ValueError) as exc:
                logger.warning("Contract poll failed for %s: %s", contract_id, exc)
                await asyncio.sleep(0.4)

        return "UNKNOWN", 0.0
