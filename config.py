"""
config.py
---------
Central configuration file for the Deriv Volatility 10 (1s) Trading Bot.
All constants, defaults, and environment-variable lookups are defined here.

The current Deriv PAT flow creates its account-specific WebSocket URL at
runtime, after an authenticated REST request. No fixed WebSocket URL belongs
in configuration.

v4: Ultra-High Quality mode — extremely strict thresholds designed to produce
only the most confident, high-probability signals (~10 quality trades/day).
"""

import os
try:
    from dotenv import load_dotenv
except ImportError:
    # Streamlit Cloud secrets work without python-dotenv. This fallback avoids
    # preventing the dashboard from starting while dependencies are installing.
    def load_dotenv() -> bool:
        return False

load_dotenv()

# ---------------------------------------------------------------------------
# Deriv API Connection Settings
# ---------------------------------------------------------------------------
# This application uses the current Deriv PAT flow.  Both values are required:
# DERIV_APP_ID is the PAT application ID; DERIV_API_TOKEN is the separate
# Personal Access Token created for that application.  No redirect URL or
# browser login is used.
def _streamlit_secret(name: str) -> str:
    """Use Streamlit Cloud secrets when no local environment variable exists."""
    try:
        import streamlit as st
        return str(st.secrets.get(name, ""))
    except Exception:
        return ""


DERIV_APP_ID = os.getenv("DERIV_APP_ID") or _streamlit_secret("DERIV_APP_ID") or ""
DERIV_API_TOKEN = os.getenv("DERIV_API_TOKEN") or _streamlit_secret("DERIV_API_TOKEN") or ""

# Compatibility only: older source files may import this name. The PAT client
# does not use it; it obtains an account-specific OTP WebSocket URL at runtime.
DERIV_WS_URL = ""

# ---------------------------------------------------------------------------
# Market / Symbol Settings
# ---------------------------------------------------------------------------
SYMBOL = "1HZ10V"          # Volatility 10 (1s) Index
SYMBOL_DISPLAY = "Volatility 10 (1s) Index"

# ---------------------------------------------------------------------------
# Strategy Parameters
# ---------------------------------------------------------------------------
# v4: EXTREME quality thresholds. We only want trades with explosive, 
# unmistakable momentum followed by a controlled shallow pullback.
# These values are deliberately much higher than standard to eliminate
# noise, whipsaw, and marginal setups. Only ~10 setups per day should pass.

TREND_WINDOW_MIN = 8           # Minimum ticks for classic trend identification
TREND_WINDOW_MAX = 12          # Maximum ticks for classic trend identification
VELOCITY_THRESHOLD = 0.55      # Minimum efficiency ratio for a classic-window trend (baseline)

# Fast "burst" path: catches strong, quick momentum (e.g. the move in your
# screenshot) without waiting out the full classic window. Checked first on
# every tick; falls back to the classic window if no burst qualifies.
BURST_WINDOW_MIN = 4             # Minimum ticks for a burst trend
BURST_WINDOW_MAX = 6             # Maximum ticks for a burst trend
BURST_VELOCITY_THRESHOLD = 0.72  # Higher bar than classic since the window is shorter/noisier

# ---- v4 STRICT STRATEGY FILTERS ----
# These override normal thresholds when the Ultra-High Quality mode is active.
# They ensure only explosive, undeniable momentum is traded.
STRICT_ER_THRESHOLD = 0.85       # Minimum ER for momentum detection (vs 0.55 baseline)
STRICT_REVERSAL_THRESHOLD = 0.382  # Max pullback depth = 38.2% Fibonacci
STRICT_CONTINUATION_TICKS = 3    # Required continuation ticks before signal (vs 2 baseline)
MIN_MOMENTUM_TICKS = 5           # Minimum momentum ticks before ARMED transition
MIN_PULLBACK_TICKS = 1           # Minimum pullback ticks before continuation considered

MAX_TRADES_PER_TREND = 1       # Maximum trades allowed per identified trend
TICK_BUFFER_SIZE = 50          # Number of recent ticks to keep in memory
MOMENTUM_CONFIRM_TICKS = 2     # Baseline continuation ticks (overridden by STRICT_CONTINUATION_TICKS)

# Cooldown between trades (in ticks). 60 ticks ≈ 60 seconds on 1s index.
# This prevents overtrading and ensures each signal is independent.
TRADE_COOLDOWN_TICKS = 60

# ---------------------------------------------------------------------------
# Contract / Trade Parameters
# ---------------------------------------------------------------------------
CONTRACT_TYPE_BUY = "ONETOUCH"
CONTRACT_TYPE_SELL = "ONETOUCH"
CONTRACT_DURATION = 5           # Duration in ticks
CONTRACT_DURATION_UNIT = "t"    # 't' = ticks
BARRIER_BUY = "+0.08"           # Barrier offset for Buy (Touch above)
BARRIER_SELL = "-0.08"          # Barrier offset for Sell (Touch below)
CURRENCY = "USD"

# ---------------------------------------------------------------------------
# Take Profit Settings
# ---------------------------------------------------------------------------
# Take Profit amount in account currency. When set to 0, TP is disabled.
# When the open P&L of the contract reaches this value, the bot will attempt
# to close the contract early via the Deriv contract_update API.
TAKE_PROFIT_ENABLED = False     # Master toggle for TP feature
TAKE_PROFIT_AMOUNT = 0.0        # Target profit amount (in account currency)
TP_POLL_INTERVAL_SECONDS = 1.0  # How often to check contract P&L for TP trigger

# ---------------------------------------------------------------------------
# Multi-Timeframe Confirmation Settings
# ---------------------------------------------------------------------------
# Granularity values in seconds (Deriv API supported values)
MTF_GRANULARITIES = {
    "5m": 300,
    "15m": 900,
    "30m": 1800,
}
MTF_CANDLE_COUNT = 10           # Number of candles to fetch per timeframe for trend analysis
MTF_MIN_AGREEMENT = 3           # v3: unanimous (3-of-3) alignment required for high-confidence trades

# ---------------------------------------------------------------------------
# Strategy Sensitivity Presets
# ---------------------------------------------------------------------------
# Lets the dashboard offer a simple Conservative/Balanced/Aggressive control
# without touching code. All presets now lean toward high-confidence setups.
STRATEGY_SENSITIVITY_PRESETS = {
    "Conservative": {
        "velocity_threshold": 0.80,
        "burst_threshold": 0.90,
        "mtf_min_agreement": 3,
    },
    "Balanced": {
        "velocity_threshold": 0.70,
        "burst_threshold": 0.85,
        "mtf_min_agreement": 3,
    },
    "Aggressive": {
        "velocity_threshold": 0.62,
        "burst_threshold": 0.75,
        "mtf_min_agreement": 3,
    },
}
DEFAULT_STRATEGY_SENSITIVITY = "Conservative"

# ---------------------------------------------------------------------------
# Martingale Settings
# ---------------------------------------------------------------------------
MARTINGALE_MULTIPLIER = 3.0     # User-configured stake multiplier on loss
DEFAULT_INITIAL_STAKE = 1.0     # Default initial stake in USD
DEFAULT_MAX_MARTINGALE_STEPS = 3  # Default max consecutive recovery steps

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_DIR = os.path.join(os.path.dirname(__file__), "logs")
LOG_FILE = os.path.join(LOG_DIR, "deriv_bot.log")
LOG_LEVEL = "INFO"
