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

CANDLE_GRANULARITIES = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600}
CANDLE_LOOKBACK = 80
CANDLE_REFRESH_SECONDS = 30

ENTRY_TIMEFRAME = "15m"
DEFAULT_ENTRY_TIMEFRAME = "15m"

# Duration-aware trigger candle:
#   1m and 2m contracts use the 5m trigger (same logic as 5m contracts).
#   5m/15m use 5m.
#   30m/60m use 15m.
ENTRY_TIMEFRAME_BY_DURATION = {1: "5m", 2: "5m", 5: "5m", 15: "5m", 30: "15m", 60: "15m"}

TREND_TIMEFRAMES = ["30m", "1h"]
MAX_TRADES_PER_DAY = 10

ENTRY_SCORE_THRESHOLD = 20
MTF_MIN_AGREEMENT = 2
SCORE_MAX = 25

RSI_PERIOD = 14
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
ADX_PERIOD = 14
ADX_MIN_TREND = 15
DIVERGENCE_LOOKBACK = 5

REGIME_VOL_BAND = {
    "SHORT": (0.00010, 0.05),
    "MEDIUM": (0.00008, 0.06),
    "LONG": (0.00006, 0.08),
}
REGIME_EXHAUSTION_ATR = {"SHORT": 3.25, "MEDIUM": 2.75, "LONG": 2.25}
REGIME_TRIGGER_BODY_MIN = 0.35
REGIME_SHORT_5M_ADX_FLOOR = 15
REGIME_LONG_1H_ADX_FLOOR = 20

CONTRACT_TYPE_BUY = "CALL"
CONTRACT_TYPE_SELL = "PUT"
CONTRACT_DURATION = 30
CONTRACT_DURATION_UNIT = "m"
CURRENCY = "USD"

STRATEGY_SENSITIVITY_PRESETS = {
    "Conservative": {"entry_score_threshold": 20, "entry_adx_floor": 18},
    "Balanced": {"entry_score_threshold": 16, "entry_adx_floor": 15},
    "Aggressive": {"entry_score_threshold": 13, "entry_adx_floor": 12},
}
DEFAULT_STRATEGY_SENSITIVITY = "Conservative"

MARTINGALE_MULTIPLIER = 2.5
DEFAULT_INITIAL_STAKE = 1.0
DEFAULT_MAX_MARTINGALE_STEPS = 3

TICK_BUFFER_SIZE = 500

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
LOG_FILE = os.path.join(LOG_DIR, "deriv_bot.log")
LOG_LEVEL = "INFO"


# ---------------------------------------------------------------------------
# Streamlit compatibility (applies to every page, since every page imports
# this module before rendering).
#
# Streamlit deprecated `use_container_width` (removed after 2025-12-31) in
# favour of `width='stretch' | 'content'`. The terminal's auto-refreshing
# fragments re-render several times per second, so the deprecation notice was
# flooding the log. This patch translates the old kwarg to the new one at call
# time, falls back to the old kwarg on Streamlit builds that lack `width`,
# and is idempotent — zero layout change, zero warnings, future-proof.
# ---------------------------------------------------------------------------
def _patch_streamlit_width_kwargs() -> None:
    try:
        import streamlit as st
    except Exception:
        return
    names = (
        "dataframe", "table", "plotly_chart", "line_chart", "bar_chart",
        "area_chart", "button", "download_button", "link_button",
        "page_link", "form_submit_button",
    )
    for name in names:
        original = getattr(st, name, None)
        if original is None or getattr(original, "_mm_width_patched", False):
            continue

        def _make(fn):
            def _wrapped(*args, **kwargs):
                if "use_container_width" in kwargs and "width" not in kwargs:
                    ucw = kwargs.pop("use_container_width")
                    new_kwargs = dict(kwargs, width=("stretch" if ucw else "content"))
                    try:
                        return fn(*args, **new_kwargs)
                    except TypeError:
                        return fn(*args, use_container_width=ucw, **kwargs)
                return fn(*args, **kwargs)
            _wrapped._mm_width_patched = True
            _wrapped.__name__ = getattr(fn, "__name__", "wrapped")
            return _wrapped

        try:
            setattr(st, name, _make(original))
        except Exception:
            pass


_patch_streamlit_width_kwargs()
