"""
config.py — MomentumMaster TF configuration.
Direction-only CALL/PUT on a selectable market (Gold by default). No barriers.
Every name imported elsewhere is defined here.
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


# --- Deriv credentials ---
DERIV_APP_ID = os.getenv("DERIV_APP_ID") or _streamlit_secret("DERIV_APP_ID") or ""
DERIV_API_TOKEN = os.getenv("DERIV_API_TOKEN") or _streamlit_secret("DERIV_API_TOKEN") or ""
DERIV_WS_URL = ""  # compatibility only; the live WebSocket URL is fetched at runtime


# --- Markets (clean keys/values — no padding) ---
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


# --- Trend engine ---
CANDLE_GRANULARITIES = {"5m": 300, "15m": 900, "30m": 1800, "1h": 3600}
CANDLE_LOOKBACK = 80
CANDLE_REFRESH_SECONDS = 30
ENTRY_TIMEFRAME = "15m"
TREND_TIMEFRAMES = ["30m", "1h"]
MAX_TRADES_PER_DAY = 10
ENTRY_SCORE_THRESHOLD = 20
MTF_MIN_AGREEMENT = 2
SCORE_MAX = 25


# --- Confluence factors ---
RSI_PERIOD = 14
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
ADX_PERIOD = 14
ADX_MIN_TREND = 15
DIVERGENCE_LOOKBACK = 5


# --- Regime-specific entry gates (duration-aware — see strategy._regime_for) ---
REGIME_VOL_PERCENTILE = {
    "SHORT": (0.20, 0.95),
    "MEDIUM": (0.10, 0.95),
    "LONG": (0.05, 0.97),
}
REGIME_EXHAUSTION_ATR = {
    "SHORT": 3.25,
    "MEDIUM": 2.75,
    "LONG": 2.25,
}
REGIME_TRIGGER_BODY_MIN = 0.35
REGIME_SHORT_5M_ADX_FLOOR = 15
REGIME_LONG_1H_ADX_FLOOR = 20


# --- Contract ---
CONTRACT_TYPE_BUY = "CALL"
CONTRACT_TYPE_SELL = "PUT"
CONTRACT_DURATION = 30
CONTRACT_DURATION_UNIT = "m"
CURRENCY = "USD"


# --- Selectivity presets (HONEST: every key here is actually used) ---
STRATEGY_SENSITIVITY_PRESETS = {
    "Conservative": {"entry_score_threshold": 20, "entry_adx_floor": 18},
    "Balanced":     {"entry_score_threshold": 16, "entry_adx_floor": 15},
    "Aggressive":   {"entry_score_threshold": 13, "entry_adx_floor": 12},
}
DEFAULT_STRATEGY_SENSITIVITY = "Conservative"


# --- Stake plan ---
MARTINGALE_MULTIPLIER = 2.5
DEFAULT_INITIAL_STAKE = 1.0
DEFAULT_MAX_MARTINGALE_STEPS = 3


# --- UI buffer ---
TICK_BUFFER_SIZE = 500


# --- Logging ---
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
LOG_FILE = os.path.join(LOG_DIR, "deriv_bot.log")
LOG_LEVEL = "INFO"