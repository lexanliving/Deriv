"""Live execution engine for the manual ONE TOUCH one-shot reversal bot.

Warm-session architecture:
- The Deriv connection (ticks + orders) is established once in the background.
- A hot proposal is kept ready continuously.
- START only arms the one-shot sequence (no connect, no history, no subscribe).
- The reversal tick fires the buy immediately on a dedicated order socket.
- Settlement runs in its own task so the tick loop never blocks.
"""

from __future__ import annotations

import asyncio
import json
import threading
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
    """Lower-latency Deriv client: no compression, no ws ping, compact JSON."""

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
        account_type: str = "UNKNOWN",
        real_execution_confirmed: bool = False,
        barrier_sign: str = "+",
        barrier_offset: float = 0.08,
        barrier_decimal_places: int = 2,
        required_trend_ticks: int = 3,
        flat_invalid: bool = True,
        exact_one_shot: bool = True,
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
        self.state.set_initial_stake(round(float(initial_stake), 2))

        self.symbol = str(symbol or "1HZ10V").strip()
        self.symbol_display = str(symbol_display or self.symbol)
        self.contract_duration = max(1, int(contract_duration))
        self.contract_type = "ONETOUCH"

        raw_sign = str(barrier_sign or "+").strip().upper()
        self.barrier_sign = "-" if raw_sign.startswith("-") or raw_sign.startswith("DOWN") else "+"
        self.barrier_offset = float(barrier_offset)
        self.barrier_decimal_places = max(0, int(barrier_decimal_places))
        self.barrier = self._build_barrier()

        self.required_trend_ticks = max(1, int(required_trend_ticks))
        self.flat_invalid = bool(flat_invalid)
        self.exact_one_shot = bool(exact_one_shot)
        self.max_wait_seconds = max(0.0, float(max_wait_seconds))

        self.proposal_refresh_seconds = max(0.05, float(proposal_refresh_seconds))
        self.proposal_max_age_seconds = max(0.10, float(proposal_max_age_seconds))
        self.proposal_timeout = max(0.25, float(proposal_timeout))
        self.request_timeout = max(0.50, float(request_timeout))

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

        # Cross-thread guards
        self._lock = threading.Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._halt = False

        # Warm session state
        self._warm_status = "OFF"
        self._tick_client: Optional[FastDerivAPIClient] = None
        self._order_client: Optional[FastDerivAPIClient] = None
        self._keeper_task: Optional[asyncio.Task] = None
        self._settle_task: Optional[asyncio.Task] = None
        self._proposal_lock: Optional[asyncio.Lock] = None

        # Live buffer (instant reference for arming)
        self._buffer_price = 0.0
        self._buffer_epoch = 0.0

        # One-shot state
        self._armed = False
        self._settle_active = False
        self._arm_monotonic = 0.0
        self._active_contract_id: Optional[int] = None

        # Hot proposal
        self._ready_proposal: Optional[Dict[str, Any]] = None
        self._proposal_time = 0.0
        self._proposal_key: Tuple[Any, ...] = ()
        self._proposal_error = ""

        self.session_key = (self.api_token, self.app_id, self.account_id)

    # ------------------------------------------------------------------ helpers

    def _build_barrier(self) -> str:
        offset = abs(float(self.barrier_offset))
        decimals = max(0, int(self.barrier_decimal_places))
        text = str(int(round(offset))) if decimals == 0 else f"{offset:.{decimals}f}"
        return f"{self.barrier_sign}{text}"

    def _proposal_identity(self) -> Tuple[Any, ...]:
        stake = float(self.state.get_martingale_state().get("stake", 0.0))
        return (round(stake, 2), self.barrier, self.contract_duration, self.contract_type, self.symbol)

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            strategy_state = self._strategy.get_state()
        age = (time.time() - self._proposal_time) if self._ready_proposal else None
        return {
            "warm": self._warm_status,
            "armed": self._armed,
            "settle": self._settle_active,
            "price": self._buffer_price,
            "barrier": self.barrier,
            "contract": self.contract_type,
            "duration_ticks": self.contract_duration,
            "required_ticks": self.required_trend_ticks,
            "proposal_hot": self._ready_proposal is not None and (age or 99) <= self.proposal_max_age_seconds,
            "proposal_age": round(age, 3) if age is not None else None,
            "proposal_error": self._proposal_error,
            **strategy_state,
        }

    # ------------------------------------------------------------------ settings

    def apply_settings(
        self,
        initial_stake: Optional[float] = None,
        barrier_sign: Optional[str] = None,
        barrier_offset: Optional[float] = None,
        barrier_decimal_places: Optional[int] = None,
        contract_duration: Optional[int] = None,
        required_trend_ticks: Optional[int] = None,
        flat_invalid: Optional[bool] = None,
        exact_one_shot: Optional[bool] = None,
        max_wait_seconds: Optional[float] = None,
        proposal_refresh_seconds: Optional[float] = None,
        proposal_max_age_seconds: Optional[float] = None,
        proposal_timeout: Optional[float] = None,
        request_timeout: Optional[float] = None,
        real_execution_confirmed: Optional[bool] = None,
    ) -> None:
        old_identity = self._proposal_identity()
        old_logic = (
            self.barrier_sign,
            self.required_trend_ticks,
            self.flat_invalid,
            self.exact_one_shot,
            self.contract_duration,
        )

        if initial_stake is not None:
            self.state.set_initial_stake(round(float(initial_stake), 2))
        if barrier_sign is not None:
            raw = str(barrier_sign).strip().upper()
            self.barrier_sign = "-" if raw.startswith("-") or raw.startswith("DOWN") else "+"
        if barrier_offset is not None:
            self.barrier_offset = float(barrier_offset)
        if barrier_decimal_places is not None:
            self.barrier_decimal_places = max(0, int(barrier_decimal_places))
        if contract_duration is not None:
            self.contract_duration = max(1, int(contract_duration))
        if required_trend_ticks is not None:
            self.required_trend_ticks = max(1, int(required_trend_ticks))
        if flat_invalid is not None:
            self.flat_invalid = bool(flat_invalid)
        if exact_one_shot is not None:
            self.exact_one_shot = bool(exact_one_shot)
        if max_wait_seconds is not None:
            self.max_wait_seconds = max(0.0, float(max_wait_seconds))
        if proposal_refresh_seconds is not None:
            self.proposal_refresh_seconds = max(0.05, float(proposal_refresh_seconds))
        if proposal_max_age_seconds is not None:
            self.proposal_max_age_seconds = max(0.10, float(proposal_max_age_seconds))
        if proposal_timeout is not None:
            self.proposal_timeout = max(0.25, float(proposal_timeout))
            api_client_module.PROPOSAL_REQUEST_TIMEOUT = self.proposal_timeout
        if request_timeout is not None:
            self.request_timeout = max(0.50, float(request_timeout))
            DerivAPIClient.REQUEST_TIMEOUT = self.request_timeout
        if real_execution_confirmed is not None:
            self.real_execution_confirmed = bool(real_execution_confirmed)
            self.execution_mode = resolve_execution_mode(self.account_type, self.real_execution_confirmed)

        self.barrier = self._build_barrier()

        self._strategy.barrier_sign = self.barrier_sign
        self._strategy.required_trend_ticks = self.required_trend_ticks
        self._strategy.flat_invalid = self.flat_invalid
        self._strategy.exact_one_shot = self.exact_one_shot

        if self._proposal_identity() != old_identity:
            self._ready_proposal = None

        if self._armed and (
            self.barrier_sign,
            self.required_trend_ticks,
            self.flat_invalid,
            self.exact_one_shot,
            self.contract_duration,
        ) != old_logic:
            self.disarm("Settings changed while armed — one-shot invalidated.")

    # ------------------------------------------------------------------ lifecycle

    def start_background(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._halt = False
        self._thread = threading.Thread(target=self._thread_main, daemon=True, name="one-touch-warm-session")
        self._thread.start()

    def halt(self) -> None:
        self._halt = True
        self.disarm("Session halted.")

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._session_loop())
        except Exception as exc:
            logger.exception("Warm session crashed: %s", exc)
            self.state.set_error(str(exc))
            self._warm_status = "DOWN"

    async def _session_loop(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._proposal_lock = asyncio.Lock()

        self._warm_status = "CONNECTING"
        self.state.set_status("Warming connection to Deriv…")

        self._tick_client = FastDerivAPIClient(self.api_token, self.app_id, self.account_id)
        self._order_client = FastDerivAPIClient(self.api_token, self.app_id, self.account_id)

        while not self._halt:
            results = await asyncio.gather(
                self._tick_client.connect(),
                self._order_client.connect(),
                return_exceptions=True,
            )
            tick_ok = results[0] is True
            order_ok = results[1] is True

            if tick_ok and order_ok:
                break

            self._warm_status = "DOWN"
            self.state.set_status("Deriv connection failed — retrying…")
            await asyncio.sleep(2.0)

        if self._halt:
            self._warm_status = "OFF"
            return

        try:
            await self._tick_client.subscribe_ticks(self.symbol, self._on_tick)
        except Exception as exc:
            self._warm_status = "DOWN"
            self.state.set_error(f"Tick subscription failed: {exc}")
            self.state.set_status("Tick subscription failed — retrying…")
            await self._shutdown_clients()
            return

        self._warm_status = "READY"
        self.state.set_status("Connection WARM — proposal keeper running. Press START when ready.")
        self._keeper_task = asyncio.create_task(self._proposal_keeper(), name="hot-proposal-keeper")

        while not self._halt:
            await asyncio.sleep(0.05)

            if self._tick_client and not self._tick_client.connected:
                await self._reconnect_client(self._tick_client, subscribe=True)

            if self._order_client and not self._order_client.connected:
                await self._reconnect_client(self._order_client, subscribe=False)

            if (
                self._armed
                and not self._settle_active
                and self.max_wait_seconds > 0
                and time.monotonic() - self._arm_monotonic > self.max_wait_seconds
            ):
                self.disarm("One-shot window expired without entry.")

        if self._keeper_task is not None:
            self._keeper_task.cancel()

        if self._settle_task is not None and not self._settle_task.done():
            try:
                await asyncio.wait_for(asyncio.shield(self._settle_task), timeout=60.0)
            except Exception:
                pass

        await self._shutdown_clients()
        self._warm_status = "OFF"

    async def _shutdown_clients(self) -> None:
        for client in (self._tick_client, self._order_client):
            if client is None:
                continue
            try:
                await client.unsubscribe_ticks()
            except Exception:
                pass
            try:
                await client.disconnect()
            except Exception:
                pass

    async def _reconnect_client(self, client: FastDerivAPIClient, subscribe: bool) -> None:
        for attempt in range(1, 4):
            try:
                await client.disconnect(cancel_callbacks=False)
                if await client.connect():
                    if subscribe:
                        await client.subscribe_ticks(self.symbol, self._on_tick)
                    self.state.set_status("Reconnected to Deriv.")
                    return
            except Exception as exc:
                logger.warning("Reconnect attempt %d failed: %s", attempt, exc)
            await asyncio.sleep(min(2 ** attempt, 5))
        self.state.set_error("Could not reconnect to Deriv.")

    # ------------------------------------------------------------------ controls

    def arm(self) -> bool:
        with self._lock:
            if self._armed or self._settle_active or self._warm_status != "READY" or self._halt:
                return False
            self._armed = True

        self.state.set_running(True)
        self.state.clear_error()
        self.state.set_status(
            f"ARMED — waiting for {self.required_trend_ticks} "
            f"{'UP' if self.barrier_sign == '+' else 'DOWN'} ticks then reversal to fire "
            f"{self.contract_type} {self.barrier}."
        )

        if self._loop is not None:
            try:
                self._loop.call_soon_threadsafe(self._finalize_arm)
            except RuntimeError:
                pass
        return True

    def _finalize_arm(self) -> None:
        self._strategy.reset()
        if self._buffer_price > 0:
            self._strategy.initialize_reference(self._buffer_price)
        self._arm_monotonic = time.monotonic()
        try:
            asyncio.ensure_future(self._refresh_proposal(force=True))
        except Exception:
            pass

    def disarm(self, reason: str = "") -> None:
        with self._lock:
            was_armed = self._armed
            self._armed = False
            settle = self._settle_active

        if settle:
            self.state.set_status("Stop requested — finishing the open contract…")
            return

        self.state.set_running(False)
        self.state.set_status(reason or ("Stopped." if was_armed else "Stopped."))

    # ------------------------------------------------------------------ proposal

    async def _proposal_keeper(self) -> None:
        while not self._halt:
            try:
                if self._order_client and self._order_client.connected and not self._settle_active:
                    now = time.time()
                    if self._ready_proposal is None or now - self._proposal_time >= self.proposal_refresh_seconds:
                        await self._refresh_proposal(force=True)
                await asyncio.sleep(0.05)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Proposal keeper failed.")
                await asyncio.sleep(0.10)

    async def _refresh_proposal(self, force: bool = False) -> None:
        if self._proposal_lock is None:
            self._proposal_lock = asyncio.Lock()

        async with self._proposal_lock:
            if self._halt or self._settle_active:
                return

            if not self._order_client or not self._order_client.connected:
                return

            identity = self._proposal_identity()
            stake = float(identity[0])

            try:
                proposal = await self._order_client.get_proposal(
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
                self._proposal_key = identity
                self._proposal_error = ""

            except Exception as exc:
                self._proposal_error = str(exc)
                logger.debug("Hot proposal refresh failed: %s", exc)

    def _get_ready_proposal(self) -> Optional[Dict[str, Any]]:
        if self._ready_proposal is None:
            return None

        age = time.time() - self._proposal_time
        if age < 0 or age > self.proposal_max_age_seconds:
            return None

        if self._proposal_key != self._proposal_identity():
            return None

        return self._ready_proposal

    # ------------------------------------------------------------------ ticks

    async def _on_tick(self, tick_data: Dict[str, Any]) -> None:
        try:
            price = float(tick_data.get("quote", 0))
            epoch = float(tick_data.get("epoch", time.time()))
        except (TypeError, ValueError):
            return

        if price <= 0:
            return

        self._buffer_price = price
        self._buffer_epoch = epoch
        self.state.update_tick(price, epoch)

        if not self._armed or self._settle_active or self._halt:
            return

        signal = self._strategy.process_tick(price, epoch, allow_signal=True)

        if signal == "ENTRY":
            self.state.set_status("Reversal detected — firing ONE TOUCH now…")
            info = await self._fire_buy(price)
            if info is not None:
                self._settle_task = asyncio.create_task(self._settle(info), name="one-touch-settle")
            self._push_state()
            return

        if signal == "INVALID":
            self._push_state()
            self.disarm(f"One-shot invalidated: {self._strategy.invalid_reason}")
            return

        self._push_state()

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
            pass

    # ------------------------------------------------------------------ execution

    async def _fire_buy(self, entry_price: float) -> Optional[Dict[str, Any]]:
        trade_id = str(uuid.uuid4())[:8]
        signal_id = str(uuid.uuid4().hex)[:10]

        stake = float(self.state.get_martingale_state().get("stake", self.state.get_martingale_state().get("initial_stake", 0.0)))
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        direction = f"{self.contract_type} {self.barrier}"

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

        try:
            if self.execution_mode == "BLOCKED":
                reason = "Order blocked: use a demo account or type LIVE exactly for a real account."
                self._cancel(trade_id, signal_id, stake, reason)
                self.disarm(reason)
                return None

            if not self._order_client or not self._order_client.connected:
                reason = "Order socket not connected."
                self._cancel(trade_id, signal_id, stake, reason)
                self.disarm(reason)
                return None

            if self.state.stop_requested:
                reason = "Stop requested before buy."
                self._cancel(trade_id, signal_id, stake, reason)
                self.disarm(reason)
                return None

            proposal = self._get_ready_proposal()
            source = "HOT_PROPOSAL"

            if proposal is None:
                source = "EMERGENCY_PROPOSAL"
                proposal = await self._order_client.get_proposal(
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
                raise DerivAPIError("Invalid one-touch proposal.", "INVALID_RESPONSE")

            try:
                buy = await self._order_client.buy_contract(proposal_id=proposal_id, price=ask_price)
            except DerivAPIError as buy_exc:
                code = str(buy_exc.code or "").upper()
                message = str(buy_exc.message or "").upper()
                recoverable = any(w in code or w in message for w in ("PROPOSAL", "PRICE", "EXPIRED", "INVALID"))
                if not recoverable:
                    raise
                source = f"{source}+RECOVERED"
                proposal = await self._order_client.get_proposal(
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
                buy = await self._order_client.buy_contract(proposal_id=proposal_id, price=ask_price)

            contract_id = buy.get("contract_id")
            if isinstance(contract_id, bool) or not isinstance(contract_id, int):
                raise DerivAPIError("No valid contract ID in buy response.", "NO_CONTRACT_ID")

            self._ready_proposal = None
            self._active_contract_id = contract_id
            trade_record.contract_id = contract_id
            self.state.update_trade_pacing()

            with self._lock:
                self._armed = False
                self._settle_active = True

            self.state.set_status(f"Contract {contract_id} active · ONE TOUCH {self.barrier} · {source}")

            return {
                "trade_id": trade_id,
                "signal_id": signal_id,
                "stake": stake,
                "contract_id": contract_id,
                "buy_price": float(buy.get("buy_price", ask_price)),
                "payout": float(buy.get("payout", payout)),
                "source": source,
            }

        except DerivAPIError as exc:
            reason = f"One-touch order failed: {exc.message} ({exc.code})."
            self._cancel(trade_id, signal_id, stake, reason)
            self.state.set_error(reason)
            self.disarm(reason)
            return None

        except Exception as exc:
            reason = f"Unexpected one-touch execution failure: {exc}"
            self._cancel(trade_id, signal_id, stake, reason)
            self.state.set_error(reason)
            self.disarm(reason)
            return None

    def _cancel(self, trade_id: str, signal_id: str, stake: float, reason: str) -> None:
        self.state.update_trade_outcome(trade_id, "CANCELLED", 0.0, reason)
        self._record_outcome(signal_id, "CANCELLED", 0.0, stake, None, note=reason)
        self.state.set_status(reason)

    def _record_evaluation(self, record: Optional[Dict[str, Any]]) -> None:
        if not self._journal or not record:
            return
        try:
            self._journal.record_evaluation(dict(record))
        except Exception:
            logger.exception("Journal evaluation write failed.")

    def _record_outcome(self, signal_id, outcome, pnl, stake, contract_id, note: str = "") -> None:
        if not self._journal or not signal_id:
            return
        try:
            self._journal.record_outcome(signal_id, outcome, pnl, stake, contract_id, self.execution_mode, 0, note=note)
        except Exception:
            logger.exception("Journal outcome write failed.")

    # ------------------------------------------------------------------ settlement

    async def _settle(self, info: Dict[str, Any]) -> None:
        contract_id = info["contract_id"]
        try:
            outcome, pnl = await self._monitor_contract(contract_id, info["buy_price"])

            if outcome == "UNKNOWN":
                reason = f"Contract {contract_id} settlement not confirmed; check the Deriv statement."
                self.state.update_trade_outcome(info["trade_id"], "UNKNOWN", 0.0, reason)
                self._record_outcome(info["signal_id"], "UNKNOWN", 0.0, info["stake"], contract_id, note=reason)
                self.state.set_error(reason)
                self.state.set_status(reason)
            else:
                self.state.update_trade_outcome(info["trade_id"], outcome, pnl)
                if outcome == "WON":
                    self.state.on_trade_win()
                    self.state.set_status(f"ONE TOUCH {self.barrier} WON · P&L {pnl:+.2f}")
                else:
                    self.state.on_trade_loss(1.0, 0)
                    self.state.set_status(f"ONE TOUCH {self.barrier} LOST · P&L {pnl:+.2f}")
                self._record_outcome(info["signal_id"], outcome, pnl, info["stake"], contract_id, note=info["source"])

        except Exception as exc:
            reason = f"Settlement monitoring failed: {exc}"
            self.state.update_trade_outcome(info["trade_id"], "UNKNOWN", 0.0, reason)
            self._record_outcome(info["signal_id"], "UNKNOWN", 0.0, info["stake"], contract_id, note=reason)
            self.state.set_error(reason)

        finally:
            with self._lock:
                self._settle_active = False
                self._armed = False
            self._active_contract_id = None
            self.state.set_running(False)

    async def _monitor_contract(self, contract_id: int, buy_price: float) -> Tuple[str, float]:
        expected = max(5.0, float(self.contract_duration) + 5.0)
        deadline = time.time() + max(25.0, expected + 25.0)
        fast_until = time.time() + max(3.0, expected + 3.0)

        while time.time() < deadline:
            if not self._order_client or not self._order_client.connected:
                await self._reconnect_client(self._order_client, subscribe=False)
                await asyncio.sleep(0.2)
                continue

            try:
                status = await self._order_client.get_open_contract_status(contract_id)

                name = str(status.get("status", "")).lower()
                expired = bool(status.get("is_expired", 0))
                sold = bool(status.get("is_sold", 0))

                if expired or sold or name in {"won", "lost", "sold"}:
                    raw_profit = status.get("profit")
                    try:
                        profit = float(raw_profit) if raw_profit is not None else float(status.get("sell_price", 0) or 0) - buy_price
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
