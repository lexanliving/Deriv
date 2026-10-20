"""pages/manual_touch.py — Manual ONE TOUCH one-shot speed bot UI (warm session)."""

from __future__ import annotations

import os
import sys
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
    html, body, .stApp { background:#060912; color:#c7d2e0; font-family:Inter,system-ui,sans-serif; }
    [data-testid="stSidebar"] { background:#0a0f1c; border-right:1px solid #1b2740; }
    [data-testid="stMainBlockContainer"] { max-width:1400px; padding-top:1.1rem; }
    .mt-panel { background:linear-gradient(160deg,#0c1322,#0a101d); border:1px solid #18233a;
                border-radius:15px; padding:16px 18px; margin-bottom:14px; }
    .mt-h { font-size:.68rem; font-weight:800; letter-spacing:.16em; text-transform:uppercase;
            color:#8294b0; margin-bottom:10px; }
    .mut { color:#6b7c97; }
    .pos { color:#4ade80; }
    .neg { color:#fb7185; }
    </style>
    """,
    unsafe_allow_html=True,
)


def run_async(coro):
    import asyncio
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


import streamlit as st  # noqa: E402  (kept for clarity)


@st.cache_data(ttl=30, show_spinner=False)
def load_accounts(api_token: str, app_id: str):
    if not api_token or not app_id:
        return []
    return run_async(DerivAPIClient.get_accounts(api_token, app_id))


if "mt_state" not in st.session_state or not hasattr(st.session_state.mt_state, "update_tick"):
    st.session_state.mt_state = StateManager()

state: StateManager = st.session_state.mt_state

st.markdown(
    """
    <div class="mt-panel">
      <div class="mt-h">Manual ONE TOUCH · One-Shot Speed Bot · Warm Session</div>
      <div class="mut">
        The connection and proposal are warmed in the background.
        START arms the one-shot instantly. 3 trend ticks + reversal = instant buy.
        Broken sequence or finished trade = auto stop. Connection stays warm for the next START.
      </div>
    </div>
    """,
    unsafe_allow_html=True,
)

with st.sidebar:
    st.markdown("### API / Account")

    api_token = st.text_input("DERIV_API_TOKEN", value=str(DERIV_API_TOKEN or ""), type="password")
    app_id = st.text_input("DERIV_APP_ID", value=str(DERIV_APP_ID or ""))

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
        account_map = {str(a.get("account_id")): a for a in accounts if isinstance(a, dict) and a.get("account_id")}
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
        account_id = st.text_input("Account ID", value="")
        account_currency = st.text_input("Currency", value=str(CURRENCY or "USD")).upper()
        account_type = st.text_input("Account type", value="DEMO").upper()

    st.markdown("### Trade settings")

    stake = st.number_input("Stake", min_value=0.35, max_value=100000.0,
                            value=float(DEFAULT_INITIAL_STAKE or 1.0), step=0.05, key="mt_stake")

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
    barrier_offset = c1.number_input("Barrier offset", min_value=0.0001, max_value=1000.0,
                                     value=0.08, step=0.01, key="mt_barrier_offset")
    barrier_decimals = c2.number_input("Barrier decimals", min_value=0, max_value=10,
                                       value=2, step=1, key="mt_barrier_decimals")

    duration_ticks = st.slider("Duration ticks", min_value=1, max_value=10, value=5, key="mt_duration")
    required_trend_ticks = st.slider("Trend ticks before reversal", min_value=1, max_value=10,
                                     value=3, key="mt_required_ticks")

    flat_invalid = st.checkbox("Flat tick invalidates sequence", value=True, key="mt_flat_invalid")
    exact_one_shot = st.checkbox("Exact one-shot: 3 trend ticks then reversal", value=True, key="mt_exact_one_shot")
    max_wait_seconds = st.number_input("Max wait seconds before invalidating", min_value=0.0, max_value=300.0,
                                       value=15.0, step=1.0, key="mt_max_wait")

    st.markdown("### Market")

    symbol = st.text_input("Symbol", value="1HZ10V", key="mt_symbol")
    symbol_display = st.text_input("Symbol display", value="Volatility 10 (1s)", key="mt_symbol_display")

    st.markdown("### Speed settings")

    proposal_refresh_seconds = st.number_input("Proposal refresh seconds", min_value=0.05, max_value=2.0,
                                               value=0.25, step=0.05, key="mt_proposal_refresh")
    proposal_max_age_seconds = st.number_input("Max proposal age seconds", min_value=0.10, max_value=5.0,
                                               value=1.00, step=0.05, key="mt_proposal_max_age")
    proposal_timeout = st.number_input("Proposal timeout (seconds)", min_value=0.25, max_value=5.0,
                                       value=0.9, step=0.1, key="mt_proposal_timeout")
    request_timeout = st.number_input("General request timeout (seconds)", min_value=0.5, max_value=10.0,
                                      value=2.0, step=0.1, key="mt_request_timeout")

    st.markdown("### Safety")

    real_confirm = st.text_input("Type LIVE to enable real orders", value="", key="mt_real_confirm")
    st.checkbox("Auto refresh dashboard", value=True, key="mt_auto_refresh")

# ---------------------------------------------------------------- engine session

config_valid = bool(api_token.strip() and app_id.strip() and account_id.strip() and symbol.strip() and stake > 0)

engine: TradingEngine = st.session_state.get("mt_engine")
session_key = (api_token.strip(), app_id.strip(), account_id.strip())

if engine is not None and getattr(engine, "session_key", None) != session_key:
    engine.halt()
    engine = None

if engine is None and config_valid:
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
        account_type=account_type,
        real_execution_confirmed=str(real_confirm).strip().upper() == "LIVE",
        barrier_sign=barrier_sign,
        barrier_offset=float(barrier_offset),
        barrier_decimal_places=int(barrier_decimals),
        required_trend_ticks=int(required_trend_ticks),
        flat_invalid=bool(flat_invalid),
        exact_one_shot=bool(exact_one_shot),
        max_wait_seconds=float(max_wait_seconds),
        proposal_refresh_seconds=float(proposal_refresh_seconds),
        proposal_max_age_seconds=float(proposal_max_age_seconds),
        proposal_timeout=float(proposal_timeout),
        request_timeout=float(request_timeout),
    )
    st.session_state.mt_engine = engine
    engine.start_background()

if engine is not None:
    engine.apply_settings(
        initial_stake=float(stake),
        barrier_sign=barrier_sign,
        barrier_offset=float(barrier_offset),
        barrier_decimal_places=int(barrier_decimals),
        contract_duration=int(duration_ticks),
        required_trend_ticks=int(required_trend_ticks),
        flat_invalid=bool(flat_invalid),
        exact_one_shot=bool(exact_one_shot),
        max_wait_seconds=float(max_wait_seconds),
        proposal_refresh_seconds=float(proposal_refresh_seconds),
        proposal_max_age_seconds=float(proposal_max_age_seconds),
        proposal_timeout=float(proposal_timeout),
        request_timeout=float(request_timeout),
        real_execution_confirmed=str(real_confirm).strip().upper() == "LIVE",
    )

snap = engine.snapshot() if engine is not None else {
    "warm": "OFF", "armed": False, "settle": False, "price": 0.0,
    "barrier": "", "contract": "ONETOUCH", "duration_ticks": 5, "required_ticks": 3,
    "proposal_hot": False, "proposal_age": None, "proposal_error": "",
    "manual_trend_ticks": 0, "manual_armed": False, "manual_last_rejection": "",
}

warm = snap.get("warm", "OFF")
armed = bool(snap.get("armed")) or bool(state.is_running and not snap.get("settle"))
settling = bool(snap.get("settle"))

c1, c2 = st.columns(2)

with c1:
    start_clicked = st.button(
        "START",
        type="primary",
        use_container_width=True,
        disabled=(not config_valid) or warm != "READY" or armed or settling,
        help="Arms the one-shot instantly. Connection and proposal are already warm."
    )

with c2:
    stop_clicked = st.button(
        "STOP",
        type="secondary",
        use_container_width=True,
        disabled=not (armed or settling),
    )

if start_clicked and engine is not None:
    if not engine.arm():
        st.warning("Could not arm. Wait for the connection to become READY.")
    st.rerun()

if stop_clicked and engine is not None:
    engine.disarm("Manual stop.")
    st.rerun()

if not config_valid:
    st.warning("Fill in API token, app id, account id, symbol, and stake to warm the connection.")

# ------------------------------------------------------------------ status panel

warm_label = {
    "READY": "🟢 WARM — instant start available",
    "CONNECTING": "🟡 Connecting…",
    "DOWN": "🔴 Connection down — retrying…",
    "OFF": "⚪ Not connected",
}.get(warm, warm)

m1, m2, m3, m4, m5, m6 = st.columns(6)
m1.metric("Connection", warm_label)
m2.metric("Price", f"{snap.get('price', 0.0):.5f}" if snap.get("price") else "—")
m3.metric("Barrier", snap.get("barrier", ""))
m4.metric("Contract", "ONETOUCH")
m5.metric("Armed", "YES" if armed else ("SETTLING" if settling else "NO"))
m6.metric("Trend ticks", snap.get("manual_trend_ticks", 0))

p1, p2, p3 = st.columns(3)
p1.metric("Hot proposal", "YES" if snap.get("proposal_hot") else "NO")
p2.metric("Proposal age (s)", snap.get("proposal_age") if snap.get("proposal_age") is not None else "—")
p3.metric("State", "SETTLING" if settling else ("ARMED" if armed else "IDLE"))

st.markdown(f"**Status:** {state.status_message}")

if state.error_message:
    st.error(state.error_message)

if snap.get("proposal_error"):
    st.caption(f"Proposal keeper note: {snap['proposal_error']}")

st.markdown(f"**Last engine note:** {snap.get('manual_last_rejection', '')}")

perf = state.get_performance_stats()

st.markdown('<div class="mt-h">Performance</div>', unsafe_allow_html=True)

q1, q2, q3, q4, q5, q6 = st.columns(6)
q1.metric("Closed trades", perf.get("total_trades", 0))
q2.metric("Wins", perf.get("wins", 0))
q3.metric("Losses", perf.get("losses", 0))
q4.metric("Win rate %", perf.get("win_rate", 0.0))
q5.metric("Session P&L", f"{perf.get('session_pnl', 0.0):+.2f}")
q6.metric("Total P&L", f"{perf.get('total_pnl', 0.0):+.2f}")

st.markdown('<div class="mt-h">Trade history</div>', unsafe_allow_html=True)

history = state.get_trade_history()
if not history:
    st.info("No trades yet.")
else:
    rows = [
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
        for t in history[:100]
    ]
    st.dataframe(rows, use_container_width=True, hide_index=True)

# Auto refresh only while something is active → zero lag when idle.
if st.session_state.get("mt_auto_refresh", False) and (armed or settling or warm in ("CONNECTING", "DOWN")):
    time.sleep(0.8)
    st.rerun()
