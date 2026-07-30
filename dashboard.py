"""dashboard.py — MomentumMaster TF terminal (original professional theme, restored)."""
import asyncio
import html
import os
import sys
import threading
import time
from datetime import datetime, timezone

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from streamlit.runtime.scriptrunner import add_script_run_ctx

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import (
    AVAILABLE_MARKETS, CONTRACT_DURATION, CONTRACT_DURATION_UNIT,
    DEFAULT_ENTRY_TIMEFRAME, DEFAULT_INITIAL_STAKE, DEFAULT_MARKET_DISPLAY,
    DEFAULT_MAX_MARTINGALE_STEPS, DEFAULT_STRATEGY_SENSITIVITY, DERIV_APP_ID,
    DERIV_API_TOKEN, ENTRY_TIMEFRAME_BY_DURATION, MARTINGALE_MULTIPLIER, SCORE_MAX,
    STRATEGY_SENSITIVITY_PRESETS,
)
from src.api_client import DerivAPIClient, DerivAPIError
from src.journal import get_journal
from src.state_manager import StateManager
from src.trading_engine import TradingEngine, normalize_account_type, resolve_execution_mode

st.set_page_config(page_title="MomentumMaster TF", page_icon="⚡", layout="wide", initial_sidebar_state="expanded")

configured_pat = DERIV_API_TOKEN.strip()

_STAGE_LABEL = {"IDLE": "Scanning", "TREND": "Trend set", "SIGNAL": "Setup armed",
                "PULLBACK": "Pullback", "MOMENTUM": "Momentum"}
_MODE_LABEL = {"DEMO": "Demo", "REAL": "Live", "BLOCKED": "Paused",
               "UNCONFIGURED": "No account", "SIGNAL_ONLY": "Preview"}


@st.cache_data(ttl=60, show_spinner=False)
def _load_accounts(app_id: str, token: str):
    return asyncio.run(DerivAPIClient.get_accounts(token, app_id))


st.markdown(
    """
<style>
@import url('https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;600;700&family=IBM+Plex+Sans:wght@400;500;600&family=JetBrains+Mono:wght@500;600;700&display=swap');
html,body,.stApp{background-color:#060912;color:#c7d2e0;font-family:'IBM Plex Sans',-apple-system,BlinkMacSystemFont,sans-serif;}
.stApp{background-image:radial-gradient(900px 460px at 6% -8%, rgba(16,185,129,0.10), transparent 60%),radial-gradient(820px 440px at 100% 108%, rgba(56,132,255,0.09), transparent 60%),radial-gradient(rgba(120,150,190,0.05) 1px, transparent 1px);background-size:auto,auto,22px 22px;background-attachment:fixed;}
[data-testid="stMainBlockContainer"]{max-width:1480px;padding-top:1.3rem;}
[data-testid="stSidebar"]{background-color:#0a0f1c;border-right:1px solid #1b2740;}
[data-testid="stSidebarNav"]{padding-top:6px;margin-bottom:6px;}
[data-testid="stSidebarNav"] li button,[data-testid="stSidebarNav"] a{font-family:'Space Grotesk',sans-serif;letter-spacing:.04em;}
.mm-header{display:flex;align-items:flex-end;justify-content:space-between;padding:4px 2px 14px 2px;position:relative;}
.mm-header::after{content:"";position:absolute;left:0;right:0;bottom:0;height:2px;background:linear-gradient(90deg,#10b981,#3884ff 45%,transparent 92%);background-size:220% 100%;animation:mm-scan 6s linear infinite;border-radius:2px;}
@keyframes mm-scan{0%{background-position:120% 0;}100%{background-position:-120% 0;}}
.mm-logo{font-family:'Space Grotesk',sans-serif;font-weight:700;font-size:1.32rem;letter-spacing:0.18em;color:#eef3fb;text-transform:uppercase;}
.mm-logo .mm-dot{color:#10b981;}
.mm-eyebrow{font-family:'Space Grotesk',sans-serif;font-size:.58rem;font-weight:600;letter-spacing:.22em;color:#4f6080;text-transform:uppercase;margin-top:5px;}
.mm-sub{font-family:'JetBrains Mono',monospace;font-size:.72rem;color:#8294b0;letter-spacing:.03em;margin-top:2px;}
.mm-acct{text-align:right;font-family:'JetBrains Mono',monospace;}
.mm-acct .mm-mode{font-size:.86rem;font-weight:600;color:#eef3fb;letter-spacing:.06em;}
.mm-acct .mm-id{font-size:.68rem;color:#6b7c97;margin-top:2px;}
.mm-dotlive{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:7px;vertical-align:middle;}
.mm-run{background:#10b981;animation:mm-pulse 2.2s ease-in-out infinite;}
.mm-stop{background:#5b6b85;}
.mm-err{background:#f43f5e;box-shadow:0 0 9px #f43f5e99;}
@keyframes mm-pulse{0%,100%{box-shadow:0 0 4px rgba(16,185,129,0.5);}50%{box-shadow:0 0 14px rgba(16,185,129,0.95);}}
.mm-strip{padding:10px 16px;border-radius:9px;font-size:.82rem;font-weight:500;margin:14px 0 18px 0;letter-spacing:.01em;}
.mm-strip-run{background:rgba(16,185,129,0.08);border:1px solid rgba(16,185,129,0.28);color:#34d399;}
.mm-strip-stop{background:rgba(91,107,133,0.07);border:1px solid rgba(91,107,133,0.22);color:#8294b0;}
.mm-strip-err{background:rgba(244,63,94,0.09);border:1px solid rgba(244,63,94,0.3);color:#fb7185;}
.mm-kpi-grid{display:grid;grid-template-columns:1.9fr 1.05fr 0.85fr 0.85fr;gap:14px;margin-bottom:18px;}
@media(max-width:900px){.mm-kpi-grid{grid-template-columns:1fr 1fr;}}
.mm-kpi{position:relative;background:linear-gradient(150deg,#0c1426,#0e1830);border:1px solid #1d2c49;border-radius:11px;padding:16px 18px 15px 18px;overflow:hidden;transition:transform .16s ease,border-color .16s ease,box-shadow .16s ease;}
.mm-kpi:hover{transform:translateY(-3px);border-color:#33507e;box-shadow:0 10px 26px rgba(0,0,0,0.4);}
.mm-kpi::before{content:"";position:absolute;top:0;left:0;right:0;height:3px;background:var(--accent,#33507e);}
.mm-kpi-hero{padding-top:20px;padding-bottom:20px;}
.mm-kpi__label{font-family:'Space Grotesk',sans-serif;font-size:.62rem;font-weight:600;letter-spacing:.16em;text-transform:uppercase;color:#6b7c97;}
.mm-kpi__value{font-family:'JetBrains Mono',monospace;font-weight:700;line-height:1.02;margin-top:8px;color:var(--val,#eef3fb);font-variant-numeric:tabular-nums;}
.mm-kpi-hero .mm-kpi__value{font-size:clamp(2.3rem,4.4vw,3.4rem);letter-spacing:-0.03em;}
.mm-kpi:not(.mm-kpi-hero) .mm-kpi__value{font-size:clamp(1.5rem,2.4vw,2rem);}
.mm-kpi__sub{font-family:'JetBrains Mono',monospace;font-size:.68rem;color:#6b7c97;margin-top:7px;}
.mm-rail{background:linear-gradient(160deg,#0c1426,#0b1222);border:1px solid #1d2c49;border-radius:12px;padding:16px 16px 18px 16px;}
.mm-rail__label{font-family:'Space Grotesk',sans-serif;font-size:.6rem;font-weight:600;letter-spacing:.16em;text-transform:uppercase;color:#6b7c97;margin:13px 0 4px 0;}
.mm-rail__label:first-child{margin-top:0;}
.mm-price{font-family:'JetBrains Mono',monospace;font-size:1.5rem;font-weight:700;color:#eef3fb;}
.mm-caret{display:inline-block;width:7px;color:#10b981;animation:mm-blink 1.1s steps(1) infinite;}
@keyframes mm-blink{50%{opacity:0;}}
.mm-trend{font-family:'Space Grotesk',sans-serif;font-weight:700;font-size:1.05rem;letter-spacing:.04em;}
.mm-up{color:#34d399;} .mm-down{color:#fb7185;} .mm-flat{color:#8294b0;}
.mm-chips{display:flex;flex-wrap:wrap;gap:6px;margin-top:6px;}
.mm-chip{font-family:'JetBrains Mono',monospace;font-size:.66rem;font-weight:600;padding:3px 8px;border-radius:6px;border:1px solid #233452;background:#0e1830;}
.mm-stage{display:inline-block;font-family:'Space Grotesk',sans-serif;font-weight:600;font-size:.82rem;letter-spacing:.06em;padding:3px 11px;border-radius:20px;}
.mm-scorebar{height:7px;border-radius:5px;background:#16223c;overflow:hidden;margin-top:7px;}
.mm-scorefill{height:100%;border-radius:5px;background:linear-gradient(90deg,#3884ff,#10b981);transition:width .4s ease;}
.mm-hb{font-family:'JetBrains Mono',monospace;font-size:.7rem;color:#6b7c97;margin-top:12px;}
.mm-ledger-head{font-family:'Space Grotesk',sans-serif;font-size:.7rem;font-weight:600;letter-spacing:.16em;text-transform:uppercase;color:#6b7c97;margin:22px 0 10px 0;padding-bottom:7px;border-bottom:1px solid #1b2740;}
.pos{color:#4ade80;} .neg{color:#fb7185;}
.mm-glitch{margin:14px 0;padding:14px 16px;border-radius:11px;background:rgba(244,63,94,.07);border:1px solid rgba(244,63,94,.28);}
.mm-glitch .t{font-family:'Space Grotesk',sans-serif;font-weight:600;color:#fb7185;font-size:.84rem;letter-spacing:.04em;}
.mm-glitch .s{font-family:'JetBrains Mono',monospace;font-size:.7rem;color:#9fb0c9;margin-top:6px;line-height:1.5;}
[data-testid="stDataFrame"]{border:0;}
[data-testid="stButton"] button{border-radius:8px;font-family:'Space Grotesk',sans-serif;font-weight:600;min-height:2.6rem;letter-spacing:.05em;transition:filter .15s ease,transform .12s ease;}
[data-testid="stButton"] button:hover{filter:brightness(1.14);}
[data-testid="stButton"] button:active{transform:scale(0.985);}
#MainMenu,footer{visibility:hidden;}
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
if "should_run" not in st.session_state:
    st.session_state.should_run = False
if "engine_config" not in st.session_state:
    st.session_state.engine_config = None
if "auto_restart_count" not in st.session_state:
    st.session_state.auto_restart_count = 0
if "last_auto_restart" not in st.session_state:
    st.session_state.last_auto_restart = 0.0

state: StateManager = st.session_state.state_manager


def _glitch(where, exc):
    st.markdown(
        f'<div class="mm-glitch"><div class="t">⚠ {where} hit a snag</div>'
        f'<div class="s">The bot is fine — this is a rendering edge case, not a trading '
        f'fault. Details are in the expander; the controls stay live.</div></div>',
        unsafe_allow_html=True,
    )
    with st.expander("Technical details"):
        st.exception(exc)


def _run_engine_in_thread(engine: TradingEngine, loop: asyncio.AbstractEventLoop):
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(engine.run())
    finally:
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


RESTART_COOLDOWN_SECONDS = 20.0
MAX_AUTO_RESTARTS = 5
HEALTHY_WINDOW_SECONDS = 120.0


def _today_filled_trade_count(state_obj) -> int:
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    count = 0
    for t in state_obj.get_trade_history():
        if getattr(t, "contract_id", None) is None:
            continue
        if str(t.timestamp).startswith(today_str):
            count += 1
    return count


def _launch_engine(config, reset_state):
    try:
        if reset_state:
            state.reset_for_new_session(config["initial_stake"])
        else:
            state.clear_error()
            history = state.get_trade_history()
            if history and history[0].status == "OPEN":
                state.update_trade_outcome(
                    history[0].trade_id, "UNKNOWN", 0.0,
                    "The engine restarted while this contract was open — check the Deriv statement.")
        engine = TradingEngine(
            api_token=config["api_token"], app_id=config["app_id"],
            account_id=config["account_id"], account_currency=config["account_currency"],
            state=state, initial_stake=config["initial_stake"],
            max_martingale_steps=config["max_steps"], symbol=config["symbol"],
            symbol_display=config["symbol_display"], contract_duration=config["duration_minutes"],
            contract_duration_unit="m", strategy_sensitivity=config["strategy_sensitivity"],
            account_type=config["account_type"],
            real_execution_confirmed=config["real_execution_confirmed"],
            martingale_multiplier=config["martingale_multiplier"])
        if not reset_state:
            engine._daily_trade_count = _today_filled_trade_count(state)
            engine._daily_date = datetime.now(timezone.utc).date()
    except Exception as exc:
        state.set_running(False)
        state.set_error(f"The engine could not start: {exc}")
        state.set_status("Could not start.")
        return False
    st.session_state.engine_instance = engine
    state.set_running(True)
    loop = asyncio.new_event_loop()
    st.session_state.engine_loop = loop
    thread = threading.Thread(target=_run_engine_in_thread, args=(engine, loop), daemon=True, name="TradingEngineThread")
    try:
        add_script_run_ctx(thread)
    except Exception:
        pass
    thread.start()
    st.session_state.engine_thread = thread
    return True


@st.fragment(run_every=2.0)
def watchdog_fragment():
    if not st.session_state.get("should_run", False):
        return
    thread = st.session_state.get("engine_thread")
    now = time.monotonic()
    if thread is not None and thread.is_alive():
        last_restart = st.session_state.get("last_auto_restart", 0.0)
        if (st.session_state.get("auto_restart_count", 0) > 0
                and (now - last_restart) > HEALTHY_WINDOW_SECONDS):
            st.session_state.auto_restart_count = 0
        return
    last = st.session_state.get("last_auto_restart", 0.0)
    if now - last < RESTART_COOLDOWN_SECONDS:
        return
    count = st.session_state.get("auto_restart_count", 0)
    if count >= MAX_AUTO_RESTARTS:
        st.session_state.should_run = False
        state.set_running(False)
        state.set_error(f"The engine stopped {MAX_AUTO_RESTARTS} times in a row — auto-restart is paused. Press Start to try again.")
        state.set_status("Auto-restart paused.")
        st.rerun()
        return
    config = st.session_state.get("engine_config")
    if not config:
        st.session_state.should_run = False
        state.set_running(False)
        return
    st.session_state.last_auto_restart = now
    st.session_state.auto_restart_count = count + 1
    state.set_status(f"The engine stopped unexpectedly — restarting (attempt {count + 1}/{MAX_AUTO_RESTARTS})…")
    if not _launch_engine(config, reset_state=False):
        st.session_state.should_run = False


def start_bot(api_token, app_id, account_id, account_currency, account_type,
              real_execution_confirmed, initial_stake, max_steps, strategy_sensitivity,
              martingale_multiplier, symbol, symbol_display, duration_minutes):
    if st.session_state.engine_thread and st.session_state.engine_thread.is_alive():
        return
    config = {
        "api_token": api_token, "app_id": app_id, "account_id": account_id,
        "account_currency": account_currency, "account_type": account_type,
        "real_execution_confirmed": real_execution_confirmed,
        "initial_stake": initial_stake, "max_steps": max_steps,
        "strategy_sensitivity": strategy_sensitivity, "martingale_multiplier": martingale_multiplier,
        "symbol": symbol, "symbol_display": symbol_display, "duration_minutes": duration_minutes,
    }
    st.session_state.engine_config = config
    st.session_state.should_run = True
    st.session_state.auto_restart_count = 0
    st.session_state.last_auto_restart = 0.0
    if not _launch_engine(config, reset_state=True):
        st.session_state.should_run = False


def stop_bot():
    st.session_state.should_run = False
    state.request_stop()
    state.set_status("Stopping…")


with st.sidebar:
    st.markdown("<div style='font-family:Space Grotesk;font-size:0.74rem;color:#6b7c97;font-weight:600;letter-spacing:0.16em;text-transform:uppercase;margin-bottom:14px;'>Setup</div>", unsafe_allow_html=True)
    accounts = []
    account_load_error = ""
    if not DERIV_APP_ID or not configured_pat:
        account_load_error = "Add DERIV_APP_ID and DERIV_API_TOKEN to Streamlit Secrets."
    else:
        try:
            accounts = _load_accounts(DERIV_APP_ID, configured_pat)
        except DerivAPIError as exc:
            account_load_error = f"Could not verify the access token: {exc.message}"
        except Exception:
            account_load_error = "Account check failed — confirm the App ID, token, and connection."
    account_id = ""
    account_currency = "USD"
    selected_account = None
    selected_account_type = "UNKNOWN"
    real_execution_confirmed = False
    execution_mode = "UNCONFIGURED"
    if account_load_error:
        st.error(account_load_error)
    elif not accounts:
        st.warning("No active accounts were found.")
    else:
        account_map = {a["account_id"]: a for a in accounts if a.get("account_id")}
        if not account_map:
            st.warning("No usable account IDs were returned.")
        else:
            _acct_opts = list(account_map)
            _acct_val = st.session_state.get("dash_account_pick", _acct_opts[0])
            if _acct_val not in _acct_opts:
                _acct_val = _acct_opts[0]
            account_id = st.selectbox(
                "Account", options=_acct_opts, index=_acct_opts.index(_acct_val),
                key="dash_account_pick", disabled=state.is_running,
                format_func=lambda v: (
                    f"{normalize_account_type(account_map[v].get('account_type', 'unknown'))} · "
                    f"{v} · {account_map[v].get('currency', 'USD')} "
                    f"{float(account_map[v].get('balance', 0)):,.2f}"))
            selected_account = account_map.get(account_id)
            if selected_account:
                selected_account_type = normalize_account_type(selected_account.get("account_type", "unknown"))
                account_currency = str(selected_account.get("currency", "USD")).upper()
                st.success("Connected")
                st.metric("Balance", f"{account_currency} {float(selected_account.get('balance', 0)):,.2f}")
    st.divider()
    st.markdown("<div style='font-size:0.7rem;color:#6b7c97;font-weight:600;letter-spacing:0.14em;text-transform:uppercase;margin-bottom:8px;'>Market</div>", unsafe_allow_html=True)
    market_options = list(AVAILABLE_MARKETS.keys())
    try:
        market_index = market_options.index(DEFAULT_MARKET_DISPLAY)
    except ValueError:
        market_index = 0
    market_display = st.selectbox("Market", options=market_options, index=market_index, disabled=state.is_running)
    selected_symbol = AVAILABLE_MARKETS.get(market_display, next(iter(AVAILABLE_MARKETS.values()), ""))
    st.caption("Up = Call · Down = Put. Direction only — no barriers.")
    st.divider()
    st.markdown("<div style='font-size:0.7rem;color:#6b7c97;font-weight:600;letter-spacing:0.14em;text-transform:uppercase;margin-bottom:8px;'>Contract</div>", unsafe_allow_html=True)
    default_duration = CONTRACT_DURATION if CONTRACT_DURATION_UNIT == "m" else 30
    duration_minutes = st.select_slider("Length (minutes)", options=[1, 2, 5, 15, 30, 60], value=default_duration, disabled=state.is_running)
    entry_tf = ENTRY_TIMEFRAME_BY_DURATION.get(int(duration_minutes), DEFAULT_ENTRY_TIMEFRAME)
    st.caption("Up to 10 trades a day.")
    st.divider()
    st.markdown("<div style='font-size:0.7rem;color:#6b7c97;font-weight:600;letter-spacing:0.14em;text-transform:uppercase;margin-bottom:8px;'>Account safety</div>", unsafe_allow_html=True)
    if selected_account:
        if selected_account_type == "DEMO":
            st.success("Demo account — trades use virtual funds.")
        elif selected_account_type == "REAL":
            live_confirmation = st.text_input("Type LIVE to allow real orders", value="", max_chars=4, disabled=state.is_running)
            real_execution_confirmed = live_confirmation == "LIVE"
            if real_execution_confirmed:
                st.warning("Real-money trading is ON.")
            else:
                st.warning("Real account — orders stay blocked until you type LIVE.")
        else:
            st.error("Unknown account type — orders are blocked.")
        execution_mode = resolve_execution_mode(selected_account_type, real_execution_confirmed)
    st.divider()
    st.markdown("<div style='font-size:0.7rem;color:#6b7c97;font-weight:600;letter-spacing:0.14em;text-transform:uppercase;margin-bottom:8px;'>Stake</div>", unsafe_allow_html=True)
    initial_stake = st.number_input("Starting stake", min_value=0.35, max_value=10000.0, value=float(DEFAULT_INITIAL_STAKE), step=0.5, format="%.2f", disabled=state.is_running)
    martingale_multiplier = st.slider("Step multiplier", 1.5, 4.0, float(MARTINGALE_MULTIPLIER), step=0.1, format="%.1f", disabled=state.is_running)
    max_martingale_steps = st.slider("Maximum steps", 1, 6, DEFAULT_MAX_MARTINGALE_STEPS, disabled=state.is_running)
    stakes = [initial_stake]
    for _ in range(1, max_martingale_steps):
        stakes.append(round(stakes[-1] * martingale_multiplier, 2))
    ccy_tag = f" {account_currency}" if selected_account else ""
    st.caption(f"Steps: {' → '.join(f'{s:.2f}' for s in stakes)}  ·  max exposure {sum(stakes):.2f}{ccy_tag}")
    st.divider()
    st.markdown("<div style='font-size:0.7rem;color:#6b7c97;font-weight:600;letter-spacing:0.14em;text-transform:uppercase;margin-bottom:8px;'>Selectivity</div>", unsafe_allow_html=True)
    _sens_opts = list(STRATEGY_SENSITIVITY_PRESETS.keys()) or ["Conservative"]
    _sens_val = DEFAULT_STRATEGY_SENSITIVITY if DEFAULT_STRATEGY_SENSITIVITY in _sens_opts else _sens_opts[0]
    strategy_sensitivity = st.select_slider("How strict", options=_sens_opts, value=_sens_val, disabled=state.is_running)
    preset = STRATEGY_SENSITIVITY_PRESETS.get(strategy_sensitivity, {}) or {}
    if duration_minutes <= 2:
        regime_note = "scalp → 1m trigger; 5m must trend; power candles get the express lane"
    elif duration_minutes <= 15:
        regime_note = "short → 5m trigger; 5m must trend & decisive body"
    elif duration_minutes <= 30:
        regime_note = "medium → 15m trigger; 30m + 1h must agree"
    else:
        regime_note = "long → 15m trigger; 30m + 1h agree, 1h ADX & MACD confirm"
    st.caption(f"Needs {preset.get('entry_score_threshold', SCORE_MAX)}/{SCORE_MAX} · {entry_tf} ADX ≥ {preset.get('entry_adx_floor', 15)} · {regime_note}")
    st.divider()
    col_start, col_stop = st.columns(2)
    with col_start:
        start_pressed = st.button("Start", type="primary", use_container_width=True, disabled=state.is_running)
    with col_stop:
        stop_pressed = st.button("Stop", type="secondary", use_container_width=True, disabled=not state.is_running)
    if start_pressed:
        if not selected_account:
            st.error("Choose a Deriv account first.")
        else:
            start_bot(api_token=configured_pat, app_id=DERIV_APP_ID, account_id=account_id,
                      account_currency=account_currency, account_type=selected_account_type,
                      real_execution_confirmed=real_execution_confirmed, initial_stake=initial_stake,
                      max_steps=max_martingale_steps, strategy_sensitivity=strategy_sensitivity,
                      martingale_multiplier=martingale_multiplier, symbol=selected_symbol,
                      symbol_display=market_display, duration_minutes=duration_minutes)
            st.rerun()
    if stop_pressed:
        stop_bot()
        st.rerun()
    st.divider()
    st.caption(f"**Market** {market_display} · {selected_symbol}\n\n**Contract** Call / Put · {duration_minutes}m\n\n**Trend** {entry_tf} trigger, confirmed by 30m + 1h")

try:
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
    mode_line = _MODE_LABEL.get(display_mode, "Paused")
    acct_id_short = html.escape(display_account_id[:8]) if display_account_id else "—"
    st.markdown(
        f'<div class="mm-header"><div>'
        f'<div class="mm-logo">Momentum <span class="mm-dot">·</span>Master '
        f'<span style="color:#6b7c97;font-weight:500;font-size:0.8rem;letter-spacing:0.1em;">TF</span></div>'
        f'<div class="mm-eyebrow">Multi-timeframe trend system</div>'
        f'<div class="mm-sub">{html.escape(str(market_display))} · {html.escape(str(selected_symbol))}</div>'
        f'</div><div class="mm-acct"><div class="mm-mode">{html.escape(str(mode_line))}</div>'
        f'<div class="mm-id">{html.escape(str(display_account_type))} · {acct_id_short} · {html.escape(str(display_currency))}</div>'
        f'</div></div>',
        unsafe_allow_html=True,
    )
except Exception as _e:
    _glitch("Header", _e)


@st.fragment(run_every=2.0)
def status_fragment():
    try:
        err = state.error_message
        msg = state.status_message
        if err:
            cls, dot, text = "mm-strip-err", "", html.escape(err)
        elif state.is_running:
            cls, dot, text = "mm-strip-run", '<span class="mm-dotlive mm-run"></span>', html.escape(msg)
        else:
            cls, dot, text = "mm-strip-stop", '<span class="mm-dotlive mm-stop"></span>', html.escape(msg)
        st.markdown(f'<div class="mm-strip {cls}">{dot}{text}</div>', unsafe_allow_html=True)
    except Exception as _e:
        _glitch("Status", _e)


def _kpi(label, value, sub=" ", accent="#33507e", val_color="#eef3fb", hero=False):
    cls = "mm-kpi mm-kpi-hero" if hero else "mm-kpi"
    return (f'<div class="{cls}" style="--accent:{accent};--val:{val_color};">'
            f'<div class="mm-kpi__label">{html.escape(label)}</div>'
            f'<div class="mm-kpi__value">{html.escape(value)}</div>'
            f'<div class="mm-kpi__sub">{html.escape(sub)}</div></div>')


@st.fragment(run_every=2.0)
def metrics_fragment():
    try:
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
        sub_trades = f"{stats['wins']}W · {stats['losses']}L"
        if step > 0:
            sub_trades += f" · step {step}"
        cards = (_kpi("Net result", pnl_str, f"{currency} · this session", pnl_accent, pnl_val, hero=True)
                 + _kpi("Edge / trade", exp_str, "expected value", exp_accent, exp_val)
                 + _kpi("Win rate", f"{wr:.1f}%", f"{stats['total_trades']} closed", wr_accent)
                 + _kpi("Stake plan", f"{mart['stake']:.2f}", sub_trades, "#3884ff"))
        st.markdown(f'<div class="mm-kpi-grid">{cards}</div>', unsafe_allow_html=True)
    except Exception as _e:
        _glitch("Metrics", _e)


def _chart_layout(height):
    return dict(
        paper_bgcolor="#0a1120", plot_bgcolor="#0a1120",
        font=dict(color="#6b7c97", size=10, family="JetBrains Mono"),
        xaxis=dict(gridcolor="#16223c", tickcolor="#16223c", rangeslider_visible=False,
                   tickfont=dict(size=9), showspikes=True, spikecolor="#33507e", spikethickness=1),
        yaxis=dict(gridcolor="#16223c", tickcolor="#16223c", side="right",
                   tickfont=dict(size=9), tickformat=".5f"),
        margin=dict(l=8, r=8, t=8, b=8), height=height, showlegend=False, dragmode="pan",
    )


@st.fragment(run_every=5.0)
def chart_fragment():
    try:
        candles = state.get_candles_5m()
        if candles:
            dfc = candles[-120:]
            times = pd.to_datetime([c.get("epoch") for c in dfc], unit="s", utc=True)
            o = [float(c.get("open")) for c in dfc]
            h = [float(c.get("high")) for c in dfc]
            l = [float(c.get("low")) for c in dfc]
            cl = [float(c.get("close")) for c in dfc]
            last = cl[-1]
            first = cl[0]
            chg = last - first
            chg_pct = (chg / first * 100) if first else 0.0
            chg_cls = "pos" if chg >= 0 else "neg"
            st.markdown(
                f'<div style="display:flex;align-items:baseline;gap:10px;margin:0 0 6px 2px;flex-wrap:wrap">'
                f'<span style="font-family:Space Grotesk,sans-serif;font-size:.62rem;font-weight:600;'
                f'letter-spacing:.16em;text-transform:uppercase;color:#6b7c97">5m candles</span>'
                f'<span style="font-family:JetBrains Mono,monospace;font-weight:700;color:#eef3fb;font-size:1.05rem">{last:.5f}</span>'
                f'<span class="{chg_cls}" style="font-family:JetBrains Mono,monospace;font-size:.8rem;font-weight:600">'
                f'{chg:+.5f} ({chg_pct:+.2f}%)</span></div>',
                unsafe_allow_html=True,
            )
            fig = go.Figure()
            fig.add_trace(go.Scatter(x=times, y=cl, mode="lines",
                                     line=dict(color="rgba(127,176,255,0.35)", width=1),
                                     showlegend=False, hoverinfo="skip"))
            fig.add_trace(go.Candlestick(
                x=times, open=o, high=h, low=l, close=cl, name="",
                increasing_line_color="#34d399", increasing_fillcolor="#34d399",
                decreasing_line_color="#fb7185", decreasing_fillcolor="#fb7185", whiskerwidth=0.5,
                hovertemplate="%{x|%b %d %H:%M}<br>O %{open:.5f}  H %{high:.5f}<br>L %{low:.5f}  C %{close:.5f}<extra></extra>"))
            fig.add_trace(go.Scatter(
                x=[times.iloc[-1] if hasattr(times, "iloc") else times[-1]], y=[last], mode="markers",
                marker=dict(color="#10b981", size=10, line=dict(color="#06281f", width=2)),
                showlegend=False, hoverinfo="skip"))
            fig.add_hline(y=last, line=dict(color="#34d399", width=1, dash="dot"), opacity=0.45)
            fig.update_layout(**_chart_layout(360))
            st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False, "scrollZoom": True})
        else:
            ticks = state.get_recent_ticks()
            if not ticks:
                st.info("Waiting for market data — the 5m chart fills in within ~30s of connecting.")
                return
            fig = go.Figure()
            fig.add_trace(go.Scatter(x=list(range(len(ticks))), y=ticks, mode="lines",
                                     line=dict(color="#3884ff", width=1.6),
                                     hovertemplate="price %{y:.5f}<extra></extra>"))
            fig.add_trace(go.Scatter(x=[len(ticks) - 1], y=[ticks[-1]], mode="markers",
                                     marker=dict(color="#10b981", size=9, line=dict(color="#06281f", width=1)),
                                     hoverinfo="skip"))
            fig.update_layout(paper_bgcolor="#0a1120", plot_bgcolor="#0a1120",
                              font=dict(color="#6b7c97", size=10, family="JetBrains Mono"),
                              xaxis=dict(gridcolor="#16223c", showticklabels=False, zeroline=False),
                              yaxis=dict(gridcolor="#16223c", zeroline=False, side="right", tickformat=".5f"),
                              margin=dict(l=8, r=8, t=8, b=8), height=360, showlegend=False)
            st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})
    except Exception as _e:
        _glitch("5m chart", _e)


@st.fragment(run_every=2.0)
def state_panel_fragment():
    try:
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
        chips_html = f'<div class="mm-chips">{" ".join(chips)}</div>' if chips else ""
        stage = s.get("pattern_stage", "IDLE")
        stage_colors = {"IDLE": ("#5b6b85", "#10182b"), "TREND": ("#a78bfa", "#1a1430"),
                        "PULLBACK": ("#fbbf24", "#241c08"), "MOMENTUM": ("#3884ff", "#0c1730"),
                        "SIGNAL": ("#34d399", "#08241a")}
        sc, sbg = stage_colors.get(stage, ("#5b6b85", "#10182b"))
        stage_label = _STAGE_LABEL.get(stage, stage)
        score = int(s.get("last_signal_score", 0))
        pct = max(0, min(100, int(round(score / SCORE_MAX * 100))))
        hb = state.get_tick_heartbeat()
        total = hb.get("total_ticks_processed", 0)
        last_t = hb.get("last_tick_time")
        if last_t is not None:
            age = time.time() - last_t
            if age <= 35:
                hb_html = f'<span style="color:#34d399;">●</span> live · {total:,} ticks'
            elif age <= 90:
                hb_html = f'<span style="color:#fbbf24;">●</span> {age:.0f}s ago · {total:,} ticks'
            else:
                hb_html = f'<span style="color:#fb7185;">●</span> waiting · {age:.0f}s'
        else:
            hb_html = '<span style="color:#fb7185;">●</span> awaiting first tick'
        rail = (
            '<div class="mm-rail">'
            '<div class="mm-rail__label">Price</div>'
            f'<div class="mm-price">{price:.5f} <span class="mm-caret">▌</span></div>'
            '<div class="mm-rail__label">Trend</div>'
            f'<div class="mm-trend {t_cls}">{t_arrow} {html.escape(str(trend))}</div>'
            '<div class="mm-rail__label">Timeframes</div>'
            f'{chips_html}'
            '<div class="mm-rail__label">Status</div>'
            f'<span class="mm-stage" style="color:{sc};background:{sbg};border:1px solid {sc}55;">{html.escape(str(stage_label))}</span>'
            '<div class="mm-rail__label">Setup score</div>'
            f'<div style="font-family:\'JetBrains Mono\',monospace;font-weight:700;color:#eef3fb;">{score} / {SCORE_MAX}</div>'
            '<div class="mm-scorebar">'
            f'<div class="mm-scorefill" style="width:{pct}%;"></div></div>'
            f'<div class="mm-hb">{hb_html}</div>'
            '</div>'
        )
        st.markdown(rail, unsafe_allow_html=True)
    except Exception as _e:
        _glitch("Status rail", _e)


@st.fragment(run_every=10.0)
def ledger_fragment():
    try:
        st.markdown('<div class="mm-ledger-head">Trades</div>', unsafe_allow_html=True)
        history = state.get_trade_history()
        if not history:
            st.markdown(
                '<div style="color:#6b7c97;font-size:0.82rem;padding:18px 0;text-align:center;">'
                "No trades yet. MomentumMaster only acts when the trend agrees for your "
                "contract length and the trigger candle confirms it.</div>",
                unsafe_allow_html=True,
            )
            return
        records = []
        for t in history[:50]:
            pnl_str = f"+{t.pnl:.2f}" if t.pnl > 0 else f"{t.pnl:.2f}" if t.pnl < 0 else "0.00"
            records.append({"Time": t.timestamp, "Side": t.direction, "Stake": t.stake,
                            "Entry": t.entry_price, "Result": t.status, "P&L": pnl_str,
                            "Step": t.martingale_step})
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

        styled = (df.style.map(st_status, subset=["Result"]).map(st_pnl, subset=["P&L"]).map(st_dir, subset=["Side"]))
        st.dataframe(styled, use_container_width=True, height=320, hide_index=True)
    except Exception as _e:
        _glitch("Trades ledger", _e)


@st.fragment(run_every=15.0)
def journal_fragment():
    try:
        journal = get_journal()
        st.markdown('<div class="mm-ledger-head">Decision log · every trigger-candle review</div>', unsafe_allow_html=True)
        csv_bytes = journal.to_csv_bytes()
        if csv_bytes:
            st.download_button("Download decision log (CSV)", data=csv_bytes,
                               file_name="momentummaster_journal.csv", mime="text/csv",
                               use_container_width=True)
        rows = journal.read_rows()
        if not rows:
            st.caption("Every trigger-candle review is recorded here — whether it traded or stood aside — along with the result and the reason.")
            return
        df = pd.DataFrame(rows).tail(40).iloc[::-1]
        cols = ["timestamp_utc", "symbol", "direction", "trend", "taken", "score", "threshold",
                "rejection_reason", "note", "outcome", "pnl"]
        cols = [c for c in cols if c in df.columns]
        st.dataframe(df[cols], use_container_width=True, height=300, hide_index=True)
    except Exception as _e:
        _glitch("Decision log", _e)


watchdog_fragment()
status_fragment()
metrics_fragment()
col_chart, col_rail = st.columns([3, 1], gap="medium")
with col_chart:
    chart_fragment()
    ledger_fragment()
    journal_fragment()
with col_rail:
    state_panel_fragment()
