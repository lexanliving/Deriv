"""
config.py
---------
Central configuration file for the Deriv Volatility 10 (1s) Trading Bot.
All constants, defaults, and environment-variable lookups are defined here.

The current Deriv PAT flow creates its account-specific WebSocket URL at
runtime, after an authenticated REST request. No fixed WebSocket URL belongs
in configuration.
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
# v2: trend quality is now measured with Kaufman's Efficiency Ratio (ER) -
# net displacement over total path length - instead of counting what
# fraction of individual ticks moved in the trend direction. ER tolerates
# occasional counter-ticks as long as net progress is strong, which is why
# these thresholds (0.55 / 0.72) look different from the old 0.70 tick-count
# ratio; they are not directly comparable numbers. See src/strategy.py for
# the full rationale.
TREND_WINDOW_MIN = 8           # Minimum ticks for classic trend identification
TREND_WINDOW_MAX = 12          # Maximum ticks for classic trend identification
VELOCITY_THRESHOLD = 0.55      # Minimum efficiency ratio for a classic-window trend

# Fast "burst" path: catches strong, quick momentum (e.g. the move in your
# screenshot) without waiting out the full classic window. Checked first on
# every tick; falls back to the classic window if no burst qualifies.
BURST_WINDOW_MIN = 4             # Minimum ticks for a burst trend
BURST_WINDOW_MAX = 6             # Maximum ticks for a burst trend
BURST_VELOCITY_THRESHOLD = 0.72  # Higher bar than classic since the window is shorter/noisier

MAX_TRADES_PER_TREND = 1       # Maximum trades allowed per identified trend
TICK_BUFFER_SIZE = 50          # Number of recent ticks to keep in memory
MOMENTUM_CONFIRM_TICKS = 2     # Continuation ticks after a pullback to enter

# ---------------------------------------------------------------------------
# Regime filters (v3): distinguish a genuine push from noise
# ---------------------------------------------------------------------------
# Rolling stdev of tick-to-tick deltas, computed over the last VOLATILITY_WINDOW
# ticks. MIN/MAX are left disabled (None) by default - hand-picking numbers
# without backtesting against your own tick history would just be a different
# flavor of overfitting. Calibrate these from a replay of historical ticks
# (see the strategy-improvement writeup) before enabling in live/demo trading.
VOLATILITY_WINDOW = 20
MIN_TICK_VOLATILITY = None     # e.g. 0.005 - reject entries in a near-flat market
MAX_TICK_VOLATILITY = None     # e.g. 0.05  - reject entries in an erratic/spiky market
# NOTE (July 2026 fix): StrategyEngine's tick buffer used to be capped below
# VOLATILITY_WINDOW, so this check silently never fired at any threshold.
# That's now fixed (see src/strategy.py, buffer_size). Backtesting after the
# fix still didn't show a real win-rate/profit-factor improvement from
# enabling this filter on simulated data (it just reduced trade frequency
# for the same quality), so it stays off by default; worth re-testing once
# real historical ticks are available.

# Require the most recent window's velocity (net move / tick) to be at least
# this fraction of the prior equal-length window's velocity, same direction.
# 0 disables the check entirely (a decaying move can still trigger, as today).
# A value like 0.8 requires the move to be holding pace or accelerating.
ACCELERATION_MIN_RATIO = 0.0

# ---------------------------------------------------------------------------
# Contract / Trade Parameters
# ---------------------------------------------------------------------------
CONTRACT_TYPE_BUY = "ONETOUCH"
CONTRACT_TYPE_SELL = "ONETOUCH"
CONTRACT_DURATION = 5           # Duration in ticks
CONTRACT_DURATION_UNIT = "t"    # 't' = ticks
BARRIER_BUY = "+0.08"           # Static fallback barrier for Buy (used when
                                # dynamic barrier is disabled, or when there
                                # isn't yet enough tick history to compute one)
BARRIER_SELL = "-0.08"          # Static fallback barrier for Sell
CURRENCY = "USD"

# ---------------------------------------------------------------------------
# Dynamic (volatility-scaled) barrier (v4)
# ---------------------------------------------------------------------------
# Simulated backtesting (see strategy-improvement report, July 2026) found
# the fixed +/-0.08 barrier poorly matched to this instrument's typical
# 5-tick move. Sizing the barrier to the recent realized tick-to-tick move
# instead - same entry logic, same duration, nothing else changed - roughly
# doubled simulated win rate (14.8%->30.2% in-sample, 14.1%->27.7%
# out-of-sample) and profit factor (0.15->0.37 / 0.14->0.33), independently
# verified in- and out-of-sample. It was the only tested change that held up
# out-of-sample, so it's enabled by default here.
#
# That backtest used simulated tick data (no real historical ticks were
# available at the time), so validate on a demo account against real
# Volatility 10 (1s) ticks before trusting this with real funds.
DYNAMIC_BARRIER_ENABLED = True
DYNAMIC_BARRIER_WINDOW = 10        # ticks used to estimate the recent typical move
DYNAMIC_BARRIER_MULTIPLIER = 2.2   # barrier = multiplier * average |tick-to-tick move|
DYNAMIC_BARRIER_FLOOR = 0.05       # never quote a barrier tighter than this
DYNAMIC_BARRIER_CEIL = 0.14        # never quote a barrier wider than this

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
MTF_MIN_AGREEMENT = 2           # v2: majority (2-of-3) instead of unanimous (3-of-3) alignment

# ---------------------------------------------------------------------------
# Strategy Sensitivity Presets
# ---------------------------------------------------------------------------
# Lets the dashboard offer a simple Conservative/Balanced/Aggressive control
# without touching code. "Balanced" reproduces the defaults above.
STRATEGY_SENSITIVITY_PRESETS = {
    "Conservative": {
        "velocity_threshold": 0.62,
        "burst_threshold": 0.80,
        "mtf_min_agreement": 3,
    },
    "Balanced": {
        "velocity_threshold": VELOCITY_THRESHOLD,
        "burst_threshold": BURST_VELOCITY_THRESHOLD,
        "mtf_min_agreement": MTF_MIN_AGREEMENT,
    },
    "Aggressive": {
        "velocity_threshold": 0.48,
        "burst_threshold": 0.65,
        "mtf_min_agreement": 2,
    },
}
DEFAULT_STRATEGY_SENSITIVITY = "Balanced"

# ---------------------------------------------------------------------------
# Stake / Risk Management Settings
# ---------------------------------------------------------------------------
# Martingale does not improve edge - it cannot turn a losing/breakeven
# strategy into a winning one. It only changes the shape of risk: mostly
# small wins, occasionally a large loss. With MARTINGALE_MULTIPLIER=3.0 over
# 3 steps, a single losing streak costs 1+3+9=13x base stake to "recover".
# FLAT is the default and recommended primary method; martingale is opt-in.
STAKE_MODE = "FLAT"             # "FLAT" (recommended default) or "MARTINGALE"
MARTINGALE_MULTIPLIER = 3.0     # Only used when STAKE_MODE == "MARTINGALE"
DEFAULT_INITIAL_STAKE = 1.0     # Default initial stake in USD
DEFAULT_MAX_MARTINGALE_STEPS = 3  # Default max consecutive recovery steps

# Session-level circuit breaker, independent of stake mode. Once cumulative
# session P&L falls to -SESSION_MAX_DRAWDOWN, the engine stops entering new
# trades (existing open contracts still settle normally). None disables it -
# but running live/real without some drawdown limit is not recommended.
SESSION_MAX_DRAWDOWN = None     # e.g. 20.0 (USD)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_DIR = os.path.join(os.path.dirname(__file__), "logs")
LOG_FILE = os.path.join(LOG_DIR, "deriv_bot.log")
LOG_LEVEL = "INFO"
