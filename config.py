"""config.py — MomentumMaster TF configuration."""

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


DERIV_APP_ID = os.getenv("DERIV_APP_ID") or _streamlit_secret("DERIV_APP_ID") or ""
DERIV_API_TOKEN = os.getenv("DERIV_API_TOKEN") or _streamlit_secret("DERIV_API_TOKEN") or ""
DERIV_WS_URL = ""

# Broad local catalogue used immediately by the selector. At startup the engine
# also asks Deriv for the live symbol catalogue and validates the chosen symbol.
# Unsupported symbol/contract combinations are rejected safely at proposal time.
AVAILABLE_MARKETS = {
    "Gold (XAU/USD)": "frxXAUUSD", "Silver (XAG/USD)": "frxXAGUSD",
    "Platinum (XPT/USD)": "frxXPTUSD", "Palladium (XPD/USD)": "frxXPDUSD",
    "EUR/USD": "frxEURUSD", "GBP/USD": "frxGBPUSD", "USD/JPY": "frxUSDJPY",
    "AUD/USD": "frxAUDUSD", "USD/CHF": "frxUSDCHF", "USD/CAD": "frxUSDCAD",
    "NZD/USD": "frxNZDUSD", "EUR/GBP": "frxEURGBP", "EUR/JPY": "frxEURJPY",
    "EUR/AUD": "frxEURAUD", "EUR/CAD": "frxEURCAD", "EUR/CHF": "frxEURCHF",
    "EUR/NZD": "frxEURNZD", "GBP/AUD": "frxGBPAUD", "GBP/CAD": "frxGBPCAD",
    "GBP/CHF": "frxGBPCHF", "GBP/JPY": "frxGBPJPY", "GBP/NZD": "frxGBPNZD",
    "AUD/CAD": "frxAUDCAD", "AUD/CHF": "frxAUDCHF", "AUD/JPY": "frxAUDJPY",
    "AUD/NZD": "frxAUDNZD", "CAD/CHF": "frxCADCHF", "CAD/JPY": "frxCADJPY",
    "CHF/JPY": "frxCHFJPY", "NZD/CAD": "frxNZDCAD", "NZD/JPY": "frxNZDJPY",
    "Volatility 10 (1s)": "1HZ10V", "Volatility 25 (1s)": "1HZ25V",
    "Volatility 50 (1s)": "1HZ50V", "Volatility 75 (1s)": "1HZ75V",
    "Volatility 100 (1s)": "1HZ100V", "Volatility 10": "R_10",
    "Volatility 25": "R_25", "Volatility 50": "R_50", "Volatility 75": "R_75",
    "Volatility 100": "R_100", "Volatility 150 (1s)": "1HZ150V",
    "Volatility 200 (1s)": "1HZ200V", "Volatility 300 (1s)": "1HZ300V",
    "Jump 10": "JD10", "Jump 25": "JD25", "Jump 50": "JD50",
    "Jump 75": "JD75", "Jump 100": "JD100",
    "Boom 300": "BOOM300N", "Boom 500": "BOOM500", "Boom 1000": "BOOM1000",
    "Crash 300": "CRASH300N", "Crash 500": "CRASH500", "Crash 1000": "CRASH1000",
    "Step Index": "stpRNG", "Range Break 100": "RDBEAR", "Range Break 200": "RDBULL",
}

DEFAULT_MARKET_DISPLAY = "Volatility 10 (1s)"
SYMBOL = AVAILABLE_MARKETS[DEFAULT_MARKET_DISPLAY]
SYMBOL_DISPLAY = DEFAULT_MARKET_DISPLAY

DIGIT_DEFAULT_BARRIER = 6
DIGIT_TICK_DURATION_OPTIONS = [1, 2]
DIGIT_DEFAULT_TICK_DURATION = 1
DIGIT_REVIEW_INTERVAL_SECONDS = 60.0
DIGIT_WINDOWS = {"fast": 20, "medium": 50, "slow": 200}
DIGIT_MIN_OVER6_SHARE = 0.35
DIGIT_LOWER_CONFIRM_MAX = 6
DIGIT_MIN_QUOTE_EDGE = 0.02
DIGIT_REQUIRE_QUOTE_EDGE = True
DIGIT_DEFAULT_RECOVERY_MULTIPLIER = 1.1
DIGIT_DEFAULT_RECOVERY_ENABLED = True
DIGIT_MAX_RECOVERY_STEPS = 3
DIGIT_MAX_SESSION_LOSS = 0.0  # 0 disables the optional session loss stop.


MAX_TRADES_PER_DAY = 10
CURRENCY = "USD"
MARTINGALE_MULTIPLIER = DIGIT_DEFAULT_RECOVERY_MULTIPLIER
DEFAULT_INITIAL_STAKE = 1.0
TICK_BUFFER_SIZE = 500

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
LOG_FILE = os.path.join(LOG_DIR, "deriv_bot.log")
LOG_LEVEL = "INFO"
