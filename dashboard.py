"""
dashboard.py
MomentumMaster TF — a crafted multi-market candle-trend terminal.

Design: deep-navy terminal with a fine dot-grid and two restrained radial glows,
a Space Grotesk display face paired with JetBrains Mono numerals, a live scanline,
a pulsing status dot, hover-lift KPI cards, and a visual score bar. Live regions
refresh themselves via st.fragment at different cadences; the sidebar and header
are static. The engine runs in a background asyncio thread and is independent of
the UI; on shutdown the thread drains its tasks before closing the loop so the
harmless "Event loop is closed" cleanup noise is silenced.
"""

import asyncio
import html
import os
import sys
import threading
import time

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from streamlit.runtime.scriptrunner import add_script_run_ctx

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import (
    AVAILABLE_MARKETS,
    CONTRACT_DURATION,
    CONTRACT_DURATION_UNIT,
    DEFAULT_INITIAL_STAKE,
    DEFAULT_MARKET_DISPLAY,
    DEFAULT_MAX_MARTINGALE_STEPS,
    DEFAULT_STRATEGY_SENSITIVITY,
    DERIV_APP_ID,
    DERIV_API_TOKEN,
    MARTINGALE_MULTIPLIER,
    SCORE_MAX,
    STRATEGY_SENSITIVITY_PRESETS,
)
from src.api_client import DerivAPIClient, DerivAPIError
from src.state_manager import StateManager
from src.trading_engine import TradingEngine, normalize_account_type, resolve_execution_mode

st.set_page_config(
    page_title="MomentumMaster TF",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="expanded",
)

configured_pat = DERIV_API_TOKEN.strip()


@st.cache_data(ttl=60, show_spinner=False)
def _load_accounts(app_id: str, token: str):
    return asyncio.run(DerivAPIClient.get_accounts(token, app_id))


# ---------------------------------------------------------------------------
# Theme — bespoke terminal aesthetic (display + mono pairing, ambient depth,
# motion). No centered hero, no equal-card row, no aurora blobs, no glass.
# ---------------------------------------------------------------------------
st.markdown(
    """
<style>
@import url('https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;600;700&family=IBM+Plex+Sans:wght@400;500;600&family=JetBrains+Mono:wght@500;600;700&display=swap');

html, body, .stApp {
    background-color: #060912;
    color: #c7d2e0;
    font-family: 'IBM Plex Sans', -apple-system, BlinkMacSystemFont, sans-serif;
}
.stApp {
    background-image:
        radial-gradient(900px 460px at 6% -8%, rgba(16, 185, 129, 0.10), transparent 60%),
        radial-gradient(820px 440px at 100% 108%, rgba(56, 132, 255, 0.09), transparent 60%),
        radial-gradient(rgba(120, 150, 190, 0.05) 1px, transparent 1px);
    background-size: auto, auto, 22px 22px;
    background-attachment: fixed;
}
[data-testid="stMainBlockContainer"] { max-width: 1480px; padding-top: 1.3rem; }
[data-testid="stSidebar"] { background-color: #0a0f1c; border-right: 1px solid #1b2740; }
[data-testid="stSidebar"] [data-testid="stMarkdownContainer"] p,
[data-testid="stSidebar"] label { font-family: 'IBM Plex Sans', sans-serif; }

/* ---- Header band ---- */
.mm-header {
    display: flex; align-items: flex-end; justify-content: space-between;
    padding: 4px 2px 14px 2px; position: relative;
}
.mm-header::after {
    content: ""; position: absolute; left: 0; right: 0; bottom: 0; height: 2px;
    background: linear-gradient(90deg, #10b981, #3884ff 45%, transparent 92%);
    background-size: 220% 100%;
    animation: mm-scan 6s linear infinite;
    border-radius: 2px;
}
@keyframes mm-scan { 0% { background-position: 120% 0; } 100% { background-position: -120% 0; } }
.mm-logo {
    font-family: 'Space Grotesk', sans-serif; font-weight: 700;
    font-size: 1.32rem; letter-spacing: 0.18em; color: #eef3fb; text-transform: uppercase;
}
.mm-logo .mm-dot { color: #10b981; }
.mm-sub {
    font-family: 'JetBrains Mono', monospace; font-size: 0.7rem; color: #6b7c97;
    letter-spacing: 0.04em; margin-top: 3px;
}
.mm-acct { text-align: right; font-family: 'JetBrains Mono', monospace; }
.mm-acct .mm-mode { font-size: 0.82rem; font-weight: 600; color: #eef3fb; letter-spacing: 0.06em; }
.mm-acct .mm-id { font-size: 0.68rem; color: #6b7c97; margin-top: 2px; }

/* ---- Status dot ---- */
.mm-dotlive { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 7px; vertical-align: middle; }
.mm-run { background: #10b981; animation: mm-pulse 2.2s ease-in-out infinite; }
.mm-stop { background: #5b6b85; }
.mm-err { background: #f43f5e; box-shadow: 0 0 9px #f43f5e99; }
@keyframes mm-pulse { 0%,100% { box-shadow: 0 0 4px rgba(16,185,129,0.5); } 50% { box-shadow: 0 0 14px rgba(16,185,129,0.95); } }

/* ---- Status strip ---- */
.mm-strip { padding: 9px 15px; border-radius: 9px; font-size: 0.8rem; font-weight: 500; margin: 14px 0 18px 0; letter-spacing: 0.01em; }
.mm-strip-run { background: rgba(16,185,129,0.08); border: 1px solid rgba(16,185,129,0.28); color: #34d399; }
.mm-strip-stop { background: rgba(91,107,133,0.07); border: 1px solid rgba(91,107,133,0.22); color: #8294b0; }
.mm-strip-err { background: rgba(244,63,94,0.09); border: 1px solid rgba(244,63,94,0.3); color: #fb7185; }

/* ---- KPI cards (asymmetric: hero P&L + smaller satellites) ---- */
.mm-kpi-grid { display: grid; grid-template-columns: 1.9fr 1.05fr 0.85fr 0.85fr; gap: 14px; margin-bottom: 18px; }
@media (max-width: 900px) { .mm-kpi-grid { grid-template-columns: 1fr 1fr; } }
.mm-kpi {
    position: relative; background: linear-gradient(150deg, #0c1426, #0e1830);
    border: 1px solid #1d2c49; border-radius: 11px; padding: 16px 18px 15px 18px;
    overflow: hidden; transition: transform 0.16s ease, border-color 0.16s ease, box-shadow 0.16s ease;
}
.mm-kpi:hover { transform: translateY(-3px); border-color: #2f4straggle; border-color: #33507e; box-shadow: 0 10px 26px rgba(0,0,0,0.4); }
.mm-kpi::before { content: ""; position: absolute; top: 0; left: 0; right: 0; height: 3px; background: var(--accent, #33507e); }
.mm-kpi-hero { padding-top: 20px; padding-bottom: 20px; }
.mm-kpi__label { font-family: 'Space Grotesk', sans-serif; font-size: 0.62rem; font-weight: 600; letter-spacing: 0.16em; text-transform: uppercase; color: #6b7c97; }
.mm-kpi__value { font-family: 'JetBrains Mono', monospace; font-weight: 700; line-height: 1.02; margin-top: 8px; color: var(--val, #eef3fb); font-variant-numeric: tabular-nums; }
.mm-kpi-hero .mm-kpi__value { font-size: clamp(2.3rem, 4.4vw, 3.4rem); letter-spacing: -0.03em; }
.mm-kpi:not(.mm-kpi-hero) .mm-kpi__value { font-size: clamp(1.5rem, 2.4vw, 2rem); }
.mm-kpi__sub { font-family: 'JetBrains Mono', monospace; font-size: 0.68rem; color: #6b7c97; margin-top: 7px; }

/* ---- Body grid ---- */
.mm-body { display: grid; grid-template-columns: minmax(0, 1fr) 320px; gap: 16px; align-items: start; }
@media (max-width: 1000px) { .mm-body { grid-template-columns: 1fr; } }

/* ---- State rail ---- */
.mm-rail { background: linear-gradient(160deg, #0c1426, #0b1222); border: 1px solid #1d2c49; border-radius: 12px; padding: 16px 16px 18px 16px; }
.mm-rail__label { font-family: 'Space Grotesk', sans-serif; font-size: 0.6rem; font-weight: 600; letter-spacing: 0.16em; text-transform: uppercase; color: #6b7c97; margin: 13px 0 4px 0; }
.mm-rail__label:first-child { margin-top: 0; }
.mm-price { font-family: 'JetBrains Mono', monospace; font-size: 1.5rem; font-weight: 700; color: #eef3fb; }
.mm-caret { display: inline-block; width: 7px; color: #10b981; animation: mm-blink 1.1s steps(1) infinite; }
@keyframes mm-blink { 50% { opacity: 0; } }
.mm-trend { font-family: 'Space Grotesk', sans-serif; font-weight: 700; font-size: 1.05rem; letter-spacing: 0.04em; }
.mm-up { color: #34d399; } .mm-down { color: #fb7185; } .mm-flat { color: #8294b0; }
.mm-chips { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 6px; }
.mm-chip { font-family: 'JetBrains Mono', monospace; font-size: 0.66rem; font-weight: 600; padding: 3px 8px; border-radius: 6px; border: 1px solid #233452; background: #0e1830; }
.mm-stage { display: inline-block; font-family: 'Space Grotesk', sans-serif; font-weight: 600; font-size: 0.82rem; letter-spacing: 0.06em; padding: 3px 11px; border-radius: 20px; }
.mm-scorebar { height: 7px; border-radius: 5px; background: #16223c; overflow: hidden; margin-top: 7px; }
.mm-scorefill { height: 100%; border-radius: 5px; background: linear-gradient(90deg, #3884ff, #10b981); transition: width 0.4s ease; }
.mm-hb { font-family: 'JetBrains Mono', monospace; font-size: 0.7rem; color: #6b7c97; margin-top: 12px; }

/* ---- Ledger ---- */
.mm-ledger-head { font-family: 'Space Grotesk', sans-serif; font-size: 0.7rem; font-weight: 600; letter-spacing: 0.16em; text-transform: uppercase; color: #6b7c97; margin: 22px 0 10px 0; padding-bottom: 7px; border-bottom: 1px solid #1b2740; }
[data-testid="stDataFrame"] { border: 0; }
[data-testid="stButton"] button { border-radius: 8px; font-family: 'Space Grotesk', sans-serif; font-weight: 600; min-height: 2.6rem; letter-spacing: 0.05em; transition: filter 0.15s ease, transform 0.12s ease; }
[data-testid="stButton"] button:hover { filter: brightness(1.14); }
[data-testid="stButton"] button:active { transform: scale(0.985); }
#MainMenu, footer { visibility: hidden; }
</style>
""",
    unsafe_allow_html=True,
)


# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------
if "state_manager" not in st.session_state:
    st.session_state.state_manager = StateManager()
if "engine_thread" not in st.session_state:
    st.session_state.engine_thread = None
if "engine_loop" not in st.session_state:
    st.session_state.engine_loop = None
if "engine_instance" not in st.session_state:
    st.session_state.engine_instance = None

state: StateManager = st.session_state.state_manager


def _run_engine_in_thread(engine: TradingEngine, loop: asyncio.AbstractEventLoop):
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(engine.run())
    finally:
        # Drain pending tasks before closing the loop so websockets' keepalive /
        # listener tasks don't emit the harmless "Event loop is closed" noise.
        try:
            pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
            for t in pending:
                t.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        except Exception:
            pass
        try:
            loop.close()
        except Exception:
            pass


def start_bot(
    api_token, app_id, account_id, account_currency, account_type,
    real_execution_confirmed, initial_stake, max_steps, strategy_sensitivity,
    martingale_multiplier, symbol, symbol_display, duration_minutes,
):
    if st.session_state.engine_thread and st.session_state.engine_thread.is_alive():
        return
    state.reset_for_new_session(initial_stake)
    state.set_running(True)
    engine = TradingEngine(
        api_token=api_token, app_id=app_id, account_id=account_id,
        account_currency=account_currency, state=state,
        initial_stake=initial_stake, max_martingale_steps=max_steps,
        symbol=symbol, symbol_display=symbol_display,
        contract_duration=duration_minutes, contract_duration_unit="m",
        strategy_sensitivity=strategy_sensitivity, account_type=account_type,
        real_execution_confirmed=real_execution_confirmed,
        martingale_multiplier=martingale_multiplier,
    )
    st.session_state.engine_instance = engine
    loop = asyncio.new_event_loop()
    st.session_state.engine_loop = loop
    thread = threading.Thread(
        target=_run_engine_in_thread, args=(engine, loop), daemon=True, name="TradingEngineThread"
    )
    add_script_run_ctx(thread)
    thread.start()
    st.session_state.engine_thread = thread


def stop_bot():
    state.request_stop()
    state.set_status("Stop requested.")


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------
with st.sidebar:
    st.markdown(
        "<div style='font-family:Space Grotesk;font-size:0.74rem;color:#6b7c97;font-weight:600;"
        "letter-spacing:0.16em;text-transform:uppercase;margin-bottom:16px;'>Configuration</div>",
        unsafe_allow_html=True,
    )

    accounts = []
    account_load_error = ""
    if not DERIV_APP_ID or not configured_pat:
        account_load_error = "Add DERIV_APP_ID and DERIV_API_TOKEN to Streamlit Secrets."
    else:
        try:
            accounts = _load_accounts(DERIV_APP_ID, configured_pat)
        except DerivAPIError as exc:
            account_load_error = f"Deriv PAT check failed: {exc.message}"
        except Exception:
            account_load_error = "Deriv account check failed. Confirm App ID, PAT, and network."

    account_id = ""
    account_currency = "USD"
    selected_account = None
    selected_account_type = "UNKNOWN"
    real_execution_confirmed = False
    execution_mode = "UNCONFIGURED"

    if account_load_error:
        st.error(account_load_error)
    elif not accounts:
        st.warning("No active Options accounts found.")
    else:
        account_map = {a["account_id"]: a for a in accounts if a.get("account_id")}
        if not account_map:
            st.warning("No usable account IDs returned.")
        else:
            account_id = st.selectbox(
                "Account", options=list(account_map), disabled=state.is_running,
                format_func=lambda v: (
                    f"{normalize_account_type(account_map[v].get('account_type', 'unknown'))} | "
                    f"{v} | {account_map[v].get('currency', 'USD')} "
                    f"{float(account_map[v].get('balance', 0)):,.2f}"
                ),
            )
            selected_account = account_map[account_id]
            selected_account_type = normalize_account_type(selected_account.get("account_type", "unknown"))
            account_currency = str(selected_account.get("currency", "USD")).upper()
            st.success("Connected.")
            st.metric("Balance", f"{account_currency} {float(selected_account.get('balance', 0)):,.2f}")

    st.divider()
    st.markdown("<div style='font-size:0.7rem;color:#6b7c97;font-weight:600;letter-spacing:0.14em;text-transform:uppercase;margin-bottom:8px;'>Market</div>", unsafe_allow_html=True)
    market_options = list(AVAILABLE_MARKETS.keys())
    try:
        market_index = market_options.index(DEFAULT_MARKET_DISPLAY)
    except ValueError:
        market_index = 0
    market_display = st.selectbox("Market", options=market_options, index=market_index, disabled=state.is_running)
    selected_symbol = AVAILABLE_MARKETS[market_display]
    st.caption("UP = CALL, DOWN = PUT. If a market can't trade on this account, the banner shows Deriv's own reason.")

    st.divider()
    st.markdown("<div style='font-size:0.7rem;color:#6b7c97;font-weight:600;letter-spacing:0.14em;text-transform:uppercase;margin-bottom:8px;'>Contract</div>", unsafe_allow_html=True)
    default_duration = CONTRACT_DURATION if CONTRACT_DURATION_UNIT == "m" else 30
    duration_minutes = st.select_slider("Duration (minutes)", options=[5, 15, 30, 60], value=default_duration, disabled=state.is_running)
    st.caption(f"Max 10 trades/day. No barriers — direction only.")

    st.divider()
    st.markdown("<div style='font-size:0.7rem;color:#6b7c97;font-weight:600;letter-spacing:0.14em;text-transform:uppercase;margin-bottom:8px;'>Execution</div>", unsafe_allow_html=True)
    if selected_account:
        if selected_account_type == "DEMO":
            st.success("Demo account — orders use demo funds.")
        elif selected_account_type == "REAL":
            live_confirmation = st.text_input("Type LIVE to enable real-money orders", value="", max_chars=4, disabled=state.is_running)
            real_execution_confirmed = live_confirmation == "LIVE"
            if real_execution_confirmed:
                st.warning("Real-money execution armed.")
            else:
                st.warning("Real account — orders blocked until LIVE confirmed.")
        else:
            st.error("Unknown account type. Orders blocked.")
        execution_mode = resolve_execution_mode(selected_account_type, real_execution_confirmed)

    st.divider()
    st.markdown("<div style='font-size:0.7rem;color:#6b7c97;font-weight:600;letter-spacing:0.14em;text-transform:uppercase;margin-bottom:8px;'>Money Management</div>", unsafe_allow_html=True)
    initial_stake = st.number_input("Initial Stake", min_value=0.35, max_value=10000.0, value=float(DEFAULT_INITIAL_STAKE), step=0.5, format="%.2f", disabled=state.is_running)
    martingale_multiplier = st.slider("Martingale Multiplier", 1.5, 4.0, float(MARTINGALE_MULTIPLIER), step=0.1, format="%.1f", disabled=state.is_running)
    max_martingale_steps = st.slider("Max Martingale Steps", 1, 6, DEFAULT_MAX_MARTINGALE_STEPS, disabled=state.is_running)
    stakes = [initial_stake]
    for _ in range(1, max_martingale_steps):
        stakes.append(round(stakes[-1] * martingale_multiplier, 2))
    st.caption(f"Progression: {' '.join(f'{s:.2f}' for s in stakes)} · exposure {sum(stakes):.2f}")

    st.divider()
    st.markdown("<div style='font-size:0.7rem;color:#6b7c97;font-weight:600;letter-spacing:0.14em;text-transform:uppercase;margin-bottom:8px;'>Sensitivity</div>", unsafe_allow_html=True)
    strategy_sensitivity = st.select_slider("Entry", options=list(STRATEGY_SENSITIVITY_PRESETS.keys()), value=DEFAULT_STRATEGY_SENSITIVITY, disabled=state.is_running)
    preset = STRATEGY_SENSITIVITY_PRESETS[strategy_sensitivity]
    st.caption(f"Min score {preset['entry_score_threshold']}/{SCORE_MAX} · higher = fewer, cleaner setups")

    st.divider()
    col_start, col_stop = st.columns(2)
    with col_start:
        start_pressed = st.button("START", type="primary", use_container_width=True, disabled=state.is_running)
    with col_stop:
        stop_pressed = st.button("STOP", type="secondary", use_container_width=True, disabled=not state.is_running)

    if start_pressed:
        if not selected_account:
            st.error("Select a valid Deriv account first.")
        else:
            start_bot(
                api_token=configured_pat, app_id=DERIV_APP_ID, account_id=account_id,
                account_currency=account_currency, account_type=selected_account_type,
                real_execution_confirmed=real_execution_confirmed,
                initial_stake=initial_stake, max_steps=max_martingale_steps,
                strategy_sensitivity=strategy_sensitivity, martingale_multiplier=martingale_multiplier,
                symbol=selected_symbol, symbol_display=market_display, duration_minutes=duration_minutes,
            )
            st.rerun()
    if stop_pressed:
        stop_bot()
        st.rerun()

    st.divider()
    st.caption(f"**Market:** {market_display} ({selected_symbol})\n\n**Contract:** CALL/PUT · {duration_minutes}m\n\n**Trend:** 15m trigger · 30m + 1h confirm")


# ---------------------------------------------------------------------------
# Header (static)
# ---------------------------------------------------------------------------
active_ctx = state.get_execution_context()
if state.is_running:
    display_account_id = active_ctx.get("account_id", "")
    display_account_type = active_ctx.get("account_type", "UNKNOWN")
    display_currency = active_ctx.get("currency", "USD")
    display_mode = active_ctx.get("execution_mode", "UNCONFIGURED")
elif selected_account:
    display_account_id = account_id
    display_account_type = selected_account_type
    display_currency = account_currency
    display_mode = execution_mode
else:
    display_account_id = ""
    display_account_type = "UNKNOWN"
    display_currency = account_currency
    display_mode = "UNCONFIGURED"

mode_lines = {
    "DEMO": "DEMO EXECUTION", "REAL": "LIVE EXECUTION", "SIGNAL_ONLY": "SIGNAL PREVIEW",
    "BLOCKED": "EXECUTION BLOCKED", "UNCONFIGURED": "AWAITING ACCOUNT",
}
mode_line = mode_lines.get(display_mode, "EXECUTION BLOCKED")
acct_id_short = html.escape(display_account_id[:8]) if display_account_id else "—"

st.markdown(
    f"""
<div class="mm-header">
    <div>
        <div class="mm-logo">Momentum<span class="mm-dot">·</span>Master <span style="color:#6b7c97;font-weight:500;font-size:0.8rem;letter-spacing:0.1em;">TF</span></div>
        <div class="mm-sub">CANDLE TREND · {html.escape(market_display)} · {html.escape(selected_symbol)}</div>
    </div>
    <div class="mm-acct">
        <div class="mm-mode">{html.escape(mode_line)}</div>
        <div class="mm-id">{html.escape(display_account_type)} · {acct_id_short} · {html.escape(display_currency)}</div>
    </div>
</div>
""",
    unsafe_allow_html=True,
)


# ---------------------------------------------------------------------------
# Live fragments
# ---------------------------------------------------------------------------
@st.fragment(run_every=2.0)
def status_fragment():
    err = state.error_message
    msg = state.status_message
    if err:
        cls, dot = "mm-strip-err", ""
        text = html.escape(err)
    elif state.is_running:
        cls, dot = "mm-strip-run", '<span class="mm-dotlive mm-run"></span>'
        text = html.escape(msg)
    else:
        cls, dot = "mm-strip-stop", '<span class="mm-dotlive mm-stop"></span>'
        text = html.escape(msg)
    st.markdown(f'<div class="mm-strip {cls}">{dot}{text}</div>', unsafe_allow_html=True)


def _kpi(label, value, sub="", accent="#33507e", val_color="#eef3fb", hero=False):
    cls = "mm-kpi mm-kpi-hero" if hero else "mm-kpi"
    return (
        f'<div class="{cls}" style="--accent:{accent};--val:{val_color};">'
        f'<div class="mm-kpi__label">{html.escape(label)}</div>'
        f'<div class="mm-kpi__value">{html.escape(value)}</div>'
        f'<div class="mm-kpi__sub">{html.escape(sub)}</div></div>'
    )


@st.fragment(run_every=2.0)
def metrics_fragment():
    stats = state.get_performance_stats()
    ctx = state.get_execution_context()
    currency = ctx.get("currency", "USD")

    pnl = stats["total_pnl"]
    pnl_str = f"+{pnl:.2f}" if pnl > 0 else f"{pnl:.2f}"
    pnl_accent = "#10b981" if pnl > 0 else "#f43f5e" if pnl < 0 else "#33507e"
    pnl_val = "#34d399" if pnl > 0 else "#fb7185" if pnl < 0 else "#eef3fb"

    exp = stats["expectancy"]
    exp_str = f"+{exp:.2f}" if exp > 0 else f"{exp:.2f}"
    exp_accent = "#10b981" if exp > 0 else "#f43f5e" if exp < 0 else "#33507e"
    exp_val = "#34d399" if exp > 0 else "#fb7185" if exp < 0 else "#9fb0c9"

    wr = stats["win_rate"]
    wr_accent = "#10b981" if wr >= 55 else "#f43f5e" if wr < 45 and stats["total_trades"] > 0 else "#3884ff"

    mart = state.get_martingale_state()
    step = mart["step"]
    sub_trades = f"{stats['wins']}W / {stats['losses']}L"
    if step > 0:
        sub_trades += f" · step {step}"

    cards = (
        _kpi(f"NET P&L · {currency}", pnl_str, "session realised", pnl_accent, pnl_val, hero=True)
        + _kpi("EXPECTANCY / TRADE", exp_str, "edge per trade", exp_accent, exp_val)
        + _kpi("WIN RATE", f"{wr:.1f}%", f"{stats['total_trades']} closed", wr_accent)
        + _kpi("MARTINGALE", f"{mart['stake']:.2f}", sub_trades, "#3884ff")
    )
    st.markdown(f'<div class="mm-kpi-grid">{cards}</div>', unsafe_allow_html=True)


@st.fragment(run_every=5.0)
def chart_fragment():
    ticks = state.get_recent_ticks()
    if not ticks:
        st.info("Waiting for market data — candles populate the price path every 30s, ticks stream live when available.")
        return
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=list(range(len(ticks))), y=ticks, mode="lines",
        line=dict(color="#3884ff", width=1.6), name="Price",
        hovertemplate="idx %{x}<br>price %{y:.5f}<extra></extra>",
    ))
    fig.add_trace(go.Scatter(
        x=[len(ticks) - 1], y=[ticks[-1]], mode="markers",
        marker=dict(color="#10b981", size=9, symbol="circle", line=dict(color="#06281f", width=1)),
        hovertemplate="last %{y:.5f}<extra></extra>",
    ))
    fig.update_layout(
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="#0a1120",
        font=dict(color="#6b7c97", size=10, family="JetBrains Mono"),
        xaxis=dict(title="", gridcolor="#16223c", showgrid=True, zeroline=False, tickcolor="#16223c", showticklabels=False),
        yaxis=dict(title="", gridcolor="#16223c", showgrid=True, zeroline=False, tickcolor="#16223c"),
        margin=dict(l=8, r=8, t=8, b=8), height=300, showlegend=False,
    )
    st.plotly_chart(fig, use_container_width=True)


@st.fragment(run_every=2.0)
def state_panel_fragment():
    s = state.get_strategy_state()
    price = state.current_price

    trend = s.get("trend_direction") or "—"
    t_cls = "mm-up" if trend == "UP" else ("mm-down" if trend == "DOWN" else "mm-flat")
    t_arrow = "▲" if trend == "UP" else ("▼" if trend == "DOWN" else "—")

    tf_biases = s.get("mtf_tf_biases", {})
    chips = []
    for tf in ["5m", "15m", "30m", "1h"]:
        if tf in tf_biases:
            v = tf_biases[tf]
            c = "#34d399" if v == "UP" else ("#fb7185" if v == "DOWN" else "#8294b0")
            ic = "▲" if v == "UP" else ("▼" if v == "DOWN" else "·")
            chips.append(f'<span class="mm-chip" style="color:{c};border-color:{c}55;">{tf} {ic}</span>')
    chips_html = f'<div class="mm-chips">{"".join(chips)}</div>' if chips else ""

    stage = s.get("pattern_stage", "IDLE")
    stage_colors = {"IDLE": ("#5b6b85", "#10182b"), "TREND": ("#a78bfa", "#1a1430"),
                    "PULLBACK": ("#fbbf24", "#241c08"), "MOMENTUM": ("#3884ff", "#0c1730"),
                    "SIGNAL": ("#34d399", "#08241a")}
    sc, sbg = stage_colors.get(stage, ("#5b6b85", "#10182b"))

    score = int(s.get("last_signal_score", 0))
    pct = max(0, min(100, int(round(score / SCORE_MAX * 100))))
    entry_mode = s.get("last_entry_mode") or ""
    score_sub = f" / {SCORE_MAX}" + (f" · {entry_mode}" if entry_mode else "")

    hb = state.get_tick_heartbeat()
    total = hb.get("total_ticks_processed", 0)
    last_t = hb.get("last_tick_time")
    if last_t is not None:
        age = time.time() - last_t
        if age <= 35:
            hbc, hbl = "#34d399", "live"
        elif age <= 90:
            hbc, hbl = "#fbbf24", f"{age:.0f}s"
        else:
            hbc, hbl = "#fb7185", f"stale {age:.0f}s"
        hb_html = f'<span style="color:{hbc};">●</span> {total:,} pts · {hbl}'
    else:
        hb_html = '<span style="color:#fb7185;">●</span> no data yet'

    st.markdown(
        f"""
<div class="mm-rail">
    <div class="mm-rail__label">Price</div>
    <div class="mm-price">{price:.5f}<span class="mm-caret">▌</span></div>
    <div class="mm-rail__label">Trend</div>
    <div class="mm-trend {t_cls}">{t_arrow} {html.escape(str(trend))}</div>
    <div class="mm-rail__label">Timeframes</div>
    {chips_html}
    <div class="mm-rail__label">Stage</div>
    <span class="mm-stage" style="color:{sc};background:{sbg};border:1px solid {sc}55;">{html.escape(stage)}</span>
    <div class="mm-rail__label">Last setup score</div>
    <div style="font-family:'JetBrains Mono',monospace;font-weight:700;color:#eef3fb;">{score}{score_sub}</div>
    <div class="mm-scorebar"><div class="mm-scorefill" style="width:{pct}%;"></div></div>
    <div class="mm-hb">{hb_html}</div>
</div>
""",
        unsafe_allow_html=True,
    )


@st.fragment(run_every=10.0)
def ledger_fragment():
    st.markdown('<div class="mm-ledger-head">Trade Ledger</div>', unsafe_allow_html=True)
    history = state.get_trade_history()
    if not history:
        st.markdown(
            '<div style="color:#6b7c97;font-size:0.82rem;padding:18px 0;text-align:center;">'
            "No trades yet. The bot waits for 30m + 1h to agree, then a 15m candle to confirm.</div>",
            unsafe_allow_html=True,
        )
        return
    records = []
    for t in history[:50]:
        pnl_str = f"+{t.pnl:.2f}" if t.pnl > 0 else f"{t.pnl:.2f}" if t.pnl < 0 else "0.00"
        records.append({
            "Time": t.timestamp, "Dir": t.direction, "Stake": t.stake,
            "Entry": t.entry_price, "Status": t.status, "P&L": pnl_str, "Step": t.martingale_step,
        })
    df = pd.DataFrame(records)

    def st_status(v):
        return {"WON": "color:#34d399;font-weight:700;", "LOST": "color:#fb7185;font-weight:700;",
                "OPEN": "color:#fbbf24;font-weight:600;", "CANCELLED": "color:#6b7c97;"}.get(v, "")

    def st_pnl(v):
        try:
            n = float(str(v).replace("+", ""))
            return "color:#34d399;font-weight:700;" if n > 0 else ("color:#fb7185;font-weight:700;" if n < 0 else "")
        except Exception:
            return ""

    def st_dir(v):
        return "color:#34d399;" if v == "BUY" else ("color:#fb7185;" if v == "SELL" else "")

    styled = df.style.map(st_status, subset=["Status"]).map(st_pnl, subset=["P&L"]).map(st_dir, subset=["Dir"])
    st.dataframe(styled, use_container_width=True, height=320, hide_index=True)


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------
status_fragment()
metrics_fragment()

st.markdown('<div class="mm-body">', unsafe_allow_html=True)
st.markdown('</div>', unsafe_allow_html=True)

col_chart, col_rail = st.columns([3, 1], gap="medium")
with col_chart:
    chart_fragment()
    ledger_fragment()
with col_rail:
    state_panel_fragment()