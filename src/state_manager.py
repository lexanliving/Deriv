"""
src/state_manager.py
Thread-safe shared state for the engine thread and the Streamlit UI.
"""
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from config import DEFAULT_INITIAL_STAKE, TICK_BUFFER_SIZE

TRADE_HISTORY_LIMIT = 500
FINAL_TRADE_STATUSES = {"WON", "LOST", "UNKNOWN", "CANCELLED"}


@dataclass
class TradeRecord:
    trade_id: str
    direction: str
    stake: float
    barrier: str
    entry_price: float
    timestamp: str
    status: str
    pnl: float = 0.0
    contract_id: Optional[int] = None
    martingale_step: int = 0
    execution_mode: str = "UNSPECIFIED"
    account_type: str = "UNKNOWN"
    error_message: str = ""


class StateManager:
    _STRATEGY_STATE_ATTRS = {
        "trend_direction": "_current_trend_direction",
        "trend_tick_count": "_trend_tick_count",
        "trend_kind": "_trend_kind",
        "trades_in_trend": "_trades_in_current_trend",
        "in_cooldown": "_in_cooldown",
        "pattern_stage": "_pattern_stage",
        "pattern_ticks": "_pattern_ticks",
        "mtf_bias": "_mtf_bias",
        "mtf_agreement": "_mtf_agreement",
        "mtf_tf_biases": "_mtf_tf_biases",
        "micro_bias": "_micro_bias",
        "last_entry_mode": "_last_entry_mode",
        "last_signal_score": "_last_signal_score",
        "last_signal_score_breakdown": "_last_signal_score_breakdown",
    }

    def __init__(self):
        self._lock = threading.Lock()
        self._is_running = False
        self._stop_requested = False
        self._current_price = 0.0
        self._recent_ticks: deque = deque(maxlen=TICK_BUFFER_SIZE)
        self._tick_timestamps: deque = deque(maxlen=TICK_BUFFER_SIZE)
        self._total_ticks_processed = 0
        self._current_trend_direction: Optional[str] = None
        self._trend_tick_count = 0
        self._trend_kind: Optional[str] = None
        self._trades_in_current_trend = 0
        self._in_cooldown = False
        self._pattern_stage = "IDLE"
        self._pattern_ticks: List[float] = []
        self._mtf_bias: Optional[str] = None
        self._mtf_agreement = 0
        self._mtf_tf_biases: Dict[str, str] = {}
        self._micro_bias: Optional[str] = None
        self._last_entry_mode: Optional[str] = None
        self._last_signal_score = 0
        self._last_signal_score_breakdown: Dict[str, int] = {}
        self._current_martingale_step = 0
        self._current_stake = DEFAULT_INITIAL_STAKE
        self._initial_stake = DEFAULT_INITIAL_STAKE
        self._last_trade_time = 0.0
        self._session_pnl = 0.0
        self._consecutive_losses = 0
        self._trade_history: deque = deque(maxlen=TRADE_HISTORY_LIMIT)
        self._trades_by_id: Dict[str, TradeRecord] = {}
        self._total_pnl = 0.0
        self._wins = 0
        self._losses = 0
        self._total_won = 0.0
        self._total_lost = 0.0
        self._execution_context: Dict[str, str] = {
            "account_id": "",
            "account_type": "UNKNOWN",
            "currency": "USD",
            "execution_mode": "UNCONFIGURED",
        }
        self._status_message = "Bot is stopped."
        self._error_message = ""

    # --- bot control ---
    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._is_running

    def set_running(self, value: bool):
        with self._lock:
            self._is_running = value
            if value:
                self._stop_requested = False

    @property
    def stop_requested(self) -> bool:
        with self._lock:
            return self._stop_requested

    def request_stop(self):
        with self._lock:
            self._stop_requested = True

    # --- market data ---
    def update_tick(self, price: float, timestamp: float):
        with self._lock:
            self._current_price = price
            self._recent_ticks.append(price)
            self._tick_timestamps.append(timestamp)
            self._total_ticks_processed += 1

    @property
    def current_price(self) -> float:
        with self._lock:
            return self._current_price

    def get_recent_ticks(self) -> List[float]:
        with self._lock:
            return list(self._recent_ticks)

    def get_tick_heartbeat(self) -> Dict[str, Any]:
        with self._lock:
            last_tick_time = self._tick_timestamps[-1] if self._tick_timestamps else None
            return {
                "total_ticks_processed": self._total_ticks_processed,
                "last_tick_time": last_tick_time,
            }

    # --- strategy state ---
    def get_strategy_state(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "trend_direction": self._current_trend_direction,
                "trend_tick_count": self._trend_tick_count,
                "trend_kind": self._trend_kind,
                "trades_in_trend": self._trades_in_current_trend,
                "in_cooldown": self._in_cooldown,
                "pattern_stage": self._pattern_stage,
                "pattern_ticks": list(self._pattern_ticks),
                "mtf_bias": self._mtf_bias,
                "mtf_agreement": self._mtf_agreement,
                "mtf_tf_biases": dict(self._mtf_tf_biases),
                "micro_bias": self._micro_bias,
                "last_entry_mode": self._last_entry_mode,
                "last_signal_score": self._last_signal_score,
                "last_signal_score_breakdown": dict(self._last_signal_score_breakdown),
            }

    def update_strategy_state(self, **kwargs):
        with self._lock:
            self._apply_strategy_state(kwargs)

    def _apply_strategy_state(self, kwargs: Dict[str, Any]) -> None:
        for key, value in kwargs.items():
            attr = self._STRATEGY_STATE_ATTRS.get(key)
            if attr is not None:
                setattr(self, attr, value)

    def update_tick_and_strategy_state(self, price, timestamp, **strategy_kwargs):
        with self._lock:
            self._current_price = price
            self._recent_ticks.append(price)
            self._tick_timestamps.append(timestamp)
            self._total_ticks_processed += 1
            self._apply_strategy_state(strategy_kwargs)

    # --- martingale ---
    def get_martingale_state(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "step": self._current_martingale_step,
                "stake": self._current_stake,
                "initial_stake": self._initial_stake,
            }

    def set_initial_stake(self, stake: float):
        with self._lock:
            self._initial_stake = stake
            self._current_stake = stake

    def on_trade_win(self):
        with self._lock:
            self._current_martingale_step = 0
            self._current_stake = self._initial_stake

    def on_trade_loss(self, multiplier: float, max_steps: int):
        with self._lock:
            if self._current_martingale_step < max_steps:
                self._current_martingale_step += 1
                self._current_stake = round(self._current_stake * multiplier, 2)
            else:
                self._current_martingale_step = 0
                self._current_stake = self._initial_stake

    # --- pacing ---
    def _cooldown_remaining_unsafe(self) -> float:
        if self._last_trade_time == 0.0:
            return 0.0
        elapsed = time.time() - self._last_trade_time
        if self._consecutive_losses >= 2:
            required = 180.0
        elif self._consecutive_losses >= 1:
            required = 90.0
        else:
            required = 30.0
        return max(0.0, required - elapsed)

    def get_cooldown_remaining(self) -> float:
        with self._lock:
            return self._cooldown_remaining_unsafe()

    def update_trade_pacing(self):
        with self._lock:
            self._last_trade_time = time.time()

    # --- trade history ---
    def add_trade(self, trade: TradeRecord):
        with self._lock:
            if len(self._trade_history) == self._trade_history.maxlen:
                evicted = self._trade_history.popleft()
                self._trades_by_id.pop(evicted.trade_id, None)
            self._trade_history.append(trade)
            self._trades_by_id[trade.trade_id] = trade

    def update_trade_outcome(self, trade_id, status, pnl, error_message=""):
        with self._lock:
            trade = self._trades_by_id.get(trade_id)
            if trade is None:
                return
            if trade.status in FINAL_TRADE_STATUSES:
                return
            trade.status = status
            trade.pnl = pnl
            if error_message:
                trade.error_message = error_message
            self._total_pnl += pnl
            self._session_pnl += pnl
            if status == "WON":
                self._wins += 1
                self._consecutive_losses = 0
                if pnl > 0:
                    self._total_won += pnl
            elif status == "LOST":
                self._losses += 1
                self._consecutive_losses += 1
                self._total_lost += abs(pnl)

    def get_trade_history(self) -> List[TradeRecord]:
        with self._lock:
            return list(reversed(self._trade_history))

    def get_performance_stats(self) -> Dict[str, Any]:
        with self._lock:
            total = self._wins + self._losses
            win_rate = (self._wins / total * 100) if total > 0 else 0.0
            avg_win = (self._total_won / self._wins) if self._wins > 0 else 0.0
            avg_loss = (self._total_lost / self._losses) if self._losses > 0 else 0.0
            expectancy = (win_rate / 100.0 * avg_win) - ((1 - win_rate / 100.0) * avg_loss)
            return {
                "total_trades": total,
                "wins": self._wins,
                "losses": self._losses,
                "win_rate": round(win_rate, 1),
                "avg_win": round(avg_win, 2),
                "avg_loss": round(avg_loss, 2),
                "expectancy": round(expectancy, 2),
                "total_pnl": round(self._total_pnl, 2),
                "session_pnl": round(self._session_pnl, 2),
                "current_stake": self._current_stake,
                "initial_stake": self._initial_stake,
                "martingale_step": self._current_martingale_step,
                "consecutive_losses": self._consecutive_losses,
                "cooldown_remaining": self._cooldown_remaining_unsafe(),
            }

    # --- execution context ---
    def set_execution_context(self, account_id, account_type, currency, execution_mode):
        with self._lock:
            self._execution_context = {
                "account_id": str(account_id or ""),
                "account_type": str(account_type or "UNKNOWN").upper(),
                "currency": str(currency or "USD").upper(),
                "execution_mode": str(execution_mode or "UNCONFIGURED").upper(),
            }

    def get_execution_context(self) -> Dict[str, str]:
        with self._lock:
            return dict(self._execution_context)

    # --- status ---
    @property
    def status_message(self) -> str:
        with self._lock:
            return self._status_message

    def set_status(self, message: str):
        with self._lock:
            self._status_message = message

    @property
    def error_message(self) -> str:
        with self._lock:
            return self._error_message

    def set_error(self, message: str):
        with self._lock:
            self._error_message = message

    def clear_error(self):
        with self._lock:
            self._error_message = ""

    def reset_for_new_session(self, initial_stake: float):
        with self._lock:
            self._is_running = False
            self._stop_requested = False
            self._current_price = 0.0
            self._recent_ticks.clear()
            self._tick_timestamps.clear()
            self._total_ticks_processed = 0
            self._current_trend_direction = None
            self._trend_tick_count = 0
            self._trend_kind = None
            self._trades_in_current_trend = 0
            self._in_cooldown = False
            self._pattern_stage = "IDLE"
            self._pattern_ticks = []
            self._mtf_bias = None
            self._mtf_agreement = 0
            self._mtf_tf_biases = {}
            self._micro_bias = None
            self._last_entry_mode = None
            self._last_signal_score = 0
            self._last_signal_score_breakdown = {}
            self._current_martingale_step = 0
            self._initial_stake = initial_stake
            self._current_stake = initial_stake
            self._trade_history = deque(maxlen=TRADE_HISTORY_LIMIT)
            self._trades_by_id = {}
            self._total_pnl = 0.0
            self._wins = 0
            self._losses = 0
            self._total_won = 0.0
            self._total_lost = 0.0
            self._session_pnl = 0.0
            self._last_trade_time = 0.0
            self._consecutive_losses = 0
            self._execution_context = {
                "account_id": "",
                "account_type": "UNKNOWN",
                "currency": "USD",
                "execution_mode": "UNCONFIGURED",
            }
            self._status_message = "Bot is stopped."
            self._error_message = ""