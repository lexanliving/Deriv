"""
config.py
Central configuration for MomentumMaster.

This file intentionally defines EVERY constant that any version of the
strategy, trading engine, or dashboard may import, so a partial/older copy
of this file can never cause an ImportError at startup.
"""

import os

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv() -> bool:
        return False

load_dotenv()


def _streamlit_secret(name: str) -> str:
    try:
        import streamlit as st
        return str(st.secrets.get(name, ""))
    except Exception:
        return ""


# --- Deriv credentials -----------------------------------------------------
DERIV_APP_ID = os.getenv("DERIV_APP_ID") or _streamlit_secret("DERIV_APP_ID") or ""
DERIV_API_TOKEN = os.getenv("DERIV_API_TOKEN") or _streamlit_secret("DERIV_API_TOKEN") or ""
DERIV_WS_URL = ""  # compatibility only; the OTP WebSocket URL is fetched at runtime


# --- Markets ---------------------------------------------------------------
# Synthetic 1s indices: available on every Deriv account and what the tick
# engine + ONETOUCH barriers are calibrated for. Symbols are case-sensitive.
# (Gold/forex need the candle CALL/PUT build and its own config.)
AVAILABLE_MARKETS = {
    "Volatility 10 (1s)": "1HZ10V",
    "Volatility 25 (1s)": "1HZ25V",
    "Volatility 50 (1s)": "1HZ50V",
    "Volatility 75 (1s)": "1HZ75V",
    "Volatility 100 (1s)": "1HZ100V",
}

DEFAULT_MARKET_DISPLAY = "Volatility 10 (1s)"
SYMBOL = AVAILABLE_MARKETS[DEFAULT_MARKET_DISPLAY]
SYMBOL_DISPLAY = DEFAULT_MARKET_DISPLAY


# --- Tick strategy parameters ---------------------------------------------
TREND_WINDOW_MIN = 8
TREND_WINDOW_MAX = 12
VELOCITY_THRESHOLD = 0.55
BURST_WINDOW_MIN = 4
BURST_WINDOW_MAX = 6
BURST_VELOCITY_THRESHOLD = 0.72
MAX_TRADES_PER_TREND = 1
TICK_BUFFER_SIZE = 50
MOMENTUM_CONFIRM_TICKS = 1
EARLY_TREND_MAX_AGE = 10
MICRO_BIAS_WINDOW = 45

# --- Regime filters (disabled by default; None/0 = off) -------------------
VOLATILITY_WINDOW = 20
MIN_TICK_VOLATILITY = None
MAX_TICK_VOLATILITY = None
ACCELERATION_MIN_RATIO = 0


# --- Scoring ---------------------------------------------------------------
ENTRY_SCORE_THRESHOLD = 8
SCORE_MAX = 14


# --- Contract --------------------------------------------------------------
CONTRACT_TYPE_BUY = "ONETOUCH"
CONTRACT_TYPE_SELL = "ONETOUCH"
CONTRACT_DURATION = 5
CONTRACT_DURATION_UNIT = "t"
BARRIER_BUY = "+0.08"
BARRIER_SELL = "-0.08"
CURRENCY = "USD"


# --- Multi-timeframe (tick build) -----------------------------------------
MTF_GRANULARITIES = {"5m": 300, "15m": 900, "30m": 1800}
MTF_CANDLE_COUNT = 10
MTF_MIN_AGREEMENT = 2


# --- Candle-trend build constants (harmless extras; used only if you swap
#     in the candle strategy/engine later) ---------------------------------
CANDLE_GRANULARITIES = {"5m": 300, "15m": 900, "30m": 1800, "1h": 3600}
CANDLE_LOOKBACK = 80
CANDLE_REFRESH_SECONDS = 30
ENTRY_TIMEFRAME = "15m"
TREND_TIMEFRAMES = ["30m", "1h"]
MAX_TRADES_PER_DAY = 10


# --- Sensitivity presets (keys MUST have no trailing spaces) --------------
STRATEGY_SENSITIVITY_PRESETS = {
    "Conservative": {
        "velocity_threshold": 0.68,
        "burst_threshold": 0.82,
        "mtf_min_agreement": 3,
        "entry_score_threshold": 10,
    },
    "Balanced": {
        "velocity_threshold": VELOCITY_THRESHOLD,
        "burst_threshold": BURST_VELOCITY_THRESHOLD,
        "mtf_min_agreement": MTF_MIN_AGREEMENT,
        "entry_score_threshold": ENTRY_SCORE_THRESHOLD,
    },
    "Aggressive": {
        "velocity_threshold": 0.50,
        "burst_threshold": 0.68,
        "mtf_min_agreement": 2,
        "entry_score_threshold": 6,
    },
}

DEFAULT_STRATEGY_SENSITIVITY = "Balanced"


# --- Stake / martingale ----------------------------------------------------
STAKE_MODE = "MARTINGALE"
MARTINGALE_MULTIPLIER = 2.5
DEFAULT_INITIAL_STAKE = 1.0
DEFAULT_MAX_MARTINGALE_STEPS = 3


# --- Logging ---------------------------------------------------------------
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
LOG_FILE = os.path.join(LOG_DIR, "deriv_bot.log")
LOG_LEVEL = "INFO"