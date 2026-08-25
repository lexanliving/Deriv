"""MomentumMaster Digit — Streamlit terminal for multi-market 1–2 tick Over 6 contracts."""
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
    DERIV_APP_ID,
    DERIV_API_TOKEN,
    DIGIT_DEFAULT_TICK_DURATION,
    DIGIT_DEFAULT_RECOVERY_ENABLED,
    DIGIT_DEFAULT_RECOVERY_MULTIPLIER,
    DIGIT_LOWER_CONFIRM_MAX,
    DIGIT_LOWER_CONFIRMATION_MAX,
    DIGIT_MAX_RECOVERY_STEPS,
    DIGIT_MIN_OVER6_SHARES,
    DIGIT_REVIEW_INTERVAL_SECONDS,
    DIGIT_TICK_DURATION_OPTIONS,
    DIGIT_UPPER_MODE,
    DIGIT_WINDOWS,
    DIGIT_WINDOW_ENABLED,
    GLOBAL_TAKE_PROFIT_TARGET,
    MANAGED_SYMBOLS,
    MARKET_ICONS,
)
from src.api_client import DerivAPIClient, DerivAPIError
from src.coordination import GlobalRiskCoordinator
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

configured_pat = DERIV_API_TOKEN.strip()
global_risk = GlobalRiskCoordinator()

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

if "managers" not in st.session_state:
    st.session_state.managers = {}
if "should_run" not in st.session_state:
    st.session_state.should_run = False
if "engine_config" not in st.session_state:
    st.session_state.engine_config = None
if "selected_markets" not in st.session_state:
    st.session_state.selected_markets = []
if "market_catalog" not in st.session_state:
    st.session_state.market_catalog = dict(AVAILABLE_MARKETS)
if "auto_restart" not in st.session_state:
    st.session_state.auto_restart = {}

SYMBOL_TO_LABEL = {str(v): str(k) for k, v in AVAILABLE_MARKETS.items()}


def _any_engine_alive() -> bool:
    managers = st.session_state.get("managers", {})
    for mgr in managers.values():
        thread = mgr.get("thread")
        if thread is not None and thread.is_alive():
            return True
    return False


any_engine_alive = _any_engine_alive()
ui_locked = bool(st.session_state.get("should_run", False) or any_engine_alive)


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


@st.cache_data(ttl=60, show_spinner=False)
def _load_accounts(app_id: str, token: str):
    return asyncio.run(DerivAPIClient.get_accounts(token, app_id))


@st.cache_data(ttl=180, show_spinner=False)
def _load_live_digit_markets(app_id: str, token: str, account_id: str):
    async def fetch():
        client = DerivAPIClient(token, app_id, account_id)
        if not await client.connect():
            raise DerivAPIError(client.last_error or "Could not connect while loading markets.", "MARKET_LOAD_FAILED")
        try:
            symbols = await client.get_active_symbols(full=True)
            result = {}
            allowed = set(MANAGED_SYMBOLS)
            for item in symbols:
                symbol = str(item.get("symbol", "")).strip()
                suspended = str(item.get("is_trading_suspended", "")).strip().lower() in {"1", "true", "yes", "y"}
                if not symbol or suspended or symbol not in allowed:
                    continue
                label = SYMBOL_TO_LABEL.get(symbol, f"{MARKET_ICONS.get(symbol, '📈')} {symbol}")
                result[label] = symbol
            return dict(sorted(result.items(), key=lambda pair: pair[0].lower()))
        finally:
            await client.disconnect()

    return asyncio.run(fetch())


def _fallback_markets() -> Dict[str, str]:
    return dict(sorted(AVAILABLE_MARKETS.items(), key=lambda pair: pair[0].lower()))


def _today_filled_trade_count(state_obj: StateManager) -> int:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return sum(
        1
        for trade in state_obj.get_trade_history()
        if getattr(trade, "contract_id", None) is not None and str(trade.timestamp).startswith(today)
    )


def _create_engine(state: StateManager, config: Dict[str, Any], symbol: str, symbol_display: str) -> TradingEngine:
    return TradingEngine(
        api_token=config["api_token"],
        app_id=config["app_id"],
        account_id=config["account_id"],
        account_currency=config["account_currency"],
        state=state,
        initial_stake=config["initial_stake"],
        max_martingale_steps=config["max_steps"],
        symbol=symbol,
        symbol_display=symbol_display,
        contract_duration=config["duration_ticks"],
        contract_duration_unit="t",
        account_type=config["account_type"],
        real_execution_confirmed=config["real_execution_confirmed"],
        martingale_multiplier=config["martingale_multiplier"],
        quote_precision=config.get("quote_precision", 2),
        min_over6_share=config.get("min_over6_share", 0.31),
        min_over6_shares=config.get("min_over6_shares"),
        lower_tick_max=config["lower_tick_max"],
        review_interval_seconds=config["review_interval_seconds"],
        required_lower_confirmations=config.get("required_lower_confirmations", 1),
        digit_windows=config.get("digit_windows"),
        digit_window_enabled=config.get("digit_window_enabled"),
        upper_mode=config.get("upper_mode", "kill"),
    )


def _launch_market(
    config: Dict[str, Any],
    symbol: str,
    symbol_display: str,
    existing_state: StateManager | None = None,
    reset_state: bool = True,
) -> Dict[str, Any]:
    state = existing_state if existing_state is not None else StateManager()
    manager = {
        "symbol": symbol,
        "display": symbol_display,
        "state": state,
        "engine": None,
        "loop": None,
        "thread": None,
        "config": config,
    }

    try:
        if reset_state:
            state.reset_for_new_session(config["initial_stake"])
        else:
            state.clear_error()
            history = state.get_trade_history()
            if history and history[0].status == "OPEN":
                state.update_trade_outcome(
                    history[0].trade_id,
                    "UNKNOWN",
                    0.0,
                    "The engine restarted while a contract was open — check the Deriv statement.",
                )

        engine = _create_engine(state, config, symbol, symbol_display)

        if not reset_state:
            engine._daily_trade_count = _today_filled_trade_count(state)
            engine._daily_date = datetime.now(timezone.utc).date()

        state.set_running(True)
        loop = asyncio.new_event_loop()
        thread = threading.Thread(
            target=_run_engine_in_thread,
            args=(engine, loop),
            daemon=True,
            name=f"DigitTradingEngineThread-{symbol}",
        )
        try:
            add_script_run_ctx(thread)
        except Exception:
            pass
        thread.start()

        manager["engine"] = engine
        manager["loop"] = loop
        manager["thread"] = thread
    except Exception as exc:
        state.set_running(False)
        state.set_error(f"The engine could not start: {exc}")
        state.set_status("Could not start.")

    return manager


def start_bot(base_config: Dict[str, Any], selected_labels: List[str], market_catalog: Dict[str, str]) -> None:
    if _any_engine_alive():
        return

    selected_labels = [label for label in selected_labels if label in market_catalog]
    if not selected_labels:
        return

    st.session_state.engine_config = base_config
    st.session_state.selected_markets = selected_labels
    st.session_state.market_catalog = market_catalog
    st.session_state.should_run = True
    st.session_state.auto_restart = {}

    managers = {}
    for label in selected_labels:
        symbol = market_catalog[label]
        cfg = dict(base_config)
        cfg["symbol"] = symbol
        cfg["symbol_display"] = label
        st.session_state.auto_restart[symbol] = {"count": 0, "last": 0.0}
        managers[symbol] = _launch_market(cfg, symbol, label, None, True)

    st.session_state.managers = managers

    if not _any_engine_alive():
        st.session_state.should_run = False


def stop_bot() -> None:
    st.session_state.should_run = False
    for mgr in st.session_state.get("managers", {}).values():
        state = mgr.get("state")
        if state is not None:
            state.request_stop()
            state.set_status("Stopping…")


@st.fragment(run_every=2.0)
def watchdog_fragment():
    if not st.session_state.get("should_run", False):
        return

    managers = st.session_state.get("managers", {})
    if not managers:
        return

    if all(
        mgr.get("state") is not None and mgr["state"].stop_requested
        for mgr in managers.values()
    ):
        st.session_state.should_run = False
        return

    now = time.monotonic()

    for symbol, mgr in list(managers.items()):
        state = mgr.get("state")
        thread = mgr.get("thread")
        if state is None:
            continue
        if state.stop_requested:
            continue
        if thread is not None and thread.is_alive():
            continue
        if not st.session_state.get("should_run", False):
            break

        restart_meta = st.session_state.auto_restart.get(symbol, {"count": 0, "last": 0.0})
        if now - restart_meta["last"] < 20.0:
            continue
        if restart_meta["count"] >= 5:
            state.set_error("Engine stopped 5 times in a row — auto-restart paused.")
            state.set_status("Auto-restart paused.")
            continue

        cfg = mgr.get("config") or st.session_state.get("engine_config")
        if not cfg:
            continue

        restart_meta["last"] = now
        restart_meta["count"] += 1
        st.session_state.auto_restart[symbol] = restart_meta
        state.set_status(f"Engine stopped unexpectedly — restarting ({restart_meta['count']}/5)…")

        st.session_state.managers[symbol] = _launch_market(
            cfg,
            symbol,
            mgr.get("display") or symbol,
            existing_state=state,
            reset_state=False,
        )


with st.sidebar:
    st.markdown("### Digit terminal")
    st.caption("Multi-market rolling 7–9 reviews · Over 6 · 1–2 ticks")

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
        account_map = {str(item.get("account_id")): item for item in accounts if item.get("account_id")}
        options = list(account_map)
        picked = st.selectbox(
            "Account",
            options=options,
            disabled=ui_locked,
            format_func=lambda value: (
                f"{normalize_account_type(account_map[value].get('account_type', 'UNKNOWN'))} · {value} · "
                f"{account_map[value].get('currency', 'USD')} {float(account_map[value].get('balance', 0) or 0):,.2f}"
            ),
        )
        account_id = picked
        selected_account = account_map[picked]
        account_type = normalize_account_type(selected_account.get("account_type", "UNKNOWN"))
        account_currency = str(selected_account.get("currency", "USD")).upper()
        st.success(f"{account_type} account · {account_currency} {float(selected_account.get('balance', 0) or 0):,.2f}")

    st.divider()

    market_catalog = _fallback_markets()
    if selected_account and DERIV_APP_ID and configured_pat:
        try:
            live_catalog = _load_live_digit_markets(DERIV_APP_ID, configured_pat, account_id)
            if live_catalog:
                market_catalog = live_catalog
                st.caption(f"{len(market_catalog)} active managed markets loaded from Deriv.")
        except Exception as exc:
            st.caption(f"Live catalogue unavailable; using local list ({str(exc)[:80]})")

    labels = list(market_catalog)

    if "market_multiselect" in st.session_state:
        st.session_state.market_multiselect = [
            label for label in st.session_state.market_multiselect if label in labels
        ]

    saved_selected = [label for label in st.session_state.get("selected_markets", []) if label in labels]
    default_selected = saved_selected if (ui_locked and saved_selected) else labels

    selected_markets = st.multiselect(
        "Markets to trade",
        options=labels,
        default=default_selected,
        disabled=ui_locked,
        key="market_multiselect",
        help="Start launches every selected market in this one dashboard session.",
    )

    st.caption("Only the 10 managed Volatility markets are shown.")

    st.divider()

    duration_ticks = st.select_slider(
        "Contract duration",
        options=list(DIGIT_TICK_DURATION_OPTIONS),
        value=DIGIT_DEFAULT_TICK_DURATION,
        disabled=ui_locked,
        format_func=lambda value: f"{value} tick" if value == 1 else f"{value} ticks",
    )

    engine_cfg = st.session_state.engine_config if isinstance(st.session_state.engine_config, dict) else {}
    default_lower = int(engine_cfg.get("required_lower_confirmations", 1)) if ui_locked else 1
    lower_confirmations = st.number_input(
        "Lower-tick confirmations before entry",
        min_value=1,
        max_value=int(DIGIT_LOWER_CONFIRMATION_MAX),
        value=default_lower,
        step=1,
        disabled=ui_locked,
        help="1 to 20 consecutive lower digits 0–6. The final lower digit triggers entry.",
    )

    upper_options = ["kill", "reset"]
    default_upper = str(engine_cfg.get("upper_mode", DIGIT_UPPER_MODE)).lower() if ui_locked else str(DIGIT_UPPER_MODE).lower()
    if default_upper not in upper_options:
        default_upper = "kill"
    upper_mode = st.selectbox(
        "Upper-digit behavior",
        options=upper_options,
        index=upper_options.index(default_upper),
        disabled=ui_locked,
        format_func=lambda value: "Kill signal on 7–9" if value == "kill" else "Reset sequence on 7–9",
        help=(
            "Kill: any 7–9 before the lower sequence completes kills the signal for that review window. "
            "Reset: any 7–9 before completion resets the lower sequence."
        ),
    )

    st.caption(
        "This is an entry-timing trigger only: the concentration rule still comes from the 7–9 review. "
        "The final required lower digit itself triggers entry."
    )

    st.divider()
    st.markdown("#### Rolling window gates")

    window_cfg = engine_cfg.get("digit_windows", DIGIT_WINDOWS) if ui_locked else DIGIT_WINDOWS
    enabled_cfg = engine_cfg.get("digit_window_enabled", DIGIT_WINDOW_ENABLED) if ui_locked else DIGIT_WINDOW_ENABLED
    share_cfg = engine_cfg.get("min_over6_shares", DIGIT_MIN_OVER6_SHARES) if ui_locked else DIGIT_MIN_OVER6_SHARES

    window_cfg = window_cfg or DIGIT_WINDOWS
    enabled_cfg = enabled_cfg or DIGIT_WINDOW_ENABLED
    share_cfg = share_cfg or DIGIT_MIN_OVER6_SHARES

    use_fast = st.checkbox(
        "Use fast window",
        value=bool(enabled_cfg.get("fast", True)),
        disabled=ui_locked,
        key="use_fast_window",
    )
    f1, f2 = st.columns(2)
    fast_ticks = f1.number_input(
        "Fast ticks",
        min_value=5,
        max_value=10000,
        value=int(window_cfg.get("fast", DIGIT_WINDOWS.get("fast", 20))),
        step=1,
        disabled=ui_locked or not use_fast,
        key="fast_ticks",
    )
    fast_pct = f2.number_input(
        "Fast 7–9 %",
        min_value=0.0,
        max_value=100.0,
        value=round(float(share_cfg.get("fast", DIGIT_MIN_OVER6_SHARES.get("fast", 0.31))) * 100.0, 2),
        step=1.0,
        disabled=ui_locked or not use_fast,
        key="fast_pct",
    )

    use_medium = st.checkbox(
        "Use medium window",
        value=bool(enabled_cfg.get("medium", True)),
        disabled=ui_locked,
        key="use_medium_window",
    )
    m1, m2 = st.columns(2)
    medium_ticks = m1.number_input(
        "Medium ticks",
        min_value=5,
        max_value=10000,
        value=int(window_cfg.get("medium", DIGIT_WINDOWS.get("medium", 50))),
        step=1,
        disabled=ui_locked or not use_medium,
        key="medium_ticks",
    )
    medium_pct = m2.number_input(
        "Medium 7–9 %",
        min_value=0.0,
        max_value=100.0,
        value=round(float(share_cfg.get("medium", DIGIT_MIN_OVER6_SHARES.get("medium", 0.31))) * 100.0, 2),
        step=1.0,
        disabled=ui_locked or not use_medium,
        key="medium_pct",
    )

    use_slow = st.checkbox(
        "Use slow window",
        value=bool(enabled_cfg.get("slow", True)),
        disabled=ui_locked,
        key="use_slow_window",
    )
    s1, s2 = st.columns(2)
    slow_ticks = s1.number_input(
        "Slow ticks",
        min_value=5,
        max_value=10000,
        value=int(window_cfg.get("slow", DIGIT_WINDOWS.get("slow", 200))),
        step=1,
        disabled=ui_locked or not use_slow,
        key="slow_ticks",
    )
    slow_pct = s2.number_input(
        "Slow 7–9 %",
        min_value=0.0,
        max_value=100.0,
        value=round(float(share_cfg.get("slow", DIGIT_MIN_OVER6_SHARES.get("slow", 0.30))) * 100.0, 2),
        step=1.0,
        disabled=ui_locked or not use_slow,
        key="slow_pct",
    )

    windows_enabled = bool(use_fast or use_medium or use_slow)
    if not windows_enabled:
        st.error("Enable at least one rolling window. If all windows are disabled, the bot can never arm.")

    st.caption(
        "Disabled windows are ignored completely. "
        "If only medium is enabled, fast and slow cannot block or reject the setup."
    )

    st.divider()
    st.markdown("#### Global app take-profit")

    g_state = global_risk.snapshot()
    current_global_target = float(g_state.get("take_profit_target", GLOBAL_TAKE_PROFIT_TARGET))
    global_target = st.number_input(
        "App-wide take-profit target",
        min_value=0.0,
        max_value=1000000.0,
        value=current_global_target,
        step=1.0,
        key="global_take_profit_target",
        help="0 disables global take-profit. When combined closed P&L across all markets reaches this amount, all markets stop.",
    )

    c_target, c_reset = st.columns(2)
    with c_target:
        apply_global_target = st.button("Apply target", use_container_width=True, key="apply_global_target")
    with c_reset:
        reset_global_session = st.button("Reset global P&L", use_container_width=True, key="reset_global_session")

    if apply_global_target:
        global_risk.set_target(float(global_target))
        st.rerun()
    if reset_global_session:
        global_risk.reset_session()
        st.rerun()

    g_state = global_risk.snapshot()
    st.caption(
        f"Global closed P&L: {float(g_state.get('session_pnl', 0.0)):+.2f} / "
        f"target {float(g_state.get('take_profit_target', 0.0)):.2f}"
    )
    if bool(g_state.get("stop_all", False)):
        st.error(str(g_state.get("stop_reason") or "Global stop active."))

    st.divider()
    st.markdown("#### Account safety")

    if selected_account and account_type == "REAL":
        live_text = st.text_input("Type LIVE to enable real orders", max_chars=4, disabled=ui_locked)
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
        disabled=ui_locked,
    )

    use_martingale = st.checkbox(
        "Enable recovery multiplier",
        value=bool(DIGIT_DEFAULT_RECOVERY_ENABLED),
        disabled=ui_locked,
    )

    martingale_multiplier = st.number_input(
        "Recovery multiplier",
        min_value=1.01,
        max_value=4.0,
        value=float(DIGIT_DEFAULT_RECOVERY_MULTIPLIER),
        step=0.01,
        format="%.2f",
        disabled=ui_locked,
    )

    max_steps = st.slider(
        "Maximum recovery steps",
        0,
        int(DIGIT_MAX_RECOVERY_STEPS),
        int(DIGIT_MAX_RECOVERY_STEPS if use_martingale else 0),
        disabled=ui_locked,
    )
    if not use_martingale:
        max_steps = 0

    st.caption(
        "Recovery: 1.10 means the next stake is 110% of the previous stake after a loss. "
        "There is no session loss-stop. Per-market take-profit has been replaced by the global app-wide take-profit above. "
        "Daily trade cap is disabled."
    )

    st.divider()

    c1, c2 = st.columns(2)
    with c1:
        start_pressed = st.button("Start", type="primary", use_container_width=True, disabled=ui_locked)
    with c2:
        stop_pressed = st.button("Stop", use_container_width=True, disabled=not ui_locked)

    if start_pressed:
        if not selected_account:
            st.error("Choose a Deriv account first.")
        elif not selected_markets:
            st.error("Select at least one market before starting.")
        elif not windows_enabled:
            st.error("Enable at least one rolling window before starting.")
        elif bool(global_risk.snapshot().get("stop_all", False)):
            st.error("Global stop is active. Reset global P&L before starting again.")
        else:
            base_config = {
                "api_token": configured_pat,
                "app_id": DERIV_APP_ID,
                "account_id": account_id,
                "account_currency": account_currency,
                "account_type": account_type,
                "real_execution_confirmed": real_execution_confirmed,
                "initial_stake": float(initial_stake),
                "max_steps": int(max_steps),
                "martingale_multiplier": float(martingale_multiplier),
                "duration_ticks": int(duration_ticks),
                "quote_precision": 2,
                "min_over6_share": float(medium_pct) / 100.0,
                "min_over6_shares": {
                    "fast": float(fast_pct) / 100.0,
                    "medium": float(medium_pct) / 100.0,
                    "slow": float(slow_pct) / 100.0,
                },
                "lower_tick_max": DIGIT_LOWER_CONFIRM_MAX,
                "required_lower_confirmations": int(lower_confirmations),
                "review_interval_seconds": DIGIT_REVIEW_INTERVAL_SECONDS,
                "digit_windows": {
                    "fast": int(fast_ticks),
                    "medium": int(medium_ticks),
                    "slow": int(slow_ticks),
                },
                "digit_window_enabled": {
                    "fast": bool(use_fast),
                    "medium": bool(use_medium),
                    "slow": bool(use_slow),
                },
                "upper_mode": str(upper_mode).lower(),
            }
            start_bot(base_config, selected_markets, market_catalog)
            st.rerun()

    if stop_pressed:
        stop_bot()
        st.rerun()

mode = "UNCONFIGURED"
if selected_account:
    mode = resolve_execution_mode(account_type, real_execution_confirmed)
mode_color = "#34d399" if mode == "DEMO" else "#fbbf24" if mode == "REAL" else "#8294b0"

st.markdown(
    f"""
    <div class="mm-head">
      <div>
        <div class="mm-logo">Momentum<b>·</b>Master DIGIT</div>
        <div class="mm-sub">Multi-market selection · rolling 7–9 frequency · Over 6 · 1–2 tick settlement</div>
      </div>
      <div style="text-align:right">
        <b style="color:{mode_color}">{html.escape(str(mode))}</b><br>
        <span class="muted">{html.escape(str(account_id[:12] if account_id else ""))}</span>
      </div>
    </div>
    """,
    unsafe_allow_html=True,
)


@st.fragment(run_every=2.0)
def global_risk_fragment():
    g = global_risk.snapshot()
    pnl = float(g.get("session_pnl", 0.0))
    target = float(g.get("take_profit_target", 0.0))
    stop_all = bool(g.get("stop_all", False))

    if stop_all:
        color = "#fb7185"
        label = f"Global closed P&L {pnl:+.2f} / target {target:.2f} — STOP ALL ACTIVE"
    elif target <= 0:
        color = "#8294b0"
        label = f"Global closed P&L {pnl:+.2f} / target disabled"
    else:
        color = "#34d399"
        label = f"Global closed P&L {pnl:+.2f} / target {target:.2f}"

    st.markdown(
        f'<div class="mm-card" style="border-left:4px solid {color};"><b>{html.escape(label)}</b></div>',
        unsafe_allow_html=True,
    )


@st.fragment(run_every=2.0)
def status_fragment():
    managers = st.session_state.get("managers", {})

    if not managers:
        message = "Stopped. Select markets and press Start."
        color = "#8294b0"
    else:
        errors = []
        statuses = []
        alive = 0

        for mgr in managers.values():
            state = mgr.get("state")
            if not state:
                continue
            if mgr.get("thread") and mgr["thread"].is_alive():
                alive += 1
            if state.error_message:
                errors.append(f"{mgr.get('display', mgr.get('symbol'))}: {state.error_message}")
            elif state.status_message:
                statuses.append(str(state.status_message))

        if errors:
            message = errors[0] if len(errors) == 1 else f"{len(errors)} market errors · {errors[0]}"
            color = "#fb7185"
        elif alive:
            g = global_risk.snapshot()
            message = (
                f"Running {alive}/{len(managers)} markets · collective closed P&L "
                f"{float(g.get('session_pnl', 0.0)):+.2f}"
            )
            color = "#34d399"
        else:
            message = statuses[-1] if statuses else "Stopped."
            color = "#8294b0"

    st.markdown(
        f'<div class="mm-card" style="border-left:4px solid {color};"><b>{html.escape(message)}</b></div>',
        unsafe_allow_html=True,
    )


@st.fragment(run_every=2.0)
def metrics_fragment():
    managers = list(st.session_state.get("managers", {}).values())
    g = global_risk.snapshot()
    collective_pnl = float(g.get("session_pnl", 0.0))
    target = float(g.get("take_profit_target", 0.0))

    if not managers:
        values = [
            ("Markets", "0", "no engines"),
            ("Collective P&L", f"{collective_pnl:+.2f}", f"target {target:.2f}"),
            ("Closed", "0W · 0L", "no trades"),
            ("Open", "0", "no contracts"),
            ("Status", "IDLE", "ready"),
        ]
    else:
        stats = []
        open_count = 0
        alive = 0

        for mgr in managers:
            state = mgr.get("state")
            if not state:
                continue
            perf = state.get_performance_stats()
            stats.append(perf)

            history = state.get_trade_history()
            if history and history[0].status == "OPEN":
                open_count += 1

            if mgr.get("thread") and mgr["thread"].is_alive():
                alive += 1

        wins = sum(int(x.get("wins", 0)) for x in stats)
        losses = sum(int(x.get("losses", 0)) for x in stats)

        values = [
            ("Markets", f"{alive}/{len(managers)}", "running engines"),
            ("Collective P&L", f"{collective_pnl:+.2f}", f"target {target:.2f}"),
            ("Closed", f"{wins}W · {losses}L", f"total closed {wins + losses}"),
            ("Open", str(open_count), "active contracts"),
            ("Global stop", "ACTIVE" if bool(g.get("stop_all", False)) else "OFF", str(g.get("stop_reason", "") or "ready")),
        ]

    cards = st.columns(5)
    for col, (label, value, sub) in zip(cards, values):
        with col:
            st.markdown(
                f'<div class="mm-card"><div class="mm-label">{label}</div>'
                f'<div class="mm-value">{html.escape(str(value))}</div>'
                f'<div class="mm-small">{html.escape(str(sub))}</div></div>',
                unsafe_allow_html=True,
            )


@st.fragment(run_every=2.0)
def markets_fragment():
    st.markdown('<div class="mm-section">Markets</div>', unsafe_allow_html=True)

    managers = st.session_state.get("managers", {})
    if not managers:
        st.info("No markets running yet.")
        return

    rows = []
    for symbol, mgr in managers.items():
        state = mgr.get("state")
        if not state:
            continue

        s = state.get_strategy_state()
        perf = state.get_performance_stats()
        mart = state.get_martingale_state()

        medium = s.get("digit_windows", {}).get("medium", {}) or {}
        medium_count = int(medium.get("count", 0) or 0)
        medium_high = int(medium.get("over6_count", 0) or 0)
        p_over = float(medium.get("p_over6", 0) or 0) * 100.0

        armed = bool(s.get("digit_armed"))
        condition_valid = bool(s.get("digit_condition_valid"))
        digit_state = "ARMED" if armed else ("QUALIFYING" if condition_valid else "DISARMED")

        status_text = state.error_message or state.status_message or ""

        rows.append(
            {
                "Market": mgr.get("display", symbol),
                "Status": "RUN" if mgr.get("thread") and mgr["thread"].is_alive() else "STOP",
                "Last digit": str(s.get("last_digit") if s.get("last_digit") is not None else "—"),
                "7–9 medium": f"{medium_high}/{medium_count} = {p_over:.1f}%",
                "Digit state": digit_state,
                "Lower": f"{int(s.get('digit_lower_confirmation_count', 0) or 0)}/{int(s.get('digit_required_lower_confirmations', 1) or 1)}",
                "Session P&L": f"{float(perf.get('session_pnl', 0.0)):+.2f}",
                "Next stake": f"{float(mart.get('stake', 0.0)):.2f}",
                "Message": str(status_text)[:120],
            }
        )

    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)


@st.fragment(run_every=8.0)
def ledger_fragment():
    st.markdown('<div class="mm-section">Trades</div>', unsafe_allow_html=True)

    managers = st.session_state.get("managers", {})
    records = []

    for mgr in managers.values():
        state = mgr.get("state")
        if not state:
            continue
        for trade in state.get_trade_history()[:50]:
            records.append(
                {
                    "Time": trade.timestamp,
                    "Market": mgr.get("display", mgr.get("symbol", "")),
                    "Direction": trade.direction,
                    "Barrier": trade.barrier,
                    "Stake": trade.stake,
                    "Result": trade.status,
                    "P&L": trade.pnl,
                    "Step": trade.martingale_step,
                    "Mode": trade.execution_mode,
                }
            )

    if not records:
        st.info("No trades yet. The bot will stand aside until the rolling digit rule qualifies.")
        return

    df = pd.DataFrame(records).sort_values("Time", ascending=False).head(50)
    st.dataframe(df, use_container_width=True, height=300, hide_index=True)


@st.fragment(run_every=15.0)
def journal_fragment():
    journal = get_journal()
    st.markdown('<div class="mm-section">Decision log · every minute review and entry decision</div>', unsafe_allow_html=True)

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
        "threshold",
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
global_risk_fragment()
status_fragment()
metrics_fragment()
markets_fragment()
ledger_fragment()
journal_fragment()
backup_fragment()
