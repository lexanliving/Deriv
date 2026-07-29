"""
config.py
MomentumMaster TF — multi-market candle-trend configuration.

Trades UP/DOWN as CALL/PUT on a selectable market (Gold by default).
No barriers: rise/fall contracts do not use them.
Every name imported by any other module is defined here.
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
# Gold first: intended default and the most widely available. frx* = forex /
# commodities (CALL/PUT). 1HZ* = synthetic 1s indices (only on some accounts).
# The dropdown lists all of them; the engine validates the pick at runtime and
# reports Deriv's own answer if a symbol cannot trade on the account.
AVAILABLE_MARKETS = {
    "Gold (XAU/USD)": "frxXAUUSD",
    "Silver (XAG/USD)": "frxXAGUSD",
    "EUR/USD": "frxEURUSD",
    "GBP/USD": "frxGBPUSD",
    "USD/JPY": "frxUSDJPY",
    "AUD/USD": "frxAUDUSD",
    "USD/CHF": "frxUSDCHF",
    "USD/CAD": "frxUSDCAD",
    "NZD/USD": "frxNZDUSD",
    "Volatility 10 (1s)": "1HZ10V",
    "Volatility 25 (1s)": "1HZ25V",
    "Volatility 50 (1s)": "1HZ50V",
    "Volatility 75 (1s)": "1HZ75V",
    "Volatility 100 (1s)": "1HZ100V",
}

DEFAULT_MARKET_DISPLAY = "Gold (XAU/USD)"
SYMBOL = AVAILABLE_MARKETS[DEFAULT_MARKET_DISPLAY]
SYMBOL_DISPLAY = DEFAULT_MARKET_DISPLAY


# --- Candle-trend engine ---------------------------------------------------
CANDLE_GRANULARITIES = {"5m": 300, "15m": 900, "30m": 1800, "1h": 3600}
CANDLE_LOOKBACK = 80
CANDLE_REFRESH_SECONDS = 30
ENTRY_TIMEFRAME = "15m"
TREND_TIMEFRAMES = ["30m", "1h"]

MAX_TRADES_PER_DAY = 10
ENTRY_SCORE_THRESHOLD = 11
MTF_MIN_AGREEMENT = 2
SCORE_MAX = 14


# --- Contract (UP/DOWN = CALL/PUT; minute duration; NO barrier) -----------
CONTRACT_TYPE_BUY = "CALL"
CONTRACT_TYPE_SELL = "PUT"
CONTRACT_DURATION = 30
CONTRACT_DURATION_UNIT = "m"
CURRENCY = "USD"


# --- Sensitivity presets (high-selectivity by default) --------------------
STRATEGY_SENSITIVITY_PRESETS = {
    "Conservative": {
        "velocity_threshold": 0.68,
        "burst_threshold": 0.82,
        "mtf_min_agreement": 3,
        "entry_score_threshold": 11,
    },
    "Balanced": {
        "velocity_threshold": 0.55,
        "burst_threshold": 0.72,
        "mtf_min_agreement": 2,
        "entry_score_threshold": 9,
    },
    "Aggressive": {
        "velocity_threshold": 0.50,
        "burst_threshold": 0.68,
        "mtf_min_agreement": 2,
        "entry_score_threshold": 7,
    },
}

DEFAULT_STRATEGY_SENSITIVITY = "Conservative"


# --- Stake / martingale (unchanged by design) -----------------------------
MARTINGALE_MULTIPLIER = 2.5
DEFAULT_INITIAL_STAKE = 1.0
DEFAULT_MAX_MARTINGALE_STEPS = 3


# --- UI buffer -------------------------------------------------------------
TICK_BUFFER_SIZE = 500


# --- Logging ---------------------------------------------------------------
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
LOG_FILE = os.path.join(LOG_DIR, "deriv_bot.log")
LOG_LEVEL = "INFO"