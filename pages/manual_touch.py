"""pages/manual_touch.py — Manual ONE TOUCH speed bot UI."""

from __future__ import annotations

import asyncio
import os
import sys
import threading
import time

import streamlit as st

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

try:
    from config import DERIV_API_TOKEN, DERIV_APP_ID, CURRENCY, DEFAULT_INITIAL_STAKE
except Exception:
    DERIV_API_TOKEN = os.getenv("DERIV_API_TOKEN", "")
    DERIV_APP_ID = os.getenv("DERIV_APP_ID", "")
    CURRENCY = "USD"
    DEFAULT_INITIAL_STAKE = 1.0

from src.api_client import DerivAPIClient
from src.state_manager import StateManager
from src.trading_engine import TradingEngine

try:
    st.set_page_config(
        page_title="Manual One Touch Bot",
        page_icon="⚡",
        layout="wide",
        initial_sidebar_state="expanded",
    )
except Exception:
    pass

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
        max-width: 1400px;
        padding-top: 1.1rem;
    }
    .mt-panel {
        background: linear-gradient(160deg, #0c1322, #0a101d);
        border: 1px solid #18233a;
        border-radius: 15px;
        padding: 16px 18px;
        margin-bottom: 14px;
    }
    .mt-h {
        font-size: .68rem;
        font-weight: 800;
        letter-spacing: .16em;
        text-transform: uppercase;
        color: #8294b0;
        margin-bottom: 10px;
    }
    .mut { color: #6b7c97; }
    .pos { color: #4ade80; }
    .neg { color: #fb7185; }
    </style>
    """,
    unsafe_allow_html=True,
)


def run_async(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@st.cache_data(ttl=30, show_spinner=False)
def load_accounts(api_token: str, app_id: str):
    if not api_token or not app_id:
        return []
    return run_async(DerivAPIClient.get_accounts(api_token, app_id))


if "mt_state" not in st.session_state or not hasattr(st.session_state.mt_state, "_manual_mode"):
    st.session_state.mt_state = StateManager()

state: StateManager = st.session_state.mt_state

thread = st.session_state.get("mt_thread")
if thread is not None and not thread.is_alive() and state.is_running:
    state.set_running(False)

engine_active = state.is_running or (thread is not None and thread.is_alive())

st.markdown(
    """
    <div class="mt-panel">
      <div class="mt-h">Manual ONE TOUCH · Speed Bot</div>
      <div class="mut">
        You analyze the market, choose the bias, press START, and the bot waits for the first reversal tick.
        It keeps a hot proposal ready so the buy request fires immediately.
      </div>
    </div>
    """,
    unsafe_allow_html=True,
)

with st.sidebar:
    st.markdown("### API / Account")

    api_token = st.text_input(
        "DERIV_API_TOKEN",
        value=str(DERIV_API_TOKEN or ""),
        type="password",
        help="Personal access token with trade scope.",
    )
    app_id = st.text_input(
        "DERIV_APP_ID",
        value=str(DERIV_APP_ID or ""),
        help="Deriv app id.",
    )

    accounts = []
    if api_token and app_id:
        try:
            accounts = load_accounts(api_token.strip(), app_id.strip())
        except Exception as exc:
            st.error(f"Could not load accounts: {exc}")

    account_id = ""
    account_currency = str(CURRENCY or "USD").upper()
    account_type = "UNKNOWN"

    if accounts:
        account_map = {
            str(a.get("account_id")): a
            for a in accounts
            if isinstance(a, dict) and a.get("account_id")
        }
        options = list(account_map.keys())

        selected_account = st.selectbox(
            "Account",
            options=options,
            format_func=lambda value: (
                f"{str(account_map[value].get('account_type', 'UNKNOWN')).upper()} · {value} · "
                f"{account_map[value].get('currency', 'USD')} "
                f"{float(account_map[value].get('balance', 0) or 0):,.2f}"
            ),
            key="mt_account",
        )

        account_id = selected_account
        account_currency = str(account_map[selected_account].get("currency", CURRENCY)).upper()
        account_type = str(account_map[selected_account].get("account_type", "UNKNOWN")).upper()
    else:
        account_id = st.text_input("Account ID", value="", help="Manual account id fallback.")
        account_currency = st.text_input("Currency", value=str(CURRENCY or "USD")).upper()
        account_type = st.text_input("Account type", value="DEMO").upper()

    st.markdown("### Trade settings")

    stake = st.number_input(
        "Stake",
        min_value=0.35,
        max_value=100000.0,
        value=float(DEFAULT_INITIAL_STAKE or 1.0),
        step=0.05,
        key="mt_stake",
    )

    barrier_choice = st.radio(
        "Barrier bias",
        options=[
            "+0.08 — wait for UP ticks, enter ONE TOUCH +0.08 on first DOWN tick",
            "-0.08 — wait for DOWN ticks, enter ONE TOUCH -0.08 on first UP tick",
        ],
        key="mt_barrier_choice",
    )
    barrier_sign = "+" if barrier_choice.startswith("+") else "-"

    c1, c2 = st.columns(2)
    barrier_offset = c1.number_input(
        "Barrier offset",
        min_value=0.0001,
        max_value=1000.0,
        value=0.08,
        step=0.01,
        key="mt_barrier_offset",
    )
    barrier_decimals = c2.number_input(
        "Barrier decimals",
        min_value=0,
        max_value=10,
        value=2,
        step=1,
        key="mt_barrier_decimals",
    )

    duration_ticks = st.slider(
        "Duration ticks",
        min_value=1,
        max_value=10,
        value=5,
        key="mt_duration",
    )

    required_trend_ticks = st.slider(
        "Trend ticks before reversal",
        min_value=1,
        max_value=10,
        value=3,
        key="mt_required_ticks",
    )

    ignore_flat_ticks = st.checkbox("Ignore flat ticks", value=True, key="mt_ignore_flat")
    exact_streak_only = st.checkbox(
        "Require exactly N trend ticks",
        value=False,
        help="If enabled, the bot only enters after exactly N trend ticks, not more.",
        key="mt_exact_streak",
    )

    st.markdown("### Market")

    symbol = st.text_input("Symbol", value="1HZ10V", key="mt_symbol")
    symbol_display = st.text_input("Symbol display", value="Volatility 10 (1s)", key="mt_symbol_display")

    st.markdown("### Speed settings")

    proposal_hot_mode = st.selectbox(
        "Proposal pre-cache",
        options=["ARMED", "ALWAYS", "OFF"],
        format_func=lambda x: {
            "ARMED": "When armed (recommended)",
            "ALWAYS": "Always (most aggressive)",
            "OFF": "Off",
        }.get(x, x),
        index=0,
        key="mt_proposal_hot_mode",
        help=(
            "When armed keeps a proposal ready as soon as the trend condition is armed. "
            "Always keeps requesting proposals even before the setup is armed. "
            "Off disables pre-caching and will be slower."
        ),
    )

    proposal_refresh_seconds = st.number_input(
        "Proposal refresh seconds",
        min_value=0.05,
        max_value=2.0,
        value=0.30,
        step=0.05,
        key="mt_proposal_refresh",
        help="How often the bot refreshes the cached proposal while hot caching is active.",
    )

    proposal_max_age_seconds = st.number_input(
        "Max proposal age seconds",
        min_value=0.10,
        max_value=5.0,
        value=1.00,
        step=0.05,
        key="mt_proposal_max_age",
        help="If the cached proposal is older than this, the bot will not use it.",
    )

    proposal_timeout = st.number_input(
        "Proposal timeout (seconds)",
        min_value=0.25,
        max_value=5.0,
        value=0.9,
        step=0.1,
        key="mt_proposal_timeout",
    )

    request_timeout = st.number_input(
        "General request timeout (seconds)",
        min_value=0.5,
        max_value=10.0,
        value=2.0,
        step=0.1,
        key="mt_request_timeout",
    )

    st.markdown("### Safety")

    real_confirm = st.text_input(
        "Type LIVE to enable real orders",
        value="",
        key="mt_real_confirm",
        help="Only used if the selected account is a real account.",
    )

    st.checkbox("Auto refresh dashboard", value=True, key="mt_auto_refresh")

can_start = bool(api_token.strip() and app_id.strip() and account_id.strip() and symbol.strip() and stake > 0)

c1, c2 = st.columns(2)

with c1:
    start_clicked = st.button(
        "START",
        type="primary",
        use_container_width=True,
        disabled=engine_active or not can_start,
    )

with c2:
    stop_clicked = st.button(
        "STOP",
        type="secondary",
        use_container_width=True,
        disabled=not state.is_running,
    )

if start_clicked and can_start and not engine_active:
    state.reset_for_new_session(float(stake))

    real_confirmed = str(real_confirm).strip().upper() == "LIVE"

    engine = TradingEngine(
        api_token=api_token.strip(),
        app_id=app_id.strip(),
        account_id=account_id.strip(),
        account_currency=account_currency,
        state=state,
        initial_stake=float(stake),
        symbol=symbol.strip(),
        symbol_display=symbol_display.strip(),
        contract_duration=int(duration_ticks),
        contract_duration_unit="t",
        account_type=account_type,
        real_execution_confirmed=real_confirmed,
        barrier_sign=barrier_sign,
        barrier_offset=float(barrier_offset),
        barrier_decimal_places=int(barrier_decimals),
        contract_style="ONETOUCH",
        required_trend_ticks=int(required_trend_ticks),
        ignore_flat_ticks=bool(ignore_flat_ticks),
        exact_streak_only=bool(exact_streak_only),
        stop_after_trade=True,
        proposal_timeout=float(proposal_timeout),
        request_timeout=float(request_timeout),
        proposal_hot_mode=str(proposal_hot_mode),
        proposal_refresh_seconds=float(proposal_refresh_seconds),
        proposal_max_age_seconds=float(proposal_max_age_seconds),
    )

    def _run_engine(engine_instance: TradingEngine, shared_state: StateManager):
        try:
            asyncio.run(engine_instance.run())
        except Exception as exc:
            shared_state.set_error(str(exc))
        finally:
            shared_state.set_running(False)

    state.set_running(True)
    state.clear_error()
    state.set_status("Starting manual one-touch engine…")

    worker = threading.Thread(
        target=_run_engine,
        args=(engine, state),
        daemon=True,
    )
    worker.start()

    st.session_state.mt_engine = engine
    st.session_state.mt_thread = worker
    st.rerun()

if stop_clicked:
    state.request_stop()
    state.set_status("Stop requested…")
    st.rerun()

if not can_start:
    st.warning("Fill in API token, app id, account id, symbol, and stake before starting.")

strategy_state = state.get_strategy_state()
perf = state.get_performance_stats()

m1, m2, m3, m4, m5, m6 = st.columns(6)

m1.metric("Engine", "RUNNING" if state.is_running else "STOPPED")
m2.metric("Price", f"{state.current_price:.5f}" if state.current_price else "—")
m3.metric("Barrier", strategy_state.get("manual_barrier", ""))
m4.metric("Contract", "ONETOUCH")
m5.metric("Armed", "YES" if strategy_state.get("manual_armed") else "NO")
m6.metric("Trend ticks", strategy_state.get("manual_trend_ticks", 0))

st.markdown(f"**Status:** {state.status_message}")

if state.error_message:
    st.error(state.error_message)

st.markdown(f"**Last engine note:** {strategy_state.get('manual_last_rejection', '')}")

st.markdown('<div class="mt-h">Performance</div>', unsafe_allow_html=True)

p1, p2, p3, p4, p5, p6 = st.columns(6)
p1.metric("Closed trades", perf.get("total_trades", 0))
p2.metric("Wins", perf.get("wins", 0))
p3.metric("Losses", perf.get("losses", 0))
p4.metric("Win rate %", perf.get("win_rate", 0.0))
p5.metric("Session P&L", f"{perf.get('session_pnl', 0.0):+.2f}")
p6.metric("Total P&L", f"{perf.get('total_pnl', 0.0):+.2f}")

st.markdown('<div class="mt-h">Trade history</div>', unsafe_allow_html=True)

history = state.get_trade_history()
if not history:
    st.info("No trades yet.")
else:
    rows = []
    for t in history:
        rows.append(
            {
                "time": t.timestamp,
                "direction": t.direction,
                "stake": t.stake,
                "barrier": t.barrier,
                "status": t.status,
                "pnl": t.pnl,
                "contract_id": t.contract_id or "",
                "mode": t.execution_mode,
                "error": t.error_message or "",
            }
        )
    st.dataframe(rows, use_container_width=True, hide_index=True)

if st.session_state.get("mt_auto_refresh", False) and state.is_running:
    time.sleep(0.75)
    st.rerun()
