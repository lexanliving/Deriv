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

MARKET_ICONS = {
    "1HZ10V": "⚡",
    "1HZ25V": "🔥",
    "1HZ50V": "🌊",
    "1HZ75V": "🛰️",
    "1HZ100V": "🚀",
    "R_10": "🧭",
    "R_100": "💎",
    "1HZ15V": "🌱",
    "1HZ30V": "🌀",
    "1HZ90V": "🌠",
}

AVAILABLE_MARKETS = {
    "⚡ Volatility 10 (1s)": "1HZ10V",
    "🔥 Volatility 25 (1s)": "1HZ25V",
    "🌊 Volatility 50 (1s)": "1HZ50V",
    "🛰️ Volatility 75 (1s)": "1HZ75V",
    "🚀 Volatility 100 (1s)": "1HZ100V",
    "🧭 Volatility 10": "R_10",
    "💎 Volatility 100": "R_100",
    "🌱 Volatility 15 (1s)": "1HZ15V",
    "🌀 Volatility 30 (1s)": "1HZ30V",
    "🌠 Volatility 90 (1s)": "1HZ90V",
}

DEFAULT_MARKET_DISPLAY = "⚡ Volatility 10 (1s)"
SYMBOL = AVAILABLE_MARKETS[DEFAULT_MARKET_DISPLAY]
SYMBOL_DISPLAY = DEFAULT_MARKET_DISPLAY
MANAGED_SYMBOLS = list(AVAILABLE_MARKETS.values())

DIGIT_DEFAULT_BARRIER = 6
DIGIT_TICK_DURATION_OPTIONS = [1, 2]
DIGIT_DEFAULT_TICK_DURATION = 1
DIGIT_REVIEW_INTERVAL_SECONDS = 60.0

# Default rolling window sizes.
DIGIT_WINDOWS = {"fast": 20, "medium": 50, "slow": 200}

# Default window stage switches.
DIGIT_WINDOW_ENABLED = {
    "fast": True,
    "medium": True,
    "slow": True,
}

# Fallback global threshold.
DIGIT_MIN_OVER6_SHARE = 0.31

# Separate default threshold for each window.
DIGIT_MIN_OVER6_SHARES = {
    "fast": 0.31,
    "medium": 0.31,
    "slow": 0.30,
}

# Lower digit values are still 0 through 6 for Over 6.
DIGIT_LOWER_CONFIRM_MAX = 6

# Maximum selectable lower-tick confirmation length.
DIGIT_LOWER_CONFIRMATION_MAX = 20

# Default lower confirmation count.
DIGIT_DEFAULT_LOWER_CONFIRMATIONS = 1

# Upper-digit behavior:
# "kill"  = any 7-9 before completion kills the signal for that review window.
# "reset" = any 7-9 before completion resets the lower sequence.
DIGIT_UPPER_MODE = "kill"

DIGIT_DEFAULT_RECOVERY_MULTIPLIER = 1.1
DIGIT_DEFAULT_RECOVERY_ENABLED = True
DIGIT_MAX_RECOVERY_STEPS = 10

# Global app-wide take-profit target.
# 0 disables global take-profit.
GLOBAL_TAKE_PROFIT_TARGET = 50.0

# Kept for backward compatibility only.
DIGIT_DEFAULT_PROFIT_TARGET = 1.0

# 0 disables the daily trade cap completely.
MAX_TRADES_PER_DAY = 0

CURRENCY = "USD"
MARTINGALE_MULTIPLIER = DIGIT_DEFAULT_RECOVERY_MULTIPLIER
DEFAULT_INITIAL_STAKE = 1.0
TICK_BUFFER_SIZE = 500

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
LOG_FILE = os.path.join(LOG_DIR, "deriv_bot.log")
LOG_LEVEL = "INFO"
