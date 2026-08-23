"""MomentumMaster Digit — Streamlit terminal for 1–2 tick Over 6 contracts."""
from __future__ import annotations

import asyncio
import html
import os
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List

import pandas as pd
import streamlit as st

try:
    from streamlit.runtime.scriptrunner import add_script_run_ctx
except ImportError:
    try:
        from streamlit.runtime.scriptrunner_utils import add_script_run_ctx
    except ImportError:
        def add_script_run_ctx(thread, ctx=None):
            return thread

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import (
    AVAILABLE_MARKETS,
    DEFAULT_INITIAL_STAKE,
    DEFAULT_MARKET_DISPLAY,
    DIGIT_DEFAULT_TICK_DURATION,
    DIGIT_DEFAULT_RECOVERY_ENABLED,
    DIGIT_DEFAULT_RECOVERY_MULTIPLIER,
    DIGIT_DEFAULT_PROFIT_TARGET,
    DIGIT_MAX_RECOVERY_STEPS,
    DIGIT_LOWER_CONFIRM_MAX,
    DIGIT_MIN_OVER6_SHARE,
    DIGIT_REVIEW_INTERVAL_SECONDS,
    DIGIT_TICK_DURATION_OPTIONS,
    DIGIT_WINDOWS,
    DERIV_APP_ID,
    DERIV_API_TOKEN,
)
from src.api_client import DerivAPIClient, DerivAPIError
from src.journal import get_journal
from src.persistence import export_archive_csv_bytes, export_merged_json_bytes, import_journal
from src.state_manager import StateManager
from src.trading_engine import TradingEngine, normalize_account_type, resolve_execution_mode

st.set_page_config(
    page_title="MomentumMaster Digit",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(
    """
    <style>
    html, body, .stApp {
        background: #060912;
        color: #c7d2e0;
        font-family: Inter, system-ui, sans-serif;
    }
    [data-testid="stSidebar"] {
        background: #0a0f1c;
        border-right: 1px solid #1b2740;
    }
    [data-testid="stMainBlockContainer"] {
        max-width: 1480px;
        padding-top: 1.2rem;
    }
    .mm-head {
        display: flex;
        justify-content: space-between;
        align-items: end;
        border-bottom: 2px solid #10b981;
        padding: 8px 0 14px;
        margin-bottom: 14px;
    }
    .mm-logo {
        font-size: 1.45rem;
        font-weight: 800;
        letter-spacing: .12em;
        color: #eef3fb;
        text-transform: uppercase;
    }
    .mm-logo b {
        color: #10b981;
    }
    .mm-sub {
        color: #8294b0;
        font-size: .78rem;
        margin-top: 5px;
    }
    .mm-card {
        background: linear-gradient(150deg, #0c1426, #0e1830);
        border: 1px solid #1d2c49;
        border-radius: 11px;
        padding: 15px 17px;
        height: 100%;
    }
    .mm-label {
        font-size: .64rem;
        font-weight: 700;
        letter-spacing: .15em;
        text-transform: uppercase;
        color: #6b7c97;
    }
    .mm-value {
        font-family: monospace;
        font-size: 1.65rem;
        font-weight: 800;
        color: #eef3fb;
        margin-top: 7px;
    }
    .mm-small {
        font-size: .76rem;
        color: #8294b0;
        margin-top: 6px;
        line-height: 1.45;
    }
    .mm-section {
        font-size: .72rem;
        font-weight: 800;
        letter-spacing: .16em;
        text-transform: uppercase;
        color: #6b7c97;
        margin: 20px 0 9px;
        border-bottom: 1px solid #1b2740;
        padding-bottom: 7px;
    }
    .good { color: #34d399; }
    .bad { color: #fb7185; }
    .warn { color: #fbbf24; }
    .muted { color: #8294b0; }
    </style>
    """,
    unsafe_allow_html=True,
)


def _run_engine_in_thread(engine: TradingEngine, loop: asyncio.AbstractEventLoop) -> None:
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(engine.run())
    finally:
        try:
            pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        except Exception:
            pass
        try:
            loop.close()
        except Exception:
            pass


def _today_filled_trade_count(state_obj: StateManager) -> int:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return sum(
        1
        for trade in state_obj.get_trade_history()
        if getattr(trade, "contract_id", None) is not None and str(trade.timestamp).startswith(today)
    )


class EngineSupervisor:
    """Keeps the engine alive independently of Streamlit fragment scheduling."""

    RESTART_COOLDOWN_SECONDS = 3.0
    HEALTHY_WINDOW_SECONDS = 180.0
    MAX_BACKOFF_SECONDS = 30.0

    def __init__(self, state: StateManager) -> None:
        self.state = state
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._engine_thread: threading.Thread | None = None
        self._engine: TradingEngine | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._config: Dict[str, Any] | None = None
        self._should_run = threading.Event()
        self._next_reset = True
        self._restart_count = 0
        self._last_restart = 0.0

    @property
    def active(self) -> bool:
        return self._should_run.is_set()

    def engine_alive(self) -> bool:
        return self._engine_thread is not None and self._engine_thread.is_alive()

    def ensure_alive(self) -> None:
        if self._should_run.is_set() or self.engine_alive():
            self._ensure_supervisor_thread()

    def start(self, config: Dict[str, Any], reset_state: bool = True) -> None:
        with self._lock:
            self._config = dict(config)
            self._next_reset = bool(reset_state)
            self._restart_count = 0
            self._last_restart = 0.0
            self.state.clear_error()
            self.state.clear_stop_request()
            self._should_run.set()
            self._ensure_supervisor_thread()

    def stop(self) -> None:
        with self._lock:
            self._should_run.clear()
        self.state.request_stop()
        self.state.set_status("Stopping…")

    def acknowledge_stop(self) -> None:
        with self._lock:
            self._should_run.clear()
        self.state.set_running(False)
        self.state.set_status("Stopped.")

    def _ensure_supervisor_thread(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(
                target=self._supervisor_loop,
                daemon=True,
                name="DigitEngineSupervisor",
            )
            self._thread.start()

    def _supervisor_loop(self) -> None:
        while self._should_run.is_set() or self.engine_alive():
            try:
                if not self._should_run.is_set():
                    # Waiting for the engine to finish an open contract / shutdown.
                    time.sleep(0.5)
                    continue

                if not self._config:
                    self._should_run.clear()
                    self.state.set_running(False)
                    self.state.set_error("Engine configuration was lost.")
                    self.state.set_status("Stopped.")
                    continue

                if self.state.stop_requested and not self.engine_alive():
                    self._should_run.clear()
                    self.state.set_running(False)
                    self.state.set_status("Stopped.")
                    continue

                if not self.engine_alive():
                    now = time.monotonic()

                    if self._restart_count > 0 and now - self._last_restart > self.HEALTHY_WINDOW_SECONDS:
                        self._restart_count = 0

                    backoff = (
                        0.0
                        if self._restart_count == 0
                        else min(
                            self.MAX_BACKOFF_SECONDS,
                            self.RESTART_COOLDOWN_SECONDS * (2 ** min(self._restart_count, 4)),
                        )
                    )

                    if now - self._last_restart < backoff:
                        time.sleep(0.5)
                        continue

                    self._restart_count += 1
                    self._last_restart = now

                    reset = self._next_reset
                    self._next_reset = False

                    self.state.set_status(f"Engine starting ({self._restart_count})…")
                    if not self._launch_engine(reset_state=reset):
                        time.sleep(2.0)

                time.sleep(0.5)
            except Exception:
                time.sleep(1.0)

        self.state.set_running(False)

    def _launch_engine(self, reset_state: bool) -> bool:
        try:
            config = self._config
            if not config:
                return False

            if reset_state:
                self.state.reset_for_new_session(float(config["initial_stake"]))
            else:
                self.state.clear_error()

            history = self.state.get_trade_history()
            if history and history[0].status == "OPEN":
                self.state.update_trade_outcome(
                    history[0].trade_id,
                    "UNKNOWN",
                    0.0,
                    "The engine restarted while a contract was open — check the Deriv statement.",
                )

            engine = TradingEngine(
                api_token=config["api_token"],
                app_id=config["app_id"],
                account_id=config["account_id"],
                account_currency=config["account_currency"],
                state=self.state,
                initial_stake=config["initial_stake"],
                max_martingale_steps=config["max_steps"],
                symbol=config["symbol"],
                symbol_display=config["symbol_display"],
                contract_duration=config["duration_ticks"],
                contract_duration_unit="t",
                account_type=config["account_type"],
                real_execution_confirmed=config["real_execution_confirmed"],
                martingale_multiplier=config["martingale_multiplier"],
                quote_precision=config.get("quote_precision", 2),
                min_over6_share=config["min_over6_share"],
                lower_tick_max=config["lower_tick_max"],
                review_interval_seconds=config["review_interval_seconds"],
                required_lower_confirmations=config.get("required_lower_confirmations", 1),
                digit_windows=config["digit_windows"],
                take_profit_target=config.get("take_profit_target", 0.0),
            )

            if not reset_state:
                engine._daily_trade_count = _today_filled_trade_count(self.state)
                engine._daily_date = datetime.now(timezone.utc).date()

            self._engine = engine
            self.state.set_running(True)

            loop = asyncio.new_event_loop()
            self._loop = loop

            thread = threading.Thread(
                target=_run_engine_in_thread,
                args=(engine, loop),
                daemon=True,
                name="DigitTradingEngineThread",
            )
            try:
                add_script_run_ctx(thread)
            except Exception:
                pass

            thread.start()
            self._engine_thread = thread
            return True
        except Exception as exc:
            self.state.set_running(False)
            self.state.set_error(f"The engine could not start: {exc}")
            self.state.set_status("Could not start.")
            return False


@st.cache_resource
def get_global_state() -> StateManager:
    return StateManager()


@st.cache_resource
def get_supervisor() -> EngineSupervisor:
    return EngineSupervisor(get_global_state())


state = get_global_state()
supervisor = get_supervisor()
engine_busy = state.is_running or supervisor.engine_alive()
configured_pat = DERIV_API_TOKEN.strip()


@st.cache_data(ttl=60, show_spinner=False)
def _load_accounts(app_id: str, token: str):
    return asyncio.run(DerivAPIClient.get_accounts(token, app_id))


def _is_derived_index(item: Dict[str, Any]) -> bool:
    symbol = str(item.get("symbol", "")).strip().upper()
    metadata = " ".join(
        str(item.get(key, ""))
        for key in ("market", "market_type", "submarket", "symbol_type", "display_name", "name")
    ).lower()

    financial_markers = ("forex", "financial", "commodit", "stock", "crypto", "exchange")
    index_markers = (
        "synthetic",
        "derived",
        "volatility",
        "jump",
        "boom",
        "crash",
        "step index",
        "range break",
    )

    if any(marker in metadata for marker in financial_markers):
        return False
    if any(marker in metadata for marker in index_markers):
        return True

    return symbol.startswith(
        ("1HZ", "R", "JD", "BOOM", "CRASH", "STPRNG", "RDBULL", "RDBEAR")
    )


@st.cache_data(ttl=180, show_spinner=False)
def _load_live_digit_markets(app_id: str, token: str, account_id: str):
    """Load all symbols the selected account/API currently exposes for digits."""

    async def fetch():
        client = DerivAPIClient(token, app_id, account_id)
        if not await client.connect():
            raise DerivAPIError(client.last_error or "Could not connect while loading markets.", "MARKET_LOAD_FAILED")
        try:
            symbols = await client.get_active_symbols(full=True)
            result = {}
            for item in symbols:
                symbol = str(item.get("symbol", "")).strip()
                suspended = str(item.get("is_trading_suspended", "")).strip().lower() in {"1", "true", "yes", "y"}
                if not symbol or suspended or not _is_derived_index(item):
                    continue

                label = str(item.get("display_name") or item.get("name") or symbol).strip()
                if label in result and result[label] != symbol:
                    label = f"{label} · {symbol}"
                result[label] = symbol

            return dict(sorted(result.items(), key=lambda pair: pair[0].lower()))
        finally:
            await client.disconnect()

    return asyncio.run(fetch())


def _fallback_markets() -> Dict[str, str]:
    return dict(sorted(AVAILABLE_MARKETS.items(), key=lambda pair: pair[0].lower()))


def start_bot(config: Dict[str, Any]) -> None:
    if supervisor.engine_alive():
        return
    supervisor.start(config, reset_state=True)


def stop_bot() -> None:
    supervisor.stop()


# ---------------------------------------------------------------------------
# Sidebar controls
# ---------------------------------------------------------------------------

with st.sidebar:
    st.markdown("### Digit terminal")
    st.caption("Rolling last-digit reviews · Over 6 · 1–2 ticks")

    accounts: List[Dict[str, Any]] = []
    account_error = ""

    if not DERIV_APP_ID or not configured_pat:
        account_error = "Add DERIV_APP_ID and DERIV_API_TOKEN to Streamlit Secrets or .env."
    else:
        try:
            accounts = _load_accounts(DERIV_APP_ID, configured_pat)
        except Exception as exc:
            account_error = str(exc)

    account_id = ""
    account_currency = "USD"
    account_type = "UNKNOWN"
    selected_account = None
    real_execution_confirmed = False

    if account_error:
        st.error(account_error)
    elif not accounts:
        st.warning("No active accounts were found.")
    else:
        account_map = {
            str(item.get("account_id")): item
            for item in accounts
            if item.get("account_id")
        }
        options = list(account_map)
        picked = st.selectbox(
            "Account",
            options=options,
            disabled=engine_busy,
            format_func=lambda value: (
                f"{normalize_account_type(account_map[value].get('account_type', 'UNKNOWN'))} · {value} · "
                f"{account_map[value].get('currency', 'USD')} {float(account_map[value].get('balance', 0) or 0):,.2f}"
            ),
        )
        account_id = picked
        selected_account = account_map[picked]
        account_type = normalize_account_type(selected_account.get("account_type", "UNKNOWN"))
        account_currency = str(selected_account.get("currency", "USD")).upper()
        st.success(
            f"{account_type} account · {account_currency} "
            f"{float(selected_account.get('balance', 0) or 0):,.2f}"
        )

    st.divider()

    market_catalog = _fallback_markets()
    if selected_account and DERIV_APP_ID and configured_pat:
        try:
            live_catalog = _load_live_digit_markets(DERIV_APP_ID, configured_pat, account_id)
            if live_catalog:
                market_catalog = live_catalog
                st.caption(
                    f"{len(market_catalog)} active markets loaded from Deriv; digit offerings are checked when the engine starts"
                )
        except Exception as exc:
            st.caption(f"Live catalogue unavailable; using local list ({str(exc)[:80]})")

    labels = list(market_catalog)
    if not labels:
        st.error("No markets are available.")
        st.stop()

    default_label = DEFAULT_MARKET_DISPLAY if DEFAULT_MARKET_DISPLAY in market_catalog else labels[0]
    market_display = st.selectbox(
        "Market",
        options=labels,
        index=labels.index(default_label),
        disabled=engine_busy,
    )
    selected_symbol = market_catalog[market_display]

    st.caption(
        "Indices only: Volatility, Jump, Boom, Crash, Step Index, and Range Break. "
        "Forex, commodities, stocks, and crypto are excluded."
    )

    st.divider()

    duration_ticks = st.select_slider(
        "Contract duration",
        options=list(DIGIT_TICK_DURATION_OPTIONS),
        value=DIGIT_DEFAULT_TICK_DURATION,
        disabled=engine_busy,
        format_func=lambda value: f"{value} tick" if value == 1 else f"{value} ticks",
    )

    lower_confirmations = st.select_slider(
        "Lower-tick confirmations before entry",
        options=[1, 2, 3],
        value=1,
        disabled=engine_busy,
        format_func=lambda value: f"{value} lower tick" if value == 1 else f"{value} consecutive lower ticks",
    )

    st.caption(
        "This is an entry-timing trigger only: the concentration rule still comes from the 7–9 review. "
        "The final required lower digit itself triggers entry; a higher digit resets the sequence. "
        "Default: 1 lower 0–6 tick, then immediate entry on that same tick."
    )

    st.divider()

    st.markdown("#### Strategy rule")
    min_over6_share_pct = st.slider(
        "Minimum recent 7–9 share (%)",
        30,
        60,
        int(round(DIGIT_MIN_OVER6_SHARE * 100)),
        1,
        disabled=engine_busy,
    )
    min_over6_share = min_over6_share_pct / 100.0

    st.caption(
        f"{min_over6_share_pct}% means at least that share of recent ticks ends in 7, 8, or 9. "
        "Because the groups contain 3 versus 6 digits, the rule compares average frequency per digit: "
        "(7–9 share ÷ 3) > (1–6 share ÷ 6). "
        f"Fast/medium use {min_over6_share_pct}%; slow support uses at least {max(30, min_over6_share_pct - 1)}%. "
        f"Windows: {DIGIT_WINDOWS['fast']} / {DIGIT_WINDOWS['medium']} / {DIGIT_WINDOWS['slow']} ticks. "
        f"Lower confirmation is separate and currently set to {lower_confirmations}."
    )

    st.divider()

    st.markdown("#### Account safety")
    if selected_account and account_type == "REAL":
        live_text = st.text_input("Type LIVE to enable real orders", max_chars=4, disabled=engine_busy)
        real_execution_confirmed = live_text == "LIVE"
        st.warning(
            "Real-money orders are ON."
            if real_execution_confirmed
            else "Real account: orders remain blocked until LIVE is typed exactly."
        )
    elif selected_account and account_type == "DEMO":
        st.success("Demo account: virtual funds only.")
    elif selected_account:
        st.error("Unrecognized account type: orders are blocked.")

    st.divider()

    initial_stake = st.number_input(
        "Starting stake",
        min_value=0.35,
        max_value=10000.0,
        value=float(DEFAULT_INITIAL_STAKE),
        step=0.5,
        format="%.2f",
        disabled=engine_busy,
    )

    use_martingale = st.checkbox(
        "Enable recovery multiplier",
        value=bool(DIGIT_DEFAULT_RECOVERY_ENABLED),
        disabled=engine_busy,
    )

    martingale_multiplier = st.number_input(
        "Recovery multiplier",
        min_value=1.01,
        max_value=4.0,
        value=float(DIGIT_DEFAULT_RECOVERY_MULTIPLIER),
        step=0.01,
        format="%.2f",
        disabled=engine_busy,
    )

    max_steps = st.slider(
        "Maximum recovery steps",
        0,
        int(DIGIT_MAX_RECOVERY_STEPS),
        int(DIGIT_MAX_RECOVERY_STEPS if use_martingale else 0),
        disabled=engine_busy,
    )
    if not use_martingale:
        max_steps = 0

    take_profit_target = st.number_input(
        "Session take-profit target",
        min_value=0.0,
        max_value=100000.0,
        value=float(DIGIT_DEFAULT_PROFIT_TARGET),
        step=1.0,
        format="%.2f",
        disabled=engine_busy,
        help="0 disables the target. When session P&L reaches this positive amount, the bot stops before a new entry.",
    )

    st.caption(
        "Recovery: 1.10 means the next stake is 110% of the previous stake after a loss. "
        "Take-profit stops new entries only after positive session P&L reaches your target; there is no session loss-stop."
    )

    st.divider()

    c1, c2 = st.columns(2)
    with c1:
        start_pressed = st.button("Start", type="primary", use_container_width=True, disabled=engine_busy)
    with c2:
        stop_pressed = st.button("Stop", use_container_width=True, disabled=not engine_busy)

    if start_pressed:
        if not selected_account:
            st.error("Choose a Deriv account first.")
        else:
            start_bot(
                {
                    "api_token": configured_pat,
                    "app_id": DERIV_APP_ID,
                    "account_id": account_id,
                    "account_currency": account_currency,
                    "account_type": account_type,
                    "real_execution_confirmed": real_execution_confirmed,
                    "initial_stake": float(initial_stake),
                    "max_steps": int(max_steps),
                    "martingale_multiplier": float(martingale_multiplier),
                    "take_profit_target": float(take_profit_target),
                    "symbol": selected_symbol,
                    "symbol_display": market_display,
                    "duration_ticks": int(duration_ticks),
                    "quote_precision": 2,
                    "min_over6_share": float(min_over6_share),
                    "lower_tick_max": DIGIT_LOWER_CONFIRM_MAX,
                    "required_lower_confirmations": int(lower_confirmations),
                    "review_interval_seconds": DIGIT_REVIEW_INTERVAL_SECONDS,
                    "digit_windows": dict(DIGIT_WINDOWS),
                }
            )
            st.rerun()

    if stop_pressed:
        stop_bot()
        st.rerun()


# ---------------------------------------------------------------------------
# Main terminal
# ---------------------------------------------------------------------------

ctx = state.get_execution_context()
mode = ctx.get("execution_mode", "UNCONFIGURED")
mode_color = "#34d399" if mode == "DEMO" else "#fbbf24" if mode == "REAL" else "#8294b0"

st.markdown(
    f"""
    <div class="mm-head">
        <div>
            <div class="mm-logo">Momentum<b>·</b>Master DIGIT</div>
            <div class="mm-sub">Manual-market selection · rolling 7–9 frequency · Over 6 · 1–2 tick settlement</div>
        </div>
        <div style="text-align:right">
            <b style="color:{mode_color}">{html.escape(str(mode))}</b><br>
            <span class="muted">{html.escape(str(ctx.get("account_id", "")[:12]))}</span>
        </div>
    </div>
    """,
    unsafe_allow_html=True,
)


@st.fragment(run_every=1.0)
def watchdog_fragment():
    sup = get_supervisor()
    if sup.active or sup.engine_alive():
        sup.ensure_alive()

    if not sup.active and not sup.engine_alive() and state.is_running:
        state.set_running(False)


@st.fragment(run_every=1.0)
def status_fragment():
    message = state.error_message or state.status_message
    color = "#fb7185" if state.error_message else "#34d399" if state.is_running else "#8294b0"
    st.markdown(
        f'<div class="mm-card" style="border-left:4px solid {color};"><b>{html.escape(message)}</b></div>',
        unsafe_allow_html=True,
    )


@st.fragment(run_every=1.0)
def metrics_fragment():
    stats = state.get_performance_stats()
    mart = state.get_martingale_state()
    s = state.get_strategy_state()

    wins = stats["wins"]
    losses = stats["losses"]

    cards = st.columns(5)

    medium = s.get("digit_windows", {}).get("medium", {}) or {}
    medium_count = int(medium.get("count", 0) or 0)
    medium_high = int(medium.get("over6_count", 0) or 0)
    condition_valid = bool(s.get("digit_condition_valid"))
    digit_state = "ARMED" if s.get("digit_armed") else ("QUALIFYING" if condition_valid else "DISARMED")

    values = [
        ("Last digit", str(s.get("last_digit") if s.get("last_digit") is not None else "—"), "latest quote digit"),
        (
            "7–9 share",
            f"{medium_high}/{medium_count} = {float(medium.get('p_over6', 0) or 0) * 100:.1f}%",
            "medium window",
        ),
        ("Digit state", digit_state, "lower tick confirmation"),
        ("Session P&L", f"{stats['total_pnl']:+.2f}", f"{wins}W · {losses}L"),
        ("Next stake", f"{mart['stake']:.2f}", f"recovery step {mart['step']}"),
    ]

    for col, (label, value, sub) in zip(cards, values):
        with col:
            st.markdown(
                f"""
                <div class="mm-card">
                    <div class="mm-label">{label}</div>
                    <div class="mm-value">{html.escape(str(value))}</div>
                    <div class="mm-small">{html.escape(str(sub))}</div>
                </div>
                """,
                unsafe_allow_html=True,
            )


@st.fragment(run_every=1.0)
def digit_panel_fragment():
    s = state.get_strategy_state()
    windows = s.get("digit_windows") or {}
    counts = s.get("digit_counts") or {}

    left, right = st.columns([1, 1])

    with left:
        st.markdown('<div class="mm-section">Manual-style digit review</div>', unsafe_allow_html=True)

        rows = []
        for name in ("fast", "medium", "slow"):
            w = windows.get(name) or {}
            count = int(w.get("count", 0) or 0)
            high_count = int(w.get("over6_count", 0) or 0)
            comparison_count = int(w.get("comparison_count_1to6", 0) or 0)

            rows.append(
                {
                    "Window": f"{name} · {count} ticks",
                    "7–9 total": f"{high_count}/{count} = {float(w.get('p_over6', 0) or 0) * 100:.1f}%",
                    "7–9 avg/digit": f"{float(w.get('p_over6_avg', 0) or 0) * 100:.1f}% each",
                    "1–6 total": f"{comparison_count}/{count} = {float(w.get('p_1to6', 0) or 0) * 100:.1f}%",
                    "1–6 avg/digit": f"{float(w.get('p_1to6_avg', 0) or 0) * 100:.1f}% each",
                    "Per-digit gap": f"{float(w.get('per_digit_dominance', 0) or 0) * 100:+.1f} pp",
                }
            )

        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

        st.caption(
            "Example: 60.0% in a 20-tick window means 12 of 20 ticks ended in 7, 8, or 9. "
            "The strategy compares average frequency per digit as well: 60% ÷ 3 = 20% for each 7–9 digit group, "
            "versus the 1–6 average."
        )

        armed = bool(s.get("digit_armed"))
        confirmed = bool(s.get("digit_lower_confirmed"))
        required_lower = int(s.get("digit_required_lower_confirmations", 1) or 1)
        lower_count = int(s.get("digit_lower_confirmation_count", 0) or 0)

        if armed and confirmed:
            if s.get("pattern_stage") == "SIGNAL":
                st.success(f"Entry triggered on the final lower digit ({lower_count}/{required_lower}); awaiting execution.")
            else:
                st.success(f"Lower sequence confirmed ({lower_count}/{required_lower}); entry trigger recorded.")
        elif armed:
            st.warning(
                f"7–9 condition is armed. Waiting for lower digits from 0 to 6: {lower_count}/{required_lower} consecutive."
            )
        else:
            st.info(str(s.get("digit_last_rejection") or "Collecting digit history…"))

    with right:
        st.markdown('<div class="mm-section">Digit counts · current buffer</div>', unsafe_allow_html=True)

        data = [{"Digit": d, "Count": int(counts.get(str(d), 0))} for d in range(10)]
        st.dataframe(pd.DataFrame(data), use_container_width=True, height=250, hide_index=True)

        st.caption(
            "Numeric counts only; no extra chart is rendered. A trade is sent only after the strategy condition, "
            "lower-tick confirmation, account safeguards, and a valid Deriv proposal all pass. "
            "Proposal payout is recorded but is not used as a guessed edge signal."
        )


@st.fragment(run_every=2.0)
def ledger_fragment():
    st.markdown('<div class="mm-section">Trades</div>', unsafe_allow_html=True)

    history = state.get_trade_history()
    if not history:
        st.info("No trades yet. The bot will stand aside until the rolling digit rule qualifies.")
        return

    records = []
    for trade in history[:50]:
        records.append(
            {
                "Time": trade.timestamp,
                "Direction": trade.direction,
                "Barrier": trade.barrier,
                "Stake": trade.stake,
                "Result": trade.status,
                "P&L": trade.pnl,
                "Step": trade.martingale_step,
                "Mode": trade.execution_mode,
            }
        )

    st.dataframe(pd.DataFrame(records), use_container_width=True, height=300, hide_index=True)


@st.fragment(run_every=10.0)
def journal_fragment():
    journal = get_journal()

    st.markdown(
        '<div class="mm-section">Decision log · every minute review and entry decision</div>',
        unsafe_allow_html=True,
    )

    csv_bytes = journal.to_csv_bytes()
    if csv_bytes:
        st.download_button(
            "Download decision log",
            data=csv_bytes,
            file_name="momentummaster_digit_journal.csv",
            mime="text/csv",
            use_container_width=True,
        )

    rows = journal.read_rows()
    if not rows:
        st.caption("No reviews recorded yet.")
        return

    df = pd.DataFrame(rows).tail(50).iloc[::-1]
    preferred = [
        "timestamp_utc",
        "symbol",
        "direction",
        "taken",
        "rejection_reason",
        "score",
        "p_over6_fast",
        "p_over6_medium",
        "p_over6_slow",
        "p_over6_avg_fast",
        "p_over6_avg_medium",
        "p_over6_avg_slow",
        "p_1to6_avg_fast",
        "p_1to6_avg_medium",
        "p_1to6_avg_slow",
        "review_timestamp_utc",
        "confirmation_boundary_utc",
        "review_epoch",
        "confirmation_boundary_epoch",
        "entry_tick_epoch",
        "lower_confirmation_digit",
        "lower_confirmation_required",
        "lower_confirmation_count",
        "entry_digit",
        "quote_ask",
        "quote_payout",
        "outcome",
        "pnl",
    ]
    cols = [col for col in preferred if col in df.columns]
    st.dataframe(df[cols], use_container_width=True, height=320, hide_index=True)


@st.fragment(run_every=30.0)
def backup_fragment():
    journal = get_journal()

    with st.expander("Backup & restore", expanded=False):
        st.caption("The append-only archive is the master journal. Restore is idempotent and does not alter the live strategy.")

        a, b = st.columns(2)
        with a:
            st.download_button(
                "Download archive CSV",
                export_archive_csv_bytes(journal),
                "momentummaster_digit_archive.csv",
                "text/csv",
                use_container_width=True,
            )
        with b:
            st.download_button(
                "Download merged JSON",
                export_merged_json_bytes(journal),
                "momentummaster_digit_merged.json",
                "application/json",
                use_container_width=True,
            )

        uploaded = st.file_uploader("Import backup", type=["csv", "json"], key="digit_backup_upload")
        if uploaded is not None and st.button("Restore backup", type="primary", use_container_width=True, key="digit_restore"):
            result = import_journal(journal, uploaded.read(), uploaded.name)
            st.success(f"Restore complete: {result}")
            st.cache_data.clear()


watchdog_fragment()
status_fragment()
metrics_fragment()
digit_panel_fragment()
ledger_fragment()
journal_fragment()
backup_fragment()
