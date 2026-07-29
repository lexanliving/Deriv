"""
src/trading_engine.py
Candle-trend engine for CALL/PUT (UP/DOWN). Symbol validation is opportunistic
(catalogue is a hint, never a gate). No barriers. Every terminal trade state —
WON / LOST / CANCELLED / SKIPPED / UNKNOWN — is written to the journal with a
`note` explaining why, so the CSV is a complete decision + execution log.
"""
import asyncio
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from config import (
    CANDLE_GRANULARITIES, CANDLE_LOOKBACK, CANDLE_REFRESH_SECONDS,
    CONTRACT_DURATION, CONTRACT_DURATION_UNIT, CONTRACT_TYPE_BUY,
    CONTRACT_TYPE_SELL, CURRENCY, DEFAULT_STRATEGY_SENSITIVITY,
    ENTRY_SCORE_THRESHOLD, MARTINGALE_MULTIPLIER, MAX_TRADES_PER_DAY,
    STRATEGY_SENSITIVITY_PRESETS, SYMBOL, SYMBOL_DISPLAY,
)
from src.api_client import DerivAPIClient, DerivAPIError
from src.journal import get_journal
from src.logger import get_logger
from src.state_manager import StateManager, TradeRecord
from src.strategy import StrategyEngine

logger = get_logger("trading_engine")

INITIAL_WARMUP_COOLDOWN_SECONDS = 30.0
_DEMO_ACCOUNT_TYPES = {"DEMO", "VIRTUAL", "PRACTICE", "VIRTUAL_ACCOUNT"}
_REAL_ACCOUNT_TYPES = {"REAL", "LIVE", "REAL_MONEY"}


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
        self, api_token: str, app_id: str, account_id: str, account_currency: str,
        state: StateManager, initial_stake: float, max_martingale_steps: int,
        symbol: str = SYMBOL, symbol_display: str = SYMBOL_DISPLAY,
        contract_duration: int = CONTRACT_DURATION,
        contract_duration_unit: str = CONTRACT_DURATION_UNIT,
        strategy_sensitivity: str = DEFAULT_STRATEGY_SENSITIVITY,
        account_type: str = "UNKNOWN", real_execution_confirmed: bool = False,
        martingale_multiplier: float = MARTINGALE_MULTIPLIER,
    ):
        self.api_token = api_token.strip()
        self.app_id = app_id.strip()
        self.account_id = account_id.strip()
        self.account_currency = (account_currency or CURRENCY).upper()
        self.account_type = normalize_account_type(account_type)
        self.real_execution_confirmed = bool(real_execution_confirmed)
        self.execution_mode = resolve_execution_mode(self.account_type, self.real_execution_confirmed)
        self.state = state
        self.initial_stake = initial_stake
        self.max_martingale_steps = max_martingale_steps
        self.martingale_multiplier = martingale_multiplier
        self.symbol = str(symbol or SYMBOL).strip()
        self.symbol_display = symbol_display or self.symbol
        self.contract_duration = int(contract_duration)
        self.contract_duration_unit = str(contract_duration_unit or "m").lower()
        preset = STRATEGY_SENSITIVITY_PRESETS.get(
            strategy_sensitivity, STRATEGY_SENSITIVITY_PRESETS[DEFAULT_STRATEGY_SENSITIVITY])
        self._client: Optional[DerivAPIClient] = None
        self._strategy = StrategyEngine(
            entry_score_threshold=preset.get("entry_score_threshold", ENTRY_SCORE_THRESHOLD))
        self._journal = get_journal()
        self._signal_monotonic: float = 0.0
        self._last_candle_refresh: float = 0.0
        self._active_contract_id: Optional[int] = None
        self._trade_in_progress: bool = False
        self._reconnect_lock = asyncio.Lock()
        self._engine_ready_monotonic: float = 0.0
        self._last_strategy_state_version: int = 0
        self._daily_trade_count: int = 0
        self._daily_date = datetime.now(timezone.utc).date()

    async def _validate_symbol(self) -> bool:
        try:
            active_symbols = await self._client.get_active_symbols()
        except DerivAPIError as exc:
            logger.warning("Symbol catalogue unavailable (%s); proceeding and letting Deriv confirm.", exc)
            self.state.set_status(
                f"Symbol catalogue unreadable; subscribing to {self.symbol} and letting Deriv confirm...")
            return True
        all_syms = sorted({str(i.get("symbol", "")) for i in active_symbols if i.get("symbol")})
        if not all_syms:
            logger.warning("Deriv returned an empty symbol catalogue for this account; proceeding with %s.", self.symbol)
            self.state.set_status(
                f"No symbol catalogue from Deriv; subscribing to {self.symbol} and letting Deriv confirm...")
            return True
        if self.symbol in all_syms:
            match = next((i for i in active_symbols if str(i.get("symbol", "")) == self.symbol), None)
            if match is not None and bool(match.get("is_trading_suspended", False)):
                logger.warning("Symbol %s reports is_trading_suspended; proceeding anyway.", self.symbol)
                self.state.set_status(f"{self.symbol} looks suspended; attempting anyway - Deriv will confirm...")
            else:
                logger.info("Symbol %s confirmed in account catalogue.", self.symbol)
            return True
        logger.warning(
            "Symbol %s not listed in the account catalogue (%d symbols returned). Proceeding anyway. Sample: %s",
            self.symbol, len(all_syms), ", ".join(all_syms[:24]))
        self.state.set_status(
            f"{self.symbol} isn't in the catalogue Deriv returned; subscribing anyway - "
            "if it can't trade, Deriv's own message shows next.")
        return True

    async def run(self):
        logger.info(
            "Trading engine starting | market=%s | execution=%s | account_type=%s | duration=%d%s",
            self.symbol, self.execution_mode, self.account_type,
            self.contract_duration, self.contract_duration_unit)
        self.state.set_execution_context(
            account_id=self.account_id, account_type=self.account_type,
            currency=self.account_currency, execution_mode=self.execution_mode)
        if self.execution_mode == "BLOCKED":
            self.state.set_error(
                "Order execution is blocked: the selected account is real without an exact "
                "LIVE confirmation, or its account type is unknown.")
            self.state.set_status("Signal monitoring is active, but no orders can be sent.")
        else:
            self.state.set_status(f"Connecting to Deriv API for {self.execution_mode.lower()} order execution...")

        self._client = DerivAPIClient(self.api_token, self.app_id, self.account_id)
        connected = await self._client.connect()
        if not connected:
            detail = self._client.last_error if self._client else ""
            msg = (f"Failed to connect to Deriv API. "
                   f"{detail or 'Check your App ID, PAT scopes, and internet connection.'}")
            logger.error(msg)
            self.state.set_error(msg)
            self.state.set_status("Connection failed.")
            self.state.set_running(False)
            return

        await self._validate_symbol()
        self.state.set_status("Connected. Fetching initial candle data...")
        await self._refresh_candles()
        self._engine_ready_monotonic = time.monotonic()
        self._last_strategy_state_version = self._strategy.state_version
        self.state.set_status(
            f"Subscribed to {self.symbol_display}. Warming up for "
            f"{INITIAL_WARMUP_COOLDOWN_SECONDS:.0f}s before first trade...")
        try:
            await self._client.subscribe_ticks(self.symbol, self._on_tick)
            logger.info("Tick subscription active for %s.", self.symbol)
            while not self.state.stop_requested:
                await asyncio.sleep(1)
                if not self._client.connected:
                    await self._reconnect()
                self._roll_daily_trade_count()
                if time.time() - self._last_candle_refresh > CANDLE_REFRESH_SECONDS:
                    await self._refresh_candles()
                if self._trade_in_progress:
                    self._strategy.consume_signal()
                    busy_sid = self._strategy.last_consumed_signal_id
                    if busy_sid:
                        self._journal.record_outcome(
                            busy_sid, "SKIPPED", 0.0, 0.0, None, self.execution_mode,
                            note="trade in progress")
                    continue
                signal = self._strategy.consume_signal()
                if signal in ("BUY", "SELL"):
                    signal_id = self._strategy.last_consumed_signal_id
                    now_mono = time.monotonic()
                    allowed, gate_reason = self._gate_allows(now_mono)
                    if allowed:
                        self._signal_monotonic = now_mono
                        entry_price = self.state.current_price or self._strategy.get_current_price()
                        self.state.set_status(f"Signal: {signal} on {self.symbol_display}. Placing trade...")
                        await self._execute_trade(signal, entry_price, signal_id)
                    else:
                        logger.info("Signal %s seen but not executed: %s", signal, gate_reason)
                        self.state.set_status(f"Signal {signal} seen, waiting: {gate_reason}")
                        self._strategy.on_signal_skipped()
                        self._journal.record_outcome(
                            signal_id, "SKIPPED", 0.0, 0.0, None, self.execution_mode,
                            note=gate_reason)
        except DerivAPIError as e:
            logger.error("API error in main loop: %s", e)
            self.state.set_error(str(e))
        except Exception as e:
            logger.exception("Unexpected error in main loop: %s", e)
            self.state.set_error(f"Unexpected error: {e}")
        finally:
            await self._shutdown()

    def _roll_daily_trade_count(self) -> None:
        today = datetime.now(timezone.utc).date()
        if today != self._daily_date:
            self._daily_date = today
            self._daily_trade_count = 0

    def _gate_allows(self, now_mono: float) -> Tuple[bool, str]:
        if self._engine_ready_monotonic == 0.0:
            return False, "engine not ready"
        if self._daily_trade_count >= MAX_TRADES_PER_DAY:
            return False, f"daily trade cap ({MAX_TRADES_PER_DAY}) reached"
        warmup_remaining = INITIAL_WARMUP_COOLDOWN_SECONDS - (now_mono - self._engine_ready_monotonic)
        if warmup_remaining > 0:
            return False, f"warm-up ({warmup_remaining:.0f}s remaining)"
        cooldown_remaining = self.state.get_cooldown_remaining()
        if cooldown_remaining > 0:
            return False, f"pacing ({cooldown_remaining:.0f}s remaining)"
        return True, "signal allowed"

    async def _reconnect(self, max_attempts: int = 5) -> bool:
        async with self._reconnect_lock:
            if self._client.connected:
                return True
            for attempt in range(1, max_attempts + 1):
                self.state.set_status(f"Connection lost. Reconnecting ({attempt}/{max_attempts})...")
                try:
                    await self._client.disconnect(cancel_callbacks=False)
                except Exception:
                    logger.debug("Error while clearing the old Deriv socket.", exc_info=True)
                try:
                    if await self._client.connect():
                        self._engine_ready_monotonic = time.monotonic()
                        await self._client.subscribe_ticks(self.symbol, self._on_tick)
                        self._last_candle_refresh = 0.0
                        self.state.set_status("Reconnected to Deriv. Bot is active.")
                        logger.info("Reconnected to Deriv successfully.")
                        return True
                except DerivAPIError as exc:
                    logger.warning("Reconnect attempt %d failed: %s", attempt, exc)
                if attempt < max_attempts:
                    await asyncio.sleep(min(2**attempt, 15))
            self.state.set_error("Could not reconnect to Deriv after repeated attempts.")
            return False

    async def _shutdown(self):
        logger.info("Trading engine shutting down...")
        if self._trade_in_progress:
            self.state.set_status("Stop requested. Finishing the active contract check...")
            deadline = time.monotonic() + max(90.0, self._contract_duration_seconds() + 180.0)
            while self._trade_in_progress and time.monotonic() < deadline:
                await asyncio.sleep(0.2)
            if self._trade_in_progress:
                logger.critical("Shutdown safety wait expired while a contract was active.")
                self.state.set_error("Shutdown could not confirm the active contract. Check the Deriv statement.")
        if self._client:
            await self._client.unsubscribe_ticks()
            await self._client.disconnect()
        self.state.set_running(False)
        self.state.set_status("Bot stopped.")
        logger.info("Trading engine stopped.")

    async def _on_tick(self, tick_data: Dict[str, Any]):
        try:
            price = float(tick_data.get("quote", 0))
            epoch = float(tick_data.get("epoch", time.time()))
            if price == 0:
                return
            self._strategy.process_tick(price)
            self._push_tick_and_strategy_state(price, epoch)
        except Exception as e:
            logger.exception("Error processing tick: %s", e)

    def _push_tick_and_strategy_state(self, price: float, epoch: float) -> None:
        current_version = self._strategy.state_version
        if current_version != self._last_strategy_state_version:
            strategy_state = self._strategy.get_state()
            self._last_strategy_state_version = current_version
            self.state.update_tick_and_strategy_state(
                price, epoch,
                trend_direction=strategy_state["trend_direction"],
                trend_tick_count=strategy_state["trend_tick_count"],
                trend_kind=strategy_state["trend_kind"],
                trades_in_trend=strategy_state["trades_in_trend"],
                in_cooldown=strategy_state["in_cooldown"],
                pattern_stage=strategy_state["pattern_stage"],
                mtf_bias=strategy_state["mtf_bias"],
                mtf_agreement=strategy_state["mtf_agreement"],
                mtf_tf_biases=strategy_state.get("mtf_tf_biases", {}),
                micro_bias=strategy_state.get("micro_bias"),
                last_entry_mode=strategy_state.get("last_entry_mode"),
                last_signal_score=strategy_state["last_signal_score"],
                last_signal_score_breakdown=strategy_state["last_signal_score_breakdown"])
        else:
            self.state.update_tick(price, epoch)

    async def _refresh_candles(self):
        self.state.set_status("Refreshing candle data...")
        candles_by_tf: Dict[str, list] = {}
        try:
            for tf, granularity in CANDLE_GRANULARITIES.items():
                candles_by_tf[tf] = await self._client.get_candles(self.symbol, granularity, CANDLE_LOOKBACK)
            now = time.time()
            self._strategy.update_candles(candles_by_tf, now)

            eval_record = self._strategy.get_last_evaluation()
            if eval_record:
                eval_record["timestamp_utc"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                eval_record["symbol"] = self.symbol
                self._journal.record_evaluation(eval_record)

            self._last_candle_refresh = now
            strategy_state = self._strategy.get_state()
            self._last_strategy_state_version = self._strategy.state_version
            self.state.update_strategy_state(
                trend_direction=strategy_state["trend_direction"],
                trend_tick_count=strategy_state["trend_tick_count"],
                trend_kind=strategy_state["trend_kind"],
                trades_in_trend=strategy_state["trades_in_trend"],
                in_cooldown=strategy_state["in_cooldown"],
                pattern_stage=strategy_state["pattern_stage"],
                mtf_bias=strategy_state["mtf_bias"],
                mtf_agreement=strategy_state["mtf_agreement"],
                mtf_tf_biases=strategy_state.get("mtf_tf_biases", {}),
                micro_bias=strategy_state.get("micro_bias"),
                last_entry_mode=strategy_state.get("last_entry_mode"),
                last_signal_score=strategy_state["last_signal_score"],
                last_signal_score_breakdown=strategy_state["last_signal_score_breakdown"])
            price = self._strategy.get_current_price()
            if price > 0:
                self.state.update_tick(price, now)
            trend = strategy_state.get("trend_direction") or "No clear trend"
            stage = strategy_state.get("pattern_stage") or "IDLE"
            self.state.set_status(f"{self.symbol_display} | Trend: {trend} | Stage: {stage}")
        except DerivAPIError as e:
            logger.warning("Candle refresh failed: %s", e)
            self.state.set_status("Candle refresh failed. Using last known state.")
        except Exception as e:
            logger.exception("Unexpected error during candle refresh: %s", e)

    def _contract_duration_seconds(self) -> float:
        unit = self.contract_duration_unit.lower()
        if unit == "t":
            return max(5.0, float(self.contract_duration))
        if unit == "m":
            return float(self.contract_duration) * 60.0
        if unit == "h":
            return float(self.contract_duration) * 3600.0
        if unit == "d":
            return float(self.contract_duration) * 86400.0
        return 1800.0

    async def _execute_trade(self, signal: str, entry_price: float, signal_id: Optional[str] = None):
        if self._trade_in_progress:
            logger.warning("Trade already in progress. Skipping signal.")
            return
        self._trade_in_progress = True
        martingale_state = self.state.get_martingale_state()
        stake = martingale_state["stake"]
        martingale_step = martingale_state["step"]
        contract_type = CONTRACT_TYPE_BUY if signal == "BUY" else CONTRACT_TYPE_SELL
        trade_id = str(uuid.uuid4())[:8]
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        logger.info(
            "Handling %s signal | Market=%s | Mode=%s | Stake=%s | Duration=%d%s | Step=%s | TradeID=%s",
            signal, self.symbol, self.execution_mode, stake,
            self.contract_duration, self.contract_duration_unit, martingale_step, trade_id)

        if self.execution_mode == "BLOCKED":
            reason = ("Order blocked: select a recognised DEMO account, or type LIVE exactly "
                      "to enable orders on a REAL account. No proposal or buy request was sent.")
            self.state.add_trade(TradeRecord(
                trade_id=trade_id, direction=signal, stake=stake, barrier="-",
                entry_price=entry_price, timestamp=timestamp, status="CANCELLED",
                martingale_step=martingale_step, execution_mode="BLOCKED",
                account_type=self.account_type, error_message=reason))
            self._strategy.on_trade_executed()
            self.state.set_error(reason)
            self.state.set_status(f"Signal: {signal}. Order blocked by safety gate.")
            self._journal.record_outcome(
                signal_id, "CANCELLED", 0.0, stake, None, "BLOCKED", martingale_step, note=reason)
            self._trade_in_progress = False
            return

        trade_record = TradeRecord(
            trade_id=trade_id, direction=signal, stake=stake, barrier="-",
            entry_price=entry_price, timestamp=timestamp, status="OPEN",
            martingale_step=martingale_step, execution_mode=self.execution_mode,
            account_type=self.account_type)
        self.state.add_trade(trade_record)
        self._strategy.on_trade_executed()
        self.state.clear_error()
        execution_stage = "proposal request"

        if not self._client.connected:
            if not await self._reconnect():
                reason = "Order not sent: could not restore Deriv connection before the proposal request."
                self.state.update_trade_outcome(trade_id, "CANCELLED", 0.0, reason)
                self.state.set_error(reason)
                self.state.set_status("Order cancelled: connection unavailable.")
                self._journal.record_outcome(
                    signal_id, "CANCELLED", 0.0, stake, None, self.execution_mode,
                    martingale_step, note=reason)
                self._trade_in_progress = False
                return

        try:
            self.state.set_status(
                f"{self.execution_mode} order: preparing {signal} "
                f"(stake {stake:.2f} {self.account_currency}, {self.contract_duration}{self.contract_duration_unit})...")
            self.state.update_trade_pacing()
            proposal = await self._client.get_proposal(
                symbol=self.symbol, contract_type=contract_type, stake=stake,
                duration=self.contract_duration, duration_unit=self.contract_duration_unit,
                barrier=None, currency=self.account_currency)
            proposal_id = proposal.get("id")
            if not isinstance(proposal_id, str) or not proposal_id:
                raise DerivAPIError("Deriv did not return a valid proposal ID.", "INVALID_RESPONSE")
            try:
                ask_price = float(proposal["ask_price"])
            except (KeyError, TypeError, ValueError) as exc:
                raise DerivAPIError("Deriv did not return a valid proposal ask price.", "INVALID_RESPONSE") from exc
            if ask_price <= 0:
                raise DerivAPIError("Deriv returned a non-positive proposal ask price.", "INVALID_RESPONSE")
            if self.state.stop_requested:
                raise DerivAPIError("Stop was requested before the buy; no order was submitted.", "STOP_REQUESTED")

            execution_stage = "buy request"
            self.state.set_status(f"Proposal received. Submitting {self.execution_mode.lower()} buy request...")
            try:
                buy_response = await self._client.buy_contract(proposal_id=proposal_id, price=ask_price)
            except DerivAPIError as buy_exc:
                if buy_exc.code in ("TIMEOUT", "CONNECTION_LOST", "INVALID_RESPONSE"):
                    buy_response = await self._reconcile_after_buy_timeout(
                        stake=stake, contract_type=contract_type, signal_time=time.time())
                    if buy_response is None:
                        raise DerivAPIError(
                            "The buy request may have reached Deriv, but no receipt or matching open "
                            "contract could be confirmed. Check the Deriv statement before restarting.",
                            "AMBIGUOUS_BUY") from buy_exc
                else:
                    raise

            contract_id = buy_response.get("contract_id")
            buy_price = float(buy_response.get("buy_price", stake))
            payout = float(buy_response.get("payout", 0))
            if not contract_id:
                raise DerivAPIError("No contract ID in buy response.", "NO_CONTRACT_ID")
            self._active_contract_id = contract_id
            trade_record.contract_id = contract_id
            self._daily_trade_count += 1
            fill_latency = time.monotonic() - self._signal_monotonic if self._signal_monotonic > 0 else -1.0
            logger.info(
                "Contract %s bought | Market=%s | Buy Price=%s | Payout=%s | TradeID=%s | latency=%.3fs",
                contract_id, self.symbol, buy_price, payout, trade_id, fill_latency)
            self.state.set_status(f"{self.execution_mode} contract {contract_id} active | Waiting for settlement...")
            outcome, pnl = await self._monitor_contract(contract_id, buy_price, payout)
            if outcome == "UNKNOWN":
                reason = (f"Contract {contract_id} was bought but settlement was not confirmed. "
                          "Check the Deriv statement; Martingale was not changed.")
                self.state.update_trade_outcome(trade_id, "UNKNOWN", 0.0, reason)
                self.state.set_error(reason)
                self.state.set_status("Trading stopped: unresolved contract outcome.")
                self.state.request_stop()
                self._journal.record_outcome(
                    signal_id, "UNKNOWN", 0.0, stake, self._active_contract_id,
                    self.execution_mode, martingale_step, note=reason)
            else:
                self.state.update_trade_outcome(trade_id, outcome, pnl)
                if outcome == "WON":
                    self.state.on_trade_win()
                    self.state.set_status(f"Trade WON! P&L: +{pnl:.2f}")
                else:
                    self.state.on_trade_loss(self.martingale_multiplier, self.max_martingale_steps)
                    new_stake = self.state.get_martingale_state()["stake"]
                    self.state.set_status(
                        f"Trade LOST. Next stake: {new_stake:.2f} (Step {self.state.get_martingale_state()['step']})")
                self._journal.record_outcome(
                    signal_id, outcome, pnl, stake, self._active_contract_id,
                    self.execution_mode, martingale_step, note="")
        except DerivAPIError as e:
            if e.code == "AMBIGUOUS_BUY":
                reason = e.message
                self.state.update_trade_outcome(trade_id, "UNKNOWN", 0.0, reason)
                self.state.set_error(reason)
                self.state.set_status("Trading stopped: a buy outcome is ambiguous. Check the Deriv statement.")
                self.state.request_stop()
                self._journal.record_outcome(
                    signal_id, "UNKNOWN", 0.0, stake, self._active_contract_id,
                    self.execution_mode, martingale_step, note=reason)
            elif self._active_contract_id is not None:
                reason = (f"Contract {self._active_contract_id} was bought, but monitoring failed: "
                          f"{e.message} ({e.code}). Check the Deriv statement; Martingale was not changed.")
                self.state.update_trade_outcome(trade_id, "UNKNOWN", 0.0, reason)
                self.state.set_error(reason)
                self.state.set_status("Trading stopped: a purchased contract outcome is unresolved.")
                self.state.request_stop()
                self._journal.record_outcome(
                    signal_id, "UNKNOWN", 0.0, stake, self._active_contract_id,
                    self.execution_mode, martingale_step, note=reason)
            else:
                reason = f"Deriv rejected the {execution_stage}: {e.message} ({e.code})."
                self.state.update_trade_outcome(trade_id, "CANCELLED", 0.0, reason)
                self.state.set_error(reason)
                self.state.set_status(f"Order cancelled during {execution_stage}: {e.message}")
                self._journal.record_outcome(
                    signal_id, "CANCELLED", 0.0, stake, None, self.execution_mode,
                    martingale_step, note=reason)
        except asyncio.CancelledError:
            if self._active_contract_id is not None:
                reason = f"Monitoring for contract {self._active_contract_id} was interrupted. Check the Deriv statement."
                self.state.update_trade_outcome(trade_id, "UNKNOWN", 0.0, reason)
                self.state.set_error(reason)
                self.state.request_stop()
                self._journal.record_outcome(
                    signal_id, "UNKNOWN", 0.0, stake, self._active_contract_id,
                    self.execution_mode, martingale_step, note=reason)
            else:
                reason = "Order attempt was stopped before any contract was confirmed."
                self.state.update_trade_outcome(trade_id, "CANCELLED", 0.0, reason)
                self._journal.record_outcome(
                    signal_id, "CANCELLED", 0.0, stake, None, self.execution_mode,
                    martingale_step, note=reason)
            raise
        except Exception as e:
            if self._active_contract_id is not None:
                reason = f"Unexpected monitoring failure for contract {self._active_contract_id}: {e}. Check the Deriv statement."
                self.state.update_trade_outcome(trade_id, "UNKNOWN", 0.0, reason)
                self.state.set_error(reason)
                self.state.set_status("Trading stopped: a purchased contract outcome is unresolved.")
                self.state.request_stop()
                self._journal.record_outcome(
                    signal_id, "UNKNOWN", 0.0, stake, self._active_contract_id,
                    self.execution_mode, martingale_step, note=reason)
            else:
                reason = f"Unexpected failure during the {execution_stage}: {e}"
                self.state.update_trade_outcome(trade_id, "CANCELLED", 0.0, reason)
                self.state.set_error(reason)
                self.state.set_status(f"Order cancelled during {execution_stage}; see error details.")
                self._journal.record_outcome(
                    signal_id, "CANCELLED", 0.0, stake, None, self.execution_mode,
                    martingale_step, note=reason)
        finally:
            self._active_contract_id = None
            self._trade_in_progress = False

    async def _reconcile_after_buy_timeout(self, stake, contract_type, signal_time) -> Optional[Dict[str, Any]]:
        logger.warning("Buy receipt not confirmed. Checking the portfolio for a matching untracked fill.")
        known_ids = {str(trade.contract_id) for trade in self.state.get_trade_history()
                     if getattr(trade, "contract_id", None) is not None}
        for attempt in range(1, 4):
            if attempt > 1:
                await asyncio.sleep(1.0)
            if not self._client.connected:
                if not await self._reconnect(max_attempts=2):
                    continue
            try:
                contracts = await self._client.get_portfolio()
            except DerivAPIError as exc:
                logger.warning("Portfolio reconciliation attempt %d/3 failed: %s", attempt, exc)
                continue
            matches = []
            for contract in contracts:
                contract_id = contract.get("contract_id")
                if isinstance(contract_id, bool) or not isinstance(contract_id, int):
                    continue
                if str(contract_id) in known_ids:
                    continue
                if contract.get("underlying_symbol") != self.symbol:
                    continue
                if contract.get("contract_type") != contract_type:
                    continue
                try:
                    buy_price = float(contract["buy_price"])
                    purchase_time = float(contract["purchase_time"])
                    payout = float(contract.get("payout", 0.0))
                except (KeyError, TypeError, ValueError):
                    continue
                if abs(buy_price - stake) > max(0.01, stake * 0.05):
                    continue
                if not signal_time - 30.0 <= purchase_time <= time.time() + 10.0:
                    continue
                matches.append({"contract_id": contract_id, "buy_price": buy_price, "payout": payout})
            if len(matches) == 1:
                logger.warning("Recovered untracked contract %s.", matches[0]["contract_id"])
                return matches[0]
            if len(matches) > 1:
                logger.error("Multiple possible fills found; automatic adoption would be unsafe.")
                return None
        logger.error("No unique matching contract found. Trading must stay stopped until the statement is checked.")
        return None

    async def _monitor_contract(self, contract_id, buy_price, payout):
        duration_seconds = self._contract_duration_seconds()
        poll_interval = 1.0 if duration_seconds <= 30 else (5.0 if duration_seconds <= 600 else 15.0)
        max_wait = duration_seconds + 180.0
        start_time = time.time()
        logger.info("Monitoring contract %s | expected %.0fs | poll %.0fs", contract_id, duration_seconds, poll_interval)
        while time.time() - start_time < max_wait:
            if not self._client.connected:
                if not await self._reconnect(max_attempts=2):
                    await asyncio.sleep(poll_interval * 2)
                    continue
            try:
                status = await self._client.get_open_contract_status(contract_id)
                is_expired = bool(status.get("is_expired", 0))
                is_sold = bool(status.get("is_sold", 0))
                status_name = str(status.get("status", "")).lower()
                if is_expired or is_sold or status_name in {"won", "lost", "sold"}:
                    sell_price = float(status.get("sell_price", 0) or 0)
                    raw_profit = status.get("profit")
                    profit = float(raw_profit) if raw_profit is not None else sell_price - buy_price
                    if status_name == "won" or profit > 0:
                        return "WON", round(profit, 2)
                    return "LOST", round(profit, 2)
                await asyncio.sleep(poll_interval)
            except DerivAPIError as exc:
                logger.warning("Error polling contract %s: %s", contract_id, exc)
                await asyncio.sleep(poll_interval * 2)
            except (TypeError, ValueError) as exc:
                logger.warning("Invalid settlement numbers for contract %s: %s", contract_id, exc)
                await asyncio.sleep(poll_interval * 2)
        logger.warning("Contract %s monitoring timed out.", contract_id)
        return "UNKNOWN", 0.0