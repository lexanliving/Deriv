"""Live execution engine for the manual ONE TOUCH one-shot reversal bot.

Final speed behavior:
- START sends the first proposal immediately.
- Proposal is kept ready while waiting.
- Strategy counts live ticks.
- If 3 trend ticks complete and the 4th tick is reversal, buy instantly.
- If the one-shot sequence breaks, engine invalidates and stops.
- After one trade, engine stops.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

import websockets
from websockets.exceptions import WebSocketException

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


class FastDerivAPIClient(DerivAPIClient):
    """Lower-latency Deriv client.

    - disables websocket compression
    - disables websocket-level ping overhead
    - uses compact JSON encoding
    """

    async def connect(self) -> bool:
        if self._ws is not None or self._listener_task is not None:
            await self.disconnect()

        self.last_error = ""

        try:
            await self.authorize()
            websocket_url = await self._websocket_url()

            try:
                self._ws = await websockets.connect(
                    websocket_url,
                    ping_interval=None,
                    ping_timeout=None,
                    open_timeout=10,
                    close_timeout=5,
                    max_size=2**20,
                    compression=None,
                )
            except TypeError:
                self._ws = await websockets.connect(
                    websocket_url,
                    ping_interval=None,
                    ping_timeout=None,
                    open_timeout=10,
                    close_timeout=5,
                    max_size=2**20,
                )

            self._connected = True
            self._listener_task = asyncio.create_task(self._message_listener(), name="deriv-message-listener")
            self._ping_task = asyncio.create_task(self._ping_loop(), name="deriv-api-ping")

            logger.info("Fast Deriv connection established for account %s.", self.account_id)
            return True

        except (DerivAPIError, WebSocketException, OSError, asyncio.TimeoutError) as exc:
            self.last_error = str(exc)
            logger.warning("Deriv connection failed: %s", exc)
            await self.disconnect()
            return False

    async def _send_request(self, payload: Dict[str, Any], timeout: Optional[float] = None) -> Dict[str, Any]:
        if not self._connected or self._ws is None:
            raise DerivAPIError("Not connected to Deriv.", "NOT_CONNECTED")

        if not isinstance(payload, dict) or not payload:
            raise DerivAPIError("Cannot send an empty Deriv request.", "INVALID_REQUEST")

        timeout = self.REQUEST_TIMEOUT if timeout is None else float(timeout)

        self._req_id += 1
        request_id = self._req_id
        request = dict(payload, req_id=request_id)

        future = asyncio.get_running_loop().create_future()
        self._pending_requests[request_id] = future

        try:
            await self._ws.send(json.dumps(request, separators=(",", ":")))
            response = await asyncio.wait_for(future, timeout=timeout)

            if not isinstance(response, dict):
                raise DerivAPIError("Deriv returned an invalid response envelope.", "INVALID_RESPONSE")

            return response

        except (ConnectionClosed, WebSocketException, OSError) as exc:
            self._pending_requests.pop(request_id, None)
            self._connected = False
            raise DerivAPIError("Deriv connection lost while sending the request.", "CONNECTION_LOST") from exc

        except asyncio.TimeoutError as exc:
            self._pending_requests.pop(request_id, None)
            raise DerivAPIError("Deriv did not answer in time.", "TIMEOUT") from exc

        except (TypeError, ValueError) as exc:
            self._pending_requests.pop(request_id, None)
            raise DerivAPIError("The Deriv request could not be encoded.", "INVALID_REQUEST") from exc


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
        contract_style: str = "ONETOUCH",
        required_trend_ticks: int = 3,
        flat_invalid: bool = True,
        exact_one_shot: bool = True,
        stop_after_trade: bool = True,
        max_wait_seconds: float = 15.0,
        proposal_refresh_seconds: float = 0.25,
        proposal_max_age_seconds: float = 1.00,
        proposal_timeout: float = 0.9,
        request_timeout: float = 2.0,
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

        # ONE TOUCH ONLY.
        self.contract_type = "ONETOUCH"

        self.required_trend_ticks = max(1, int(required_trend_ticks))
        self.flat_invalid = bool(flat_invalid)
        self.exact_one_shot = bool(exact_one_shot)
        self.stop_after_trade = bool(stop_after_trade)
        self.max_wait_seconds = max(0.0, float(max_wait_seconds))

        self.proposal_refresh_seconds = max(0.05, float(proposal_refresh_seconds))
        self.proposal_max_age_seconds = max(0.10, float(proposal_max_age_seconds))
        self.proposal_timeout = max(0.25, float(proposal_timeout))
        self.request_timeout = max(0.50, float(request_timeout))

        # Speed-critical Deriv client behavior.
        api_client_module.PROPOSAL_REQUEST_TIMEOUT = self.proposal_timeout
        DerivAPIClient.REQUEST_TIMEOUT = self.request_timeout

        self._strategy = ManualTouchStrategy(
            barrier_sign=self.barrier_sign,
            required_trend_ticks=self.required_trend_ticks,
            flat_invalid=self.flat_invalid,
            exact_one_shot=self.exact_one_shot,
        )

        try:
            self._journal = get_journal()
        except Exception:
            self._journal = None

        self._client: Optional[FastDerivAPIClient] = None
        self._reconnect_lock: Optional[asyncio.Lock] = None
        self._proposal_lock: Optional[asyncio.Lock] = None
        self._proposal_keeper_task: Optional[asyncio.Task] = None

        self._ready_proposal: Optional[Dict[str, Any]] = None
        self._proposal_time = 0.0
        self._proposal_stake = 0.0
        self._proposal_error = ""

        self._entry_in_progress = False
        self._active_contract_id: Optional[int] = None
        self._run_started_monotonic = 0.0

        self.state.update_strategy_state(
            strategy_mode="MANUAL_ONE_TOUCH",
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
                strategy_mode="MANUAL_ONE_TOUCH",
                manual_mode=self.barrier_sign,
                manual_barrier=self.barrier,
                manual_contract_type=self.contract_type,
                manual_duration_ticks=self.contract_duration,
                manual_required_ticks=self.required_trend_ticks,
                **self._strategy.get_state(),
            )
        except Exception:
            logger.exception("Could not push manual one-touch strategy state.")

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

    # ------------------------------------------------------------- proposal

    async def _refresh_proposal(self, force: bool = False) -> None:
        if self._proposal_lock is None:
            self._proposal_lock = asyncio.Lock()

        async with self._proposal_lock:
            if self._entry_in_progress or self.state.stop_requested or self._strategy.finished:
                return

            now = time.time()
            if (
                not force
                and self._ready_proposal is not None
                and now - self._proposal_time < self.proposal_refresh_seconds
            ):
                return

            if not self._client or not self._client.connected:
                return

            stake = float(self.state.get_martingale_state().get("stake", self.initial_stake))

            try:
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
                    raise DerivAPIError("Deriv returned an invalid proposal.", "INVALID_RESPONSE")

                self._ready_proposal = proposal
                self._proposal_time = time.time()
                self._proposal_stake = stake
                self._proposal_error = ""

            except Exception as exc:
                self._proposal_error = str(exc)
                logger.debug("Proposal refresh failed: %s", exc)

    def _get_ready_proposal(self) -> Optional[Dict[str, Any]]:
        if self._ready_proposal is None:
            return None

        age = time.time() - self._proposal_time
        if age < 0 or age > self.proposal_max_age_seconds:
            return None

        try:
            proposal_id = self._ready_proposal.get("id")
            ask_price = float(self._ready_proposal.get("ask_price"))
            payout = float(self._ready_proposal.get("payout"))
        except (TypeError, ValueError):
            return None

        if not proposal_id or ask_price <= 0 or payout <= 0:
            return None

        current_stake = float(self.state.get_martingale_state().get("stake", self.initial_stake))
        if abs(current_stake - float(self._proposal_stake)) > 0.001:
            return None

        return self._ready_proposal

    async def _proposal_keeper(self) -> None:
        while True:
            try:
                if self.state.stop_requested or self._entry_in_progress or self._strategy.finished:
                    break

                now = time.time()
                if self._ready_proposal is None or now - self._proposal_time >= self.proposal_refresh_seconds:
                    await self._refresh_proposal(force=True)

                await asyncio.sleep(0.02)

            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Proposal keeper failed.")
                await asyncio.sleep(0.10)

    # ---------------------------------------------------------------- lifecycle

    async def run(self) -> None:
        self._reconnect_lock = asyncio.Lock()
        self._proposal_lock = asyncio.Lock()
        self._run_started_monotonic = time.monotonic()

        logger.info(
            "Manual ONE TOUCH one-shot engine start | market=%s mode=%s contract=%s barrier=%s duration=%dt required_ticks=%d",
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
        self._client = FastDerivAPIClient(self.api_token, self.app_id, self.account_id)

        if not await self._client.connect():
            detail = self._client.last_error if self._client else ""
            message = f"Failed to connect to Deriv API. {detail or 'Check App ID, PAT scopes, and internet connection.'}"
            self.state.set_error(message)
            self.state.set_status("Connection failed.")
            self.state.set_running(False)
            return

        try:
            self.state.set_status("START pressed — sending ONE TOUCH proposal immediately…")

            # START behavior:
            # 1) send proposal immediately
            # 2) load one reference tick so the first live tick can count as tick 1
            # 3) subscribe to live ticks
            proposal_task = asyncio.create_task(self._refresh_proposal(force=True))

            try:
                history = await self._client.get_ticks_history(self.symbol, count=2)
                prices = history.get("prices", [])
                if prices:
                    self._strategy.initialize_reference(prices[-1])
            except Exception as exc:
                logger.warning("Could not load reference tick: %s", exc)

            await self._client.subscribe_ticks(self.symbol, self._on_tick)

            self._proposal_keeper_task = asyncio.create_task(
                self._proposal_keeper(),
                name="one-touch-start-proposal-keeper",
            )

            await asyncio.gather(proposal_task, return_exceptions=True)

            side = "UP" if self.barrier_sign == "+" else "DOWN"
            reversal = "DOWN" if self.barrier_sign == "+" else "UP"

            if self._get_ready_proposal() is not None:
                self.state.set_status(
                    f"Proposal ready. Waiting for {self.required_trend_ticks} {side} ticks, "
                    f"then a {reversal} tick to fire ONE TOUCH {self.barrier} instantly."
                )
            else:
                self.state.set_status(
                    f"Waiting for proposal while watching for {self.required_trend_ticks} {side} ticks."
                )

            while True:
                await asyncio.sleep(MAIN_LOOP_INTERVAL)

                if self.state.stop_requested and not self._entry_in_progress:
                    break

                if self._strategy.finished and not self._entry_in_progress:
                    break

                if (
                    self.max_wait_seconds > 0
                    and not self._strategy.finished
                    and not self._entry_in_progress
                    and time.monotonic() - self._run_started_monotonic > self.max_wait_seconds
                ):
                    self.state.set_status("One-shot window expired without entry. Stopping.")
                    self.state.request_stop()
                    break

                if self._client and not self._client.connected:
                    if not self._entry_in_progress:
                        self.state.set_status("Connection lost. Reconnecting…")
                    await self._reconnect(max_attempts=3)

                if not self._entry_in_progress:
                    self._push_state()

        except asyncio.CancelledError:
            raise
        except DerivAPIError as exc:
            logger.error("API error in manual one-touch engine: %s", exc)
            self.state.set_error(str(exc))
            self.state.set_status("Engine stopped due to API error.")
        except Exception as exc:
            logger.exception("Unexpected error in manual one-touch engine: %s", exc)
            self.state.set_error(f"Unexpected error: {exc}")
            self.state.set_status("Engine stopped due to an unexpected error.")
        finally:
            await self._shutdown()

    async def _shutdown(self) -> None:
        logger.info("Manual one-touch engine shutting down…")

        if self._proposal_keeper_task is not None:
            self._proposal_keeper_task.cancel()
            try:
                await self._proposal_keeper_task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.debug("Proposal keeper shutdown failed.", exc_info=True)

        if self._entry_in_progress:
            self.state.set_status("Stopping — letting the open contract finish…")
            deadline = time.monotonic() + max(45.0, self._contract_duration_seconds() + 30.0)
            while self._entry_in_progress and time.monotonic() < deadline:
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

    # ------------------------------------------------------------- reconnect

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
                        if not self._entry_in_progress:
                            await self._client.subscribe_ticks(self.symbol, self._on_tick)
                            await self._refresh_proposal(force=True)
                            self.state.set_status("Reconnected to Deriv.")
                        return True
                except Exception as exc:
                    logger.warning("Reconnect attempt %d failed: %s", attempt, exc)

                if attempt < max_attempts:
                    await asyncio.sleep(min(2 ** attempt, 5))

            self.state.set_error("Could not reconnect to Deriv after repeated attempts.")
            return False

    # ------------------------------------------------------------------ ticks

    async def _on_tick(self, tick_data: Dict[str, Any]) -> None:
        try:
            price = float(tick_data.get("quote", 0))
            epoch = float(tick_data.get("epoch", time.time()))
        except (TypeError, ValueError):
            return

        if price <= 0:
            return

        if self._entry_in_progress or self.state.stop_requested or not self.state.is_running:
            return

        signal = self._strategy.process_tick(price, epoch, allow_signal=True)

        if signal == "ENTRY":
            self._entry_in_progress = True
            self.state.set_status("Reversal detected — firing ONE TOUCH instantly…")
            await self._execute_trade()
            self.state.update_tick(price, epoch)
            self._push_state()
            return

        if signal == "INVALID":
            self.state.update_tick(price, epoch)
            self._push_state()
            self.state.set_status(f"One-shot invalidated: {self._strategy.invalid_reason}")
            self.state.request_stop()
            return

        self.state.update_tick(price, epoch)
        self._push_state()

    # -------------------------------------------------------------- execution

    async def _execute_trade(self) -> None:
        trade_id = str(uuid.uuid4())[:8]
        signal_id = str(uuid.uuid4().hex)[:10]

        stake = float(self.state.get_martingale_state().get("stake", self.initial_stake))
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
            martingale_step=0,
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
                "note": str(self._strategy.last_rejection or "Manual one-touch reversal trigger"),
                "score": "",
                "threshold": "",
                "regime": "MANUAL_ONE_TOUCH",
                "duration_min": round(self.contract_duration / 60.0, 4),
                "strategy_mode": "MANUAL_ONE_TOUCH",
                "barrier": self.barrier,
                "duration_unit": "t",
                "digit_precision": "",
                "last_digit": "",
            }
        )

        proposal_source = ""

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

            proposal = self._get_ready_proposal()
            if proposal is not None:
                proposal_source = "START_PROPOSAL"
            else:
                proposal_source = "EMERGENCY_PROPOSAL"
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
                raise DerivAPIError("Deriv returned an invalid one-touch proposal.", "INVALID_RESPONSE")

            if self.state.stop_requested:
                raise DerivAPIError("Stop requested before buy; no order was submitted.", "STOP_REQUESTED")

            self.state.set_status(f"Firing ONE TOUCH {self.barrier} using {proposal_source.lower()}…")

            try:
                buy = await self._client.buy_contract(proposal_id=proposal_id, price=ask_price)
            except DerivAPIError as buy_exc:
                code = str(buy_exc.code or "").upper()
                message = str(buy_exc.message or "").upper()
                recoverable = any(
                    word in code or word in message
                    for word in ("PROPOSAL", "PRICE", "EXPIRED", "INVALID")
                )

                if not recoverable or self.state.stop_requested:
                    raise

                proposal_source = f"{proposal_source}+RECOVERED"
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
                    raise DerivAPIError("Deriv returned an invalid recovered proposal.", "INVALID_RESPONSE")

                buy = await self._client.buy_contract(proposal_id=proposal_id, price=ask_price)

            contract_id = buy.get("contract_id")
            buy_price = float(buy.get("buy_price", ask_price))
            buy_payout = float(buy.get("payout", payout))

            if not isinstance(contract_id, int) or isinstance(contract_id, bool):
                raise DerivAPIError("No valid contract ID in one-touch buy response.", "NO_CONTRACT_ID")

            # Invalidate proposal cache after use.
            self._ready_proposal = None
            self._proposal_time = 0.0
            self._proposal_stake = 0.0

            self._active_contract_id = contract_id
            trade_record.contract_id = contract_id
            self.state.update_trade_pacing()
            self.state.set_status(
                f"Contract {contract_id} active · ONE TOUCH {self.barrier} · {proposal_source}"
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
                self.state.set_status(f"ONE TOUCH {self.barrier} WON · P&L {pnl:+.2f}")
            else:
                self.state.on_trade_loss(1.0, 0)
                self.state.set_status(f"ONE TOUCH {self.barrier} LOST · P&L {pnl:+.2f}")

            self._record_outcome(signal_id, outcome, pnl, stake, contract_id, note=proposal_source)

        except DerivAPIError as exc:
            reason = f"One-touch order failed: {exc.message} ({exc.code})."
            if self._active_contract_id is not None:
                self.state.update_trade_outcome(trade_id, "UNKNOWN", 0.0, reason)
                self._record_outcome(signal_id, "UNKNOWN", 0.0, stake, self._active_contract_id, note=reason)
            else:
                self.state.update_trade_outcome(trade_id, "CANCELLED", 0.0, reason)
                self._record_outcome(signal_id, "CANCELLED", 0.0, stake, None, note=reason)
            self.state.set_error(reason)
            self.state.set_status(reason)

        except Exception as exc:
            reason = f"Unexpected one-touch execution failure: {exc}"
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
            self._entry_in_progress = False
            if self.stop_after_trade:
                self.state.request_stop()

    # ------------------------------------------------------------- monitoring

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
