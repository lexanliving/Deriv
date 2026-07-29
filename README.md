# MomentumMaster

Tick-momentum trading terminal for Deriv's synthetic 1-second volatility indices.

## Files

    config.py
    requirements.txt
    dashboard.py
    README.md
    src/logger.py
    src/state_manager.py
    src/api_client.py
    src/strategy.py
    src/trading_engine.py

## Credentials

Streamlit Cloud secrets:

    DERIV_APP_ID = "YOUR_PAT_APP_ID"
    DERIV_API_TOKEN = "YOUR_DERIV_PAT"

## Run

    python3 -m venv .venv
    source .venv/bin/activate
    python3 -m pip install -r requirements.txt
    python3 -m compileall -q .
    streamlit run dashboard.py

`python3 -m compileall -q .` should print nothing. If it prints errors, a file
still contains invalid Python (most often a dashed separator line without `#`).

## Markets

The selector offers the five synthetic 1s indices (1HZ10V-1HZ100V), available on
every Deriv account. Symbols are sent to Deriv exactly as written (case-sensitive).

## Risk notice

This software can submit demo or real-money orders. It does not guarantee signals,
fills, uptime, or profit. Run on demo first. Martingale is enabled by configuration
and can increase losses quickly; an unresolved contract never advances it.