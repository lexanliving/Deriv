"""Live execution engine for the rolling digit Over 6 strategy.

The engine is intentionally separate from the candle strategy. It consumes raw
quotes, asks the strategy for a digit signal, requests a live proposal, and
only buys after the quote and execution gates pass.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, Optional

from config import CURRENCY, DEFAULT_INITIAL_STAKE, MAX_TRADES_PER_DAY, MARTINGALE_MULTIPLIER
from src.api_client import DerivAPIClient, DerivAPIError
from src.digit_strategy import DigitStrategyEngine
from src.journal import get_journal
from src.logger import get_logger
from src.state_manager import StateManager, TradeRecord

logger = get_logger("trading_engine")

INITIAL_WARMUP_COOLDOWN_SECONDS = 10.0
DIGIT_CONTRACT_TYPE_OVER = "DIGITOVER"
DIGIT_CONTRACT_TYPE_UNDER = "DIGITUNDER"

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
        self,
        api_token: str,
        app_id: str,
        account_id: str,
        account_currency: str,
        state: StateManager,
        initial_stake: float = DEFAULT_INITIAL_STAKE,
        max_martingale_steps: int = 0,
        symbol: str = "1HZ10V",
        symbol_display: str = "Volatility 10 (1s)",
        contract_duration: int = 1,
        contract_duration_unit: str = "t",
        account_type: str = "UNKNOWN",
        real_execution_confirmed: bool = False,
        martingale_multiplier: float = MARTINGALE_MULTIPLIER,
        quote_precision: int = 2,
        min_over6_share: float = 0.35,
        lower_tick_max: int = 6,
        review_interval_seconds: float = 60.0,
        require_quote_edge: bool = True,
        min_quote_edge: float = 0.02,
        max_session_loss: float = 0.0,
        digit_windows: Optional[Dict[str, int]] = None,
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
        self.initial_stake = float(initial_stake)
        self.max_martingale_steps = max(0, int(max_martingale_steps))
        self.martingale_multiplier = float(martingale_multiplier)
        self.symbol = str(symbol or "1HZ10V").strip()
        self.symbol_display = str(symbol_display or self.symbol)
        self.contract_duration = max(1, int(contract_duration))
        self.contract_duration_unit = str(contract_duration_unit or "t").lower()
        self.barrier = "6"
        self.quote_precision = max(0, int(quote_precision))
        self.min_over6_share = float(min_over6_share)
        self.lower_tick_max = int(lower_tick_max)
        self.review_interval_seconds = float(review_interval_seconds)
        self.require_quote_edge = bool(require_quote_edge)
        self.min_quote_edge = float(min_quote_edge)
        self.max_session_loss = max(0.0, float(max_session_loss))
        self._client: Optional[DerivAPIClient] = None
        self._strategy = DigitStrategyEngine(
            contract_duration_ticks=self.contract_duration,
            quote_precision=self.quote_precision,
            windows=digit_windows,
            min_over6_share=self.min_over6_share,
            low_digit_max=self.lower_tick_max,
            review_interval_seconds=self.review_interval_seconds,
        )
        self._journal = get_journal()
        self._active_contract_id: Optional[int] = None
        self._trade_in_progress = False
        self._signal_monotonic = 0.0
        self._engine_ready_monotonic = 0.0
        self._last_strategy_state_version = 0
        self._last_pushed_tick_epoch: Optional[float] = None
        self._daily_trade_count = 0
        self._daily_date = datetime.now(timezone.utc).date()
        self._reconnect_lock = asyncio.Lock()

    # ------------------------------------------------------------- utilities
    @staticmethod
    def _precision_from_symbol(symbol_info: Dict[str, Any], fallback: int = 2) -> int:
        for key in ("pip_size", "pip", "pipSize"):
            value = symbol_info.get(key)
            if value in (None, ""):
                continue
            try:
                numeric = float(value)
            except (TypeError, ValueError):
                continue
            if 0 < numeric < 1:
                return max(0, min(10, -Decimal(str(numeric)).as_tuple().exponent))
            if numeric.is_integer() and 0 <= numeric <= 10:
                # Some Deriv responses expose the number of decimal places;
                # others expose pip itself. This is the useful interpretation
                # for digit extraction and is checked against live quotes.
                return int(numeric)
        return fallback

    def _contract_duration_seconds(self) -> float:
        if self.contract_duration_unit == "t":
            return max(2.0, float(self.contract_duration))
        if self.contract_duration_unit == "s":
            return max(2.0, float(self.contract_duration))
        if self.contract_duration_unit == "m":
            return max(2.0, float(self.contract_duration) * 60.0)
        return max(2.0, float(self.contract_duration))

    def _roll_daily_trade_count(self) -> None:
        today = datetime.now(timezone.utc).date()
        if today != self._daily_date:
            self._daily_date = today
            self._daily_trade_count = 0

    def _gate_allows(self, now_mono: float) -> tuple[bool, str]:
        if self._engine_ready_monotonic == 0.0:
            return False, "starting up"
        if self._daily_trade_count >= MAX_TRADES_PER_DAY:
            return False, f"daily trade cap ({MAX_TRADES_PER_DAY}) reached"
        if self.max_session_loss > 0:
            pnl = float(self.state.get_performance_stats().get("total_pnl", 0.0))
            if pnl <= -self.max_session_loss:
                return False, f"session loss stop reached ({pnl:.2f})"
        warmup = INITIAL_WARMUP_COOLDOWN_SECONDS - (now_mono - self._engine_ready_monotonic)
        if warmup > 0:
            return False, f"warming up, {warmup:.0f}s left"
        cooldown = self.state.get_cooldown_remaining()
        if cooldown > 0:
            return False, f"cooling down, {cooldown:.0f}s left"
        return True, "ready"

    def _push_strategy_state(self, price: Optional[float] = None, epoch: Optional[float] = None) -> None:
        strategy_state = self._strategy.get_state()
        self._last_strategy_state_version = self._strategy.state_version
        if price is None:
            price = self._strategy.get_current_price()
        if epoch is None:
            epoch = self._last_pushed_tick_epoch if self._last_pushed_tick_epoch is not None else time.time()
        epoch = float(epoch)
        if self._last_pushed_tick_epoch != epoch:
            self.state.update_tick_and_strategy_state(float(price or 0.0), epoch, **strategy_state)
            self._last_pushed_tick_epoch = epoch
        else:
            self.state.update_strategy_state(**strategy_state)

    def _record_evaluation(self, record: Optional[Dict[str, Any]]) -> None:
        if not record:
            return
        record = dict(record)
        record["symbol"] = self.symbol
        self._journal.record_evaluation(record)

    def _record_entry_decision(
        self,
        entry_record: Optional[Dict[str, Any]],
        taken: bool,
        reason: str = "",
        ask_price: Any = "",
        payout: Any = "",
        break_even: Any = "",
        quote_edge: Any = "",
    ) -> None:
        if not entry_record:
            return
        record = dict(entry_record)
        record["symbol"] = self.symbol
        record["taken"] = "TRUE" if taken else "FALSE"
        record["executed"] = "FALSE"
        record["rejection_reason"] = reason
        record["note"] = record.get("note", "") or ""
        record["quote_ask"] = ask_price
        record["quote_payout"] = payout
        record["quote_break_even"] = break_even
        record["quote_edge"] = quote_edge
        self._journal.record_evaluation(record)

    # --------------------------------------------------------------- lifecycle
    async def _validate_symbol(self) -> None:
        try:
            symbols = await self._client.get_active_symbols(full=True)
            match = next((s for s in symbols if str(s.get("symbol", "")) == self.symbol), None)
            if match:
                self.quote_precision = self._precision_from_symbol(match, self.quote_precision)
                if self.quote_precision != self._strategy.quote_precision:
                    self._strategy.quote_precision = self.quote_precision
                logger.info("Symbol %s confirmed; quote precision=%d.", self.symbol, self.quote_precision)
                return
            logger.warning("Symbol %s was not in the returned catalogue; Deriv will confirm.", self.symbol)
        except Exception as exc:
            logger.warning("Symbol catalogue unavailable: %s", exc)

    async def _seed_history(self) -> None:
        try:
            history = await self._client.get_ticks_history(self.symbol, count=500)
            self._strategy.seed_ticks(history.get("prices", []), history.get("times", []))
            self._push_strategy_state()
            logger.info("Seeded %d historical ticks for %s.", len(history.get("prices", [])), self.symbol)
        except DerivAPIError as exc:
            logger.warning("Initial tick history unavailable: %s", exc)
            self.state.set_status("Connected; waiting for enough live ticks to review digits…")

    async def run(self) -> None:
        logger.info("Digit engine start | market=%s mode=%s duration=%dt barrier=%s", self.symbol,
                    self.execution_mode, self.contract_duration, self.barrier)
        self.state.set_execution_context(
            account_id=self.account_id,
            account_type=self.account_type,
            currency=self.account_currency,
            execution_mode=self.execution_mode,
        )
        if self.execution_mode == "BLOCKED":
            self.state.set_error("Trading is paused: type LIVE on a real account to enable orders, or use a demo account.")
            self.state.set_status("Monitoring only — no orders will be sent.")
        else:
            self.state.set_status(f"Connecting to Deriv ({self.execution_mode.lower()})…")

        self._client = DerivAPIClient(self.api_token, self.app_id, self.account_id)
        if not await self._client.connect():
            detail = self._client.last_error if self._client else ""
            message = f"Failed to connect to Deriv API. {detail or 'Check App ID, PAT scopes, and internet connection.'}"
            self.state.set_error(message)
            self.state.set_status("Connection failed.")
            self.state.set_running(False)
            return

        await self._validate_symbol()
        await self._seed_history()
        self._engine_ready_monotonic = time.monotonic()
        self.state.set_status(
            f"Live digit review on {self.symbol_display}. Reviewing every minute; first entry possible in "
            f"{INITIAL_WARMUP_COOLDOWN_SECONDS:.0f}s."
        )

        try:
            await self._client.subscribe_ticks(self.symbol, self._on_tick)
            while not self.state.stop_requested:
                await asyncio.sleep(0.5)
                if not self._client.connected:
                    await self._reconnect()
                self._roll_daily_trade_count()

                review = self._strategy.review_if_due()
                if review:
                    self._record_evaluation(review)
                    fast = (review.get("p_over6_fast") or 0) * 100
                    medium = (review.get("p_over6_medium") or 0) * 100
                    self.state.set_status(
                        f"Digit review · 7–9 {fast:.1f}% fast / {medium:.1f}% medium · "
                        f"{review.get('rejection_reason') or 'armed for lower-tick confirmation'}"
                    )

                self._push_strategy_state()

                if self._trade_in_progress:
                    busy = self._strategy.consume_signal()
                    if busy:
                        self._record_evaluation({
                            "signal_id": self._strategy.last_consumed_signal_id or "",
                            "timestamp_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                            "symbol": self.symbol,
                            "direction": busy,
                            "taken": "FALSE",
                            "executed": "FALSE",
                            "rejection_reason": "a trade was already open in this tab",
                            "strategy_mode": "DIGIT_OVER_6",
                            "barrier": self.barrier,
                            "duration_unit": "t",
                            "regime": "DIGIT",
                        })
                    continue

                signal = self._strategy.consume_signal()
                if signal != "OVER6":
                    continue
                signal_id = self._strategy.last_consumed_signal_id
                entry_record = self._strategy.get_entry_evaluation()
                allowed, gate_reason = self._gate_allows(time.monotonic())
                if not allowed:
                    self._record_entry_decision(entry_record, False, gate_reason)
                    self._strategy.on_signal_skipped()
                    self.state.set_status(f"Over 6 setup held — standing by ({gate_reason})")
                    continue

                self._signal_monotonic = time.monotonic()
                self.state.set_status("Over 6 confirmed — requesting live 1–2 tick quote…")
                await self._execute_trade(signal, entry_record, signal_id)
        except DerivAPIError as exc:
            logger.error("API error in main loop: %s", exc)
            self.state.set_error(str(exc))
        except Exception as exc:
            logger.exception("Unexpected error in main loop: %s", exc)
            self.state.set_error(f"Unexpected error: {exc}")
        finally:
            await self._shutdown()

    async def _reconnect(self, max_attempts: int = 5) -> bool:
        async with self._reconnect_lock:
            if self._client and self._client.connected:
                return True
            for attempt in range(1, max_attempts + 1):
                self.state.set_status(f"Connection lost. Reconnecting ({attempt}/{max_attempts})…")
                try:
                    if self._client:
                        await self._client.disconnect(cancel_callbacks=False)
                    if self._client and await self._client.connect():
                        self._engine_ready_monotonic = time.monotonic()
                        await self._client.subscribe_ticks(self.symbol, self._on_tick)
                        self.state.set_status("Reconnected to Deriv. Digit bot is active.")
                        return True
                except Exception as exc:
                    logger.warning("Reconnect %d failed: %s", attempt, exc)
                if attempt < max_attempts:
                    await asyncio.sleep(min(2 ** attempt, 15))
            self.state.set_error("Could not reconnect to Deriv after repeated attempts.")
            return False

    async def _shutdown(self) -> None:
        logger.info("Digit engine shutting down…")
        if self._trade_in_progress:
            self.state.set_status("Stopping — letting the open digit contract finish…")
            deadline = time.monotonic() + max(30.0, self._contract_duration_seconds() + 30.0)
            while self._trade_in_progress and time.monotonic() < deadline:
                await asyncio.sleep(0.2)
        if self._client:
            try:
                await self._client.unsubscribe_ticks()
                await self._client.disconnect()
            except Exception:
                logger.debug("Error during Deriv disconnect.", exc_info=True)
        self.state.set_running(False)
        self.state.set_status("Stopped.")

    # ------------------------------------------------------------------ ticks
    async def _on_tick(self, tick_data: Dict[str, Any]) -> None:
        try:
            price = float(tick_data.get("quote", 0))
            epoch = float(tick_data.get("epoch", time.time()))
            if price <= 0:
                return
            self._strategy.process_tick(price, epoch)
            self._push_strategy_state(price, epoch)
        except Exception as exc:
            logger.exception("Error processing digit tick: %s", exc)

    # -------------------------------------------------------------- execution
    async def _execute_trade(self, signal: str, entry_record: Optional[Dict[str, Any]], signal_id: Optional[str]) -> None:
        if self._trade_in_progress:
            return
        self._trade_in_progress = True
        martingale = self.state.get_martingale_state()
        stake = float(martingale["stake"])
        martingale_step = int(martingale["step"])
        trade_id = str(uuid.uuid4())[:8]
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        contract_type = DIGIT_CONTRACT_TYPE_OVER if signal == "OVER6" else DIGIT_CONTRACT_TYPE_UNDER
        trade_record = TradeRecord(
            trade_id=trade_id,
            signal_id=signal_id or "",
            direction=signal,
            stake=stake,
            barrier=self.barrier,
            entry_price=self._strategy.get_current_price(),
            timestamp=timestamp,
            status="OPEN",
            martingale_step=martingale_step,
            execution_mode=self.execution_mode,
            account_type=self.account_type,
        )
        self.state.add_trade(trade_record)
        self._strategy.on_trade_executed()
        self.state.clear_error()
        try:
            if self.execution_mode == "BLOCKED":
                reason = "Order blocked: use a demo account or type LIVE exactly for a real account."
                self._record_entry_decision(entry_record, False, reason)
                self.state.update_trade_outcome(trade_id, "CANCELLED", 0.0, reason)
                self.state.set_error(reason)
                self._journal.record_outcome(signal_id, "CANCELLED", 0.0, stake, None, self.execution_mode, martingale_step, note=reason)
                return

            if not self._client.connected and not await self._reconnect():
                reason = "Order not sent: could not restore Deriv connection."
                self._record_entry_decision(entry_record, False, reason)
                self.state.update_trade_outcome(trade_id, "CANCELLED", 0.0, reason)
                self._journal.record_outcome(signal_id, "CANCELLED", 0.0, stake, None, self.execution_mode, martingale_step, note=reason)
                return

            proposal = await self._client.get_proposal(
                symbol=self.symbol,
                contract_type=contract_type,
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
                raise DerivAPIError("Deriv returned an invalid digit proposal.", "INVALID_RESPONSE")

            metrics = (entry_record or {})
            estimates = [metrics.get("p_over6_fast"), metrics.get("p_over6_medium"), metrics.get("p_over6_slow")]
            estimates = [float(x) for x in estimates if x not in (None, "")]
            estimated_probability = min(estimates) if estimates else 0.0
            break_even = ask_price / payout
            quote_edge = estimated_probability - break_even
            if self.require_quote_edge and quote_edge < self.min_quote_edge:
                reason = (f"quote edge {quote_edge:.2%} below required {self.min_quote_edge:.2%} "
                          f"(estimated p={estimated_probability:.2%}, break-even={break_even:.2%})")
                self._record_entry_decision(entry_record, False, reason, ask_price, payout, break_even, quote_edge)
                self.state.update_trade_outcome(trade_id, "CANCELLED", 0.0, reason)
                self._journal.record_outcome(signal_id, "CANCELLED", 0.0, stake, None, self.execution_mode, martingale_step, note=reason)
                self.state.set_status(f"Over 6 quote rejected — {reason}")
                return

            self._record_entry_decision(entry_record, True, "", ask_price, payout, break_even, quote_edge)
            if self.state.stop_requested:
                raise DerivAPIError("Stop requested before buy; no order was submitted.", "STOP_REQUESTED")
            self.state.set_status(f"Buying Over 6 for {self.contract_duration} tick(s) at {ask_price:.2f}…")
            buy = await self._client.buy_contract(proposal_id=proposal_id, price=ask_price)
            contract_id = buy.get("contract_id")
            buy_price = float(buy.get("buy_price", ask_price))
            buy_payout = float(buy.get("payout", payout))
            if not isinstance(contract_id, int) or isinstance(contract_id, bool):
                raise DerivAPIError("No valid contract ID in digit buy response.", "NO_CONTRACT_ID")
            self._active_contract_id = contract_id
            trade_record.contract_id = contract_id
            self.state.update_trade_pacing()
            self._daily_trade_count += 1
            self.state.set_status(f"Over 6 contract {contract_id} active · awaiting {self.contract_duration} tick result")
            outcome, pnl = await self._monitor_contract(contract_id, buy_price, buy_payout)
            if outcome == "UNKNOWN":
                reason = f"Contract {contract_id} settlement was not confirmed; check the Deriv statement."
                self.state.update_trade_outcome(trade_id, "UNKNOWN", 0.0, reason)
                self.state.set_error(reason)
                self.state.request_stop()
                self._journal.record_outcome(signal_id, "UNKNOWN", 0.0, stake, contract_id, self.execution_mode, martingale_step, note=reason)
                return

            self.state.update_trade_outcome(trade_id, outcome, pnl)
            if outcome == "WON":
                self.state.on_trade_win()
                self.state.set_status(f"Over 6 WON · P&L {pnl:+.2f}")
            else:
                self.state.on_trade_loss(self.martingale_multiplier, self.max_martingale_steps)
                self.state.set_status(f"Over 6 LOST · next stake {self.state.get_martingale_state()['stake']:.2f}")
            self._journal.record_outcome(signal_id, outcome, pnl, stake, contract_id, self.execution_mode, martingale_step)
        except DerivAPIError as exc:
            reason = f"Digit order failed: {exc.message} ({exc.code})."
            if self._active_contract_id is not None:
                self.state.update_trade_outcome(trade_id, "UNKNOWN", 0.0, reason)
                self.state.request_stop()
                outcome = "UNKNOWN"
                self._journal.record_outcome(signal_id, outcome, 0.0, stake, self._active_contract_id, self.execution_mode, martingale_step, note=reason)
            else:
                self._record_entry_decision(entry_record, False, reason)
                self.state.update_trade_outcome(trade_id, "CANCELLED", 0.0, reason)
                self._journal.record_outcome(signal_id, "CANCELLED", 0.0, stake, None, self.execution_mode, martingale_step, note=reason)
            self.state.set_error(reason)
            self.state.set_status(reason)
        except Exception as exc:
            reason = f"Unexpected digit execution failure: {exc}"
            if self._active_contract_id is not None:
                self.state.update_trade_outcome(trade_id, "UNKNOWN", 0.0, reason)
                self.state.request_stop()
                self._journal.record_outcome(signal_id, "UNKNOWN", 0.0, stake, self._active_contract_id, self.execution_mode, martingale_step, note=reason)
            else:
                self._record_entry_decision(entry_record, False, reason)
                self.state.update_trade_outcome(trade_id, "CANCELLED", 0.0, reason)
                self._journal.record_outcome(signal_id, "CANCELLED", 0.0, stake, None, self.execution_mode, martingale_step, note=reason)
            self.state.set_error(reason)
        finally:
            self._active_contract_id = None
            self._trade_in_progress = False

    async def _monitor_contract(self, contract_id: int, buy_price: float, payout: float) -> tuple[str, float]:
        expected = self._contract_duration_seconds()
        deadline = time.time() + max(20.0, expected + 20.0)
        while time.time() < deadline:
            if not self._client.connected and not await self._reconnect(max_attempts=2):
                await asyncio.sleep(1.0)
                continue
            try:
                status = await self._client.get_open_contract_status(contract_id)
                name = str(status.get("status", "")).lower()
                expired = bool(status.get("is_expired", 0))
                sold = bool(status.get("is_sold", 0))
                if expired or sold or name in {"won", "lost", "sold"}:
                    raw_profit = status.get("profit")
                    if raw_profit is None:
                        sell_price = float(status.get("sell_price", 0) or 0)
                        profit = sell_price - buy_price
                    else:
                        profit = float(raw_profit)
                    return ("WON" if name == "won" or profit > 0 else "LOST", round(profit, 2))
                await asyncio.sleep(0.25)
            except (DerivAPIError, TypeError, ValueError) as exc:
                logger.warning("Digit contract poll failed for %s: %s", contract_id, exc)
                await asyncio.sleep(0.75)
        return "UNKNOWN", 0.0
