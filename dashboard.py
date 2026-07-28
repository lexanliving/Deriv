"""
dashboard.py
MomentumMaster TF — fragment-driven Streamlit terminal.

Live regions refresh themselves via st.fragment (metrics 2s, chart 5s,
ledger 10s). The full script only re-runs on user interaction.
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
    MAX_TRADES_PER_DAY,
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


st.markdown(
    """
<style>
@import url('https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;600;700&family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@500;600&display=swap');

body, .stApp {
    background-color: #0b1120;
    background-image:
        radial-gradient(1100px 520px at 8% -10%, rgba(16, 185, 129, 0.06), transparent 62%),
        radial-gradient(950px 500px at 98% 112%, rgba(56, 132, 255, 0.055), transparent 62%);
    background-attachment: fixed;
    color: #cbd5e1;
    font-family: 'IBM Plex Sans', -apple-system, BlinkMacSystemFont, sans-serif;
}

[data-testid="stMainBlockContainer"] { max-width: 1500px; padding-top: 1.4rem; }
[data-testid="stSidebar"] { background-color: #0d1526; border-right: 1px solid #1e2d3d; }

/* Metric cards */
[data-testid="stMetric"] {
    background: linear-gradient(135deg, #0f1a2e, #12203a);
    border: 1px solid #22344d;
    border-radius: 10px;
    padding: 16px 20px;
    min-height: 92px;
    transition: transform 0.16s ease, border-color 0.16s ease, box-shadow 0.16s ease;
}
[data-testid="stMetric"]:hover {
    transform: translateY(-2px);
    border-color: #33507a;
    box-shadow: 0 6px 18px rgba(0,0,0,0.35);
}
[data-testid="stMetricValue"] {
    font-family: 'IBM Plex Mono', monospace !important;
    font-variant-numeric: tabular-nums;
    font-size: 2.05rem !important;
    font-weight: 600;
    letter-spacing: -0.02em;
}
[data-testid="stMetricLabel"] {
    font-size: 0.66rem !important;
    color: #7c8ba1;
    font-weight: 600;
    letter-spacing: 0.14em;
    text-transform: uppercase;
}

/* Header */
.terminal-header {
    display: flex; align-items: center; justify-content: space-between;
    padding: 0 0 12px 0; margin-bottom: 16px; border-bottom: 1px solid #1e2d3d;
}
.terminal-logo {
    font-family: 'Space Grotesk', sans-serif;
    font-size: 1.2rem; font-weight: 700; color: #e8eef7;
    letter-spacing: 0.16em; text-transform: uppercase;
}
.terminal-subtitle { font-size: 0.7rem; color: #7c8ba1; font-weight: 500; letter-spacing: 0.05em; margin-top: 2px; }

/* Status dot with live pulse */
.status-dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 6px; }
@keyframes pulse-live {
    0%, 100% { box-shadow: 0 0 5px rgba(16,185,129,0.45); }
    50%       { box-shadow: 0 0 14px rgba(16,185,129,0.9); }
}
.status-running { background: #10b981; animation: pulse-live 2.4s ease-in-out infinite; }
.status-stopped { background: #64748b; }
.status-error   { background: #f43f5e; box-shadow: 0 0 8px #f43f5e80; }

.status-bar { padding: 8px 14px; border-radius: 6px; font-size: 0.78rem; font-weight: 500; margin-bottom: 14px; letter-spacing: 0.02em; }
.status-bar-running { background: rgba(16,185,129,0.08); border: 1px solid rgba(16,185,129,0.25); color: #34d399; }
.status-bar-stopped { background: rgba(100,116,139,0.06); border: 1px solid rgba(100,116,139,0.2); color: #7c8ba1; }
.status-bar-error   { background: rgba(244,63,94,0.08); border: 1px solid rgba(244,63,94,0.25); color: #fb7185; }

/* State panel */
.state-panel { background: #0f1a2e; border: 1px solid #22344d; border-radius: 10px; padding: 16px; }
.state-label {
    font-size: 0.64rem; color: #7c8ba1; font-weight: 600; letter-spacing: 0.14em;
    text-transform: uppercase; margin-bottom: 4px; margin-top: 10px;
}
.state-value { font-family: 'IBM Plex Mono', monospace; font-variant-numeric: tabular-nums; font-size: 1.05rem; font-weight: 600; color: #e8eef7; }
.state-value-up { color: #34d399; }
.state-value-down { color: #fb7185; }
.state-value-neutral { color: #7c8ba1; }

.trade-ledger-header {
    font-family: 'Space Grotesk', sans-serif;
    font-size: 0.72rem; color: #7c8ba1; font-weight: 600; letter-spacing: 0.16em;
    text-transform: uppercase; margin-bottom: 10px; padding-bottom: 6px; border-bottom: 1px solid #1e2d3d;
}

[data-testid="stButton"] button {
    border-radius: 6px; font-family: 'Space Grotesk', sans-serif; font-weight: 600;
    min-height: 2.5rem; font-size: 0.82rem; letter-spacing: 0.06em;
    transition: filter 0.15s ease, transform 0.15s ease;
}
[data-testid="stButton"] button:hover { filter: brightness(1.15); }
[data-testid="stButton"] button:active { transform: scale(0.985); }

[data-testid="stDataFrame"] { border: 0; }
#MainMenu, footer { visibility: hidden; }
</style>
""",
    unsafe_allow_html=True,
)


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


# --- Sidebar ---------------------------------------------------------------
with st.sidebar:
    st.markdown(
        "<div style='font-family:Space Grotesk;font-size:0.75rem;color:#7c8ba1;font-weight:600;"
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
    st.markdown("<div style='font-size:0.72rem;color:#7c8ba1;font-weight:600;letter-spacing:0.12em;"
                "text-transform:uppercase;margin-bottom:8px;'>Market</div>", unsafe_allow_html=True)

    market_options = list(AVAILABLE_MARKETS.keys())
    try:
        market_index = market_options.index(DEFAULT_MARKET_DISPLAY)
    except ValueError:
        market_index = 0

    market_display = st.selectbox("Market", options=market_options, index=market_index, disabled=state.is_running)
    selected_symbol = AVAILABLE_MARKETS[market_display]

    use_custom_symbol = st.checkbox("Custom symbol", value=False, disabled=state.is_running)
    if use_custom_symbol:
        selected_symbol = st.text_input("Custom Deriv symbol", value=selected_symbol, disabled=state.is_running).strip().upper()

    symbol_display = f"{market_display} ({selected_symbol})" if not use_custom_symbol else selected_symbol

    default_duration = CONTRACT_DURATION if CONTRACT_DURATION_UNIT == "m" else 30
    duration_minutes = st.select_slider(
        "Contract duration (minutes)", options=[5, 15, 30, 60],
        value=default_duration, disabled=state.is_running,
    )
    st.caption(f"Contract type: CALL/PUT | Max {MAX_TRADES_PER_DAY} trades/day")

    st.divider()
    st.markdown("<div style='font-size:0.72rem;color:#7c8ba1;font-weight:600;letter-spacing:0.12em;"
                "text-transform:uppercase;margin-bottom:8px;'>Execution</div>", unsafe_allow_html=True)

    if selected_account:
        if selected_account_type == "DEMO":
            st.success("Demo account — orders use demo funds.")
        elif selected_account_type == "REAL":
            live_confirmation = st.text_input(
                "Type LIVE to enable real-money orders", value="", max_chars=4, disabled=state.is_running
            )
            real_execution_confirmed = live_confirmation == "LIVE"
            if real_execution_confirmed:
                st.warning("Real-money execution armed.")
            else:
                st.warning("Real account — orders blocked until LIVE confirmed.")
        else:
            st.error("Unknown account type. Orders blocked.")
        execution_mode = resolve_execution_mode(selected_account_type, real_execution_confirmed)

    st.divider()
    st.markdown("<div style='font-size:0.72rem;color:#7c8ba1;font-weight:600;letter-spacing:0.12em;"
                "text-transform:uppercase;margin-bottom:8px;'>Money Management</div>", unsafe_allow_html=True)

    initial_stake = st.number_input(
        "Initial Stake", min_value=0.35, max_value=10000.0,
        value=float(DEFAULT_INITIAL_STAKE), step=0.5, format="%.2f", disabled=state.is_running,
    )
    martingale_multiplier = st.slider(
        "Martingale Multiplier", 1.5, 4.0, float(MARTINGALE_MULTIPLIER),
        step=0.1, format="%.1f", disabled=state.is_running,
    )
    max_martingale_steps = st.slider(
        "Max Martingale Steps", 1, 6, DEFAULT_MAX_MARTINGALE_STEPS, disabled=state.is_running,
    )

    stakes = [initial_stake]
    for _ in range(1, max_martingale_steps):
        stakes.append(round(stakes[-1] * martingale_multiplier, 2))
    st.caption(f"Stake progression: {' '.join(f'{s:.2f}' for s in stakes)} | Total exposure: {sum(stakes):.2f}")

    st.divider()
    st.markdown("<div style='font-size:0.72rem;color:#7c8ba1;font-weight:600;letter-spacing:0.12em;"
                "text-transform:uppercase;margin-bottom:8px;'>Sensitivity</div>", unsafe_allow_html=True)

    strategy_sensitivity = st.select_slider(
        "Entry", options=list(STRATEGY_SENSITIVITY_PRESETS.keys()),
        value=DEFAULT_STRATEGY_SENSITIVITY, disabled=state.is_running,
    )
    preset = STRATEGY_SENSITIVITY_PRESETS[strategy_sensitivity]
    st.caption(f"Minimum score: {preset['entry_score_threshold']}/14")

    st.divider()
    col_start, col_stop = st.columns(2)
    with col_start:
        start_pressed = st.button("START", type="primary", use_container_width=True, disabled=state.is_running)
    with col_stop:
        stop_pressed = st.button("STOP", type="secondary", use_container_width=True, disabled=not state.is_running)

    if start_pressed:
        if not selected_account:
            st.error("Select a valid Deriv account first.")
        elif not selected_symbol:
            st.error("Select or enter a valid market symbol.")
        else:
            start_bot(
                api_token=configured_pat, app_id=DERIV_APP_ID, account_id=account_id,
                account_currency=account_currency, account_type=selected_account_type,
                real_execution_confirmed=real_execution_confirmed,
                initial_stake=initial_stake, max_steps=max_martingale_steps,
                strategy_sensitivity=strategy_sensitivity,
                martingale_multiplier=martingale_multiplier,
                symbol=selected_symbol, symbol_display=symbol_display,
                duration_minutes=duration_minutes,
            )
            st.rerun()

    if stop_pressed:
        stop_bot()
        st.rerun()

    st.divider()
    st.caption(
        f"**Market:** {symbol_display}\n\n"
        f"**Contract:** CALL/PUT | {duration_minutes}m\n\n"
        f"**Max trades/day:** {MAX_TRADES_PER_DAY}"
    )


# --- Header ----------------------------------------------------------------
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
status_dot_class = "running" if state.is_running else ("error" if state.error_message else "stopped")

st.markdown(
    f"""
<div class="terminal-header">
    <div>
        <div class="terminal-logo">MomentumMaster TF</div>
        <div class="terminal-subtitle">Multi-Market Candle Trend | {html.escape(symbol_display)}</div>
    </div>
    <div style="text-align:right;">
        <span class="status-dot status-{status_dot_class}"></span>
        <span style="color:#e8eef7;font-size:0.78rem;font-weight:600;">{mode_line}</span><br>
        <span style="color:#7c8ba1;font-size:0.68rem;">
            {html.escape(display_account_type)} | {html.escape(display_account_id[:8]) if display_account_id else "—"} | {html.escape(display_currency)}
        </span>
    </div>
</div>
""",
    unsafe_allow_html=True,
)


# --- Live fragments --------------------------------------------------------
@st.fragment(run_every=2.0)
def status_fragment():
    error_msg = state.error_message
    status_msg = state.status_message
    if error_msg:
        st.markdown(f'<div class="status-bar status-bar-error">{html.escape(error_msg)}</div>', unsafe_allow_html=True)
    elif state.is_running:
        st.markdown(
            f'<div class="status-bar status-bar-running"><span class="status-dot status-running"></span>'
            f"{html.escape(status_msg)}</div>", unsafe_allow_html=True,
        )
    else:
        st.markdown(
            f'<div class="status-bar status-bar-stopped"><span class="status-dot status-stopped"></span>'
            f"{html.escape(status_msg)}</div>", unsafe_allow_html=True,
        )


@st.fragment(run_every=2.0)
def metrics_fragment():
    stats = state.get_performance_stats()
    ctx = state.get_execution_context()
    currency = ctx.get("currency", "USD")

    col_pnl, col_exp, col_wr, col_tc = st.columns(4)

    with col_pnl:
        pnl = stats["total_pnl"]
        pnl_display = f"+{pnl:.2f}" if pnl > 0 else f"{pnl:.2f}" if pnl < 0 else "0.00"
        pnl_color = "#34d399" if pnl > 0 else "#fb7185" if pnl < 0 else "#e8eef7"
        st.metric(f"Net P&L ({currency})", pnl_display)
        st.markdown(
            f'<style>div[data-testid="stMetric"]:nth-of-type(1) div[data-testid="stMetricValue"]'
            f'{{ color: {pnl_color} !important; }}</style>', unsafe_allow_html=True,
        )

    with col_exp:
        exp = stats["expectancy"]
        exp_display = f"+{exp:.2f}" if exp > 0 else f"{exp:.2f}"
        exp_color = "#34d399" if exp > 0 else "#fb7185" if exp < 0 else "#7c8ba1"
        st.metric("Expectancy / trade", exp_display,
                  help="(win rate × avg win) − (loss rate × avg loss). The number that tells you if this actually works.")
        st.markdown(
            f'<style>div[data-testid="stMetric"]:nth-of-type(2) div[data-testid="stMetricValue"]'
            f'{{ color: {exp_color} !important; }}</style>', unsafe_allow_html=True,
        )

    with col_wr:
        st.metric("Win Rate", f"{stats['win_rate']:.1f}%")

    with col_tc:
        st.metric("Trades", f"{stats['total_trades']}")
        if stats["total_trades"] > 0:
            stake_label = f"{stats['current_stake']:.2f}"
            step_label = f"Step {stats['martingale_step']}" if stats["martingale_step"] > 0 else ""
            parts = [f"{stats['wins']}W / {stats['losses']}L", f"Stake: {stake_label} {currency}"]
            if step_label:
                parts.append(step_label)
            st.caption(" | ".join(parts))


@st.fragment(run_every=5.0)
def chart_fragment():
    ticks = state.get_recent_ticks()
    if not ticks:
        st.info("Waiting for market data...")
        return

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=list(range(len(ticks))), y=ticks, mode="lines",
        line=dict(color="#3884ff", width=1.5), name="Price",
        hovertemplate="Tick %{x}<br>Price: %{y:.5f}<extra></extra>",
    ))
    fig.add_trace(go.Scatter(
        x=[len(ticks) - 1], y=[ticks[-1]], mode="markers",
        marker=dict(color="#10b981", size=8, symbol="circle"),
        name=f"Current: {ticks[-1]:.5f}", hovertemplate="Current: %{y:.5f}<extra></extra>",
    ))
    fig.update_layout(
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="#0f1a2e",
        font=dict(color="#7c8ba1", size=10, family="IBM Plex Mono"),
        xaxis=dict(title="", gridcolor="#1e2d3d", showgrid=True, zeroline=False, tickcolor="#1e2d3d"),
        yaxis=dict(title="", gridcolor="#1e2d3d", showgrid=True, zeroline=False, tickcolor="#1e2d3d"),
        margin=dict(l=10, r=10, t=10, b=10), height=300, showlegend=False,
    )
    st.plotly_chart(fig, use_container_width=True)


@st.fragment(run_every=2.0)
def state_panel_fragment():
    strategy_state = state.get_strategy_state()
    st.markdown('<div class="state-panel">', unsafe_allow_html=True)

    st.markdown('<div class="state-label" style="margin-top:0;">Price</div>', unsafe_allow_html=True)
    current_price = state.current_price
    if current_price:
        st.markdown(f'<div class="state-value" style="font-size:1.35rem;">{current_price:.5f}</div>', unsafe_allow_html=True)

    trend = strategy_state.get("trend_direction") or "—"
    trend_cls = "state-value-up" if trend == "UP" else ("state-value-down" if trend == "DOWN" else "state-value-neutral")
    st.markdown(f'<div class="state-label">Trend</div><div class="state-value {trend_cls}">{trend}</div>', unsafe_allow_html=True)

    mtf = strategy_state.get("mtf_bias") or "—"
    mtf_agreement = strategy_state.get("mtf_agreement", 0)
    mtf_cls = "state-value-up" if mtf == "UP" else ("state-value-down" if mtf == "DOWN" else "state-value-neutral")
    st.markdown(
        f'<div class="state-label">MTF Bias</div>'
        f'<div class="state-value {mtf_cls}">{mtf} <span style="font-size:0.72rem;color:#7c8ba1;">({mtf_agreement})</span></div>',
        unsafe_allow_html=True,
    )

    tf_biases = strategy_state.get("mtf_tf_biases", {})
    if tf_biases:
        tf_parts = []
        for tf_key in ["5m", "15m", "30m", "1h"]:
            if tf_key in tf_biases:
                v = tf_biases[tf_key]
                c = "#34d399" if v == "UP" else ("#fb7185" if v == "DOWN" else "#fbbf24")
                icon = "▲" if v == "UP" else ("▼" if v == "DOWN" else "▶")
                tf_parts.append(
                    f'<span style="color:#7c8ba1;font-size:0.7rem;">{tf_key}</span>: '
                    f'<span style="color:{c};font-weight:600;font-size:0.75rem;">{icon} {v}</span>'
                )
        if tf_parts:
            st.markdown('<div style="margin-top:8px;display:flex;gap:10px;flex-wrap:wrap;">' + " | ".join(tf_parts) + "</div>", unsafe_allow_html=True)

    stage = strategy_state.get("pattern_stage", "IDLE")
    stage_colors = {"IDLE": "#7c8ba1", "TREND": "#a78bfa", "PULLBACK": "#fbbf24", "MOMENTUM": "#3884ff", "SIGNAL": "#34d399"}
    st.markdown(
        f'<div class="state-label">Stage</div>'
        f'<div style="color:{stage_colors.get(stage, "#7c8ba1")};font-weight:600;font-size:0.85rem;'
        f'font-family:Space Grotesk;letter-spacing:0.06em;">{stage}</div>',
        unsafe_allow_html=True,
    )

    heartbeat = state.get_tick_heartbeat()
    total_ticks = heartbeat.get("total_ticks_processed", 0)
    last_tick_time = heartbeat.get("last_tick_time")
    if last_tick_time is not None:
        age = time.time() - last_tick_time
        if age <= 35:
            hb_color, hb_label = "#34d399", "live"
        elif age <= 90:
            hb_color, hb_label = "#fbbf24", f"{age:.0f}s ago"
        else:
            hb_color, hb_label = "#fb7185", f"stale ({age:.0f}s ago)"
        st.markdown(
            f'<div style="margin-top:10px;font-size:0.72rem;color:#7c8ba1;">'
            f'<span style="color:{hb_color};">&#9679;</span> {total_ticks:,} updates &middot; {hb_label}</div>',
            unsafe_allow_html=True,
        )
    else:
        st.markdown('<div style="margin-top:10px;font-size:0.72rem;color:#fb7185;">&#9679; no market data received yet</div>', unsafe_allow_html=True)

    score = strategy_state.get("last_signal_score", 0)
    entry_mode = strategy_state.get("last_entry_mode")
    mode_suffix = f' <span style="font-size:0.66rem;color:#7c8ba1;">({entry_mode})</span>' if entry_mode else ""
    st.markdown(
        f'<div class="state-label">Last Score</div>'
        f'<div style="color:#e8eef7;font-weight:600;font-size:1rem;font-family:IBM Plex Mono;">{score}/14{mode_suffix}</div>',
        unsafe_allow_html=True,
    )

    stats = state.get_performance_stats()
    cooldown_remaining = stats.get("cooldown_remaining", 0)
    consecutive_losses = stats.get("consecutive_losses", 0)
    if cooldown_remaining > 0:
        st.markdown(f'<div style="color:#fbbf24;font-size:0.75rem;font-weight:600;margin-top:8px;">Cooldown: {int(cooldown_remaining)}s</div>', unsafe_allow_html=True)
    elif consecutive_losses > 0:
        st.markdown(f'<div style="color:#3884ff;font-size:0.75rem;font-weight:600;margin-top:8px;">Consecutive losses: {consecutive_losses} | Ready</div>', unsafe_allow_html=True)

    st.markdown("</div>", unsafe_allow_html=True)


@st.fragment(run_every=10.0)
def ledger_fragment():
    st.markdown('<div class="trade-ledger-header">Trade Ledger</div>', unsafe_allow_html=True)
    trade_history = state.get_trade_history()
    if not trade_history:
        st.markdown(
            '<div style="color:#7c8ba1;font-size:0.82rem;padding:20px 0;text-align:center;">'
            "No trades yet. Start the bot and wait for a qualifying candle setup.</div>",
            unsafe_allow_html=True,
        )
        return

    records = []
    for t in trade_history[:50]:
        pnl_str = f"+{t.pnl:.2f}" if t.pnl > 0 else f"{t.pnl:.2f}" if t.pnl < 0 else "0.00"
        records.append({
            "Time": t.timestamp, "Direction": t.direction, "Stake": t.stake,
            "Entry": t.entry_price, "Status": t.status, "P&L": pnl_str, "Step": t.martingale_step,
        })

    df = pd.DataFrame(records)

    def style_status(val):
        if val == "WON":
            return "color: #34d399; font-weight: 700;"
        if val == "LOST":
            return "color: #fb7185; font-weight: 700;"
        if val == "OPEN":
            return "color: #fbbf24; font-weight: 600;"
        if val == "CANCELLED":
            return "color: #7c8ba1;"
        return ""

    def style_pnl(val):
        try:
            num = float(str(val).replace("+", ""))
            if num > 0:
                return "color: #34d399; font-weight: 700;"
            if num < 0:
                return "color: #fb7185; font-weight: 700;"
        except Exception:
            pass
        return ""

    def style_direction(val):
        if val == "BUY":
            return "color: #34d399;"
        if val == "SELL":
            return "color: #fb7185;"
        return ""

    styled_df = (
        df.style.map(style_status, subset=["Status"])
        .map(style_pnl, subset=["P&L"])
        .map(style_direction, subset=["Direction"])
    )
    st.dataframe(styled_df, use_container_width=True, height=320, hide_index=True)


# --- Layout ----------------------------------------------------------------
status_fragment()

col_chart, col_state_panel = st.columns([4, 1])
with col_chart:
    metrics_fragment()
    chart_fragment()
with col_state_panel:
    state_panel_fragment()

ledger_fragment()