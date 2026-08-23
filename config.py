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
    # Derived indices only. Financial markets such as forex, commodities,
    # stocks, and crypto are intentionally not included in the fallback list.
    "Volatility 10 (1s)": "1HZ10V",
    "Volatility 25 (1s)": "1HZ25V",
    "Volatility 50 (1s)": "1HZ50V",
    "Volatility 75 (1s)": "1HZ75V",
    "Volatility 100 (1s)": "1HZ100V",
    "Volatility 150 (1s)": "1HZ150V",
    "Volatility 200 (1s)": "1HZ200V",
    "Volatility 300 (1s)": "1HZ300V",
    "Volatility 10": "R_10",
    "Volatility 25": "R_25",
    "Volatility 50": "R_50",
    "Volatility 75": "R_75",
    "Volatility 100": "R_100",
    "Jump 10": "JD10",
    "Jump 25": "JD25",
    "Jump 50": "JD50",
    "Jump 75": "JD75",
    "Jump 100": "JD100",
    "Boom 300": "BOOM300N",
    "Boom 500": "BOOM500",
    "Boom 1000": "BOOM1000",
    "Crash 300": "CRASH300N",
    "Crash 500": "CRASH500",
    "Crash 1000": "CRASH1000",
    "Step Index": "stpRNG",
    "Range Break 100": "RDBEAR",
    "Range Break 200": "RDBULL",
}

DEFAULT_MARKET_DISPLAY = "Volatility 10 (1s)"
SYMBOL = AVAILABLE_MARKETS[DEFAULT_MARKET_DISPLAY]
SYMBOL_DISPLAY = DEFAULT_MARKET_DISPLAY

DIGIT_DEFAULT_BARRIER = 6
DIGIT_TICK_DURATION_OPTIONS = [1, 2]
DIGIT_DEFAULT_TICK_DURATION = 1
DIGIT_REVIEW_INTERVAL_SECONDS = 60.0

DIGIT_WINDOWS = {"fast": 20, "medium": 50, "slow": 200}

# Optional window toggles / per-window thresholds for newer dashboard versions.
DIGIT_WINDOW_ENABLED = {
    "fast": True,
    "medium": True,
    "slow": True,
}

DIGIT_MIN_OVER6_SHARE = 0.31

DIGIT_MIN_OVER6_SHARES = {
    "fast": 0.31,
    "medium": 0.31,
    "slow": 0.30,
}

# Lower digit values are still 0 through 6 for Over 6.
DIGIT_LOWER_CONFIRM_MAX = 6

# Maximum selectable lower-tick confirmation length in newer dashboard versions.
DIGIT_LOWER_CONFIRMATION_MAX = 20

# Default lower confirmation count.
DIGIT_DEFAULT_LOWER_CONFIRMATIONS = 1

DIGIT_DEFAULT_RECOVERY_MULTIPLIER = 1.1
DIGIT_DEFAULT_RECOVERY_ENABLED = True
DIGIT_MAX_RECOVERY_STEPS = 10

# This is the default take-profit amount in account currency.
# Set it to 0 in the sidebar if you do not want take-profit.
DIGIT_DEFAULT_PROFIT_TARGET = 1.0

# 0 disables the daily trade cap completely.
# The bot may take as many trades as the strategy produces.
MAX_TRADES_PER_DAY = 0

CURRENCY = "USD"
MARTINGALE_MULTIPLIER = DIGIT_DEFAULT_RECOVERY_MULTIPLIER
DEFAULT_INITIAL_STAKE = 1.0
TICK_BUFFER_SIZE = 500

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
LOG_FILE = os.path.join(LOG_DIR, "deriv_bot.log")
LOG_LEVEL = "INFO"
