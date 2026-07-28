# MomentumMaster

Tick-momentum trading terminal for Deriv's synthetic 1-second volatility indices.

## Files

    config.py
    requirements.txt
    dashboard.py
    README.md
    src/logger.py        (unchanged — keep your existing copy)
    src/strategy.py      (unchanged — keep your existing copy)
    src/state_manager.py
    src/api_client.py
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

## Markets

The selector offers the five synthetic 1s indices (1HZ10V–1HZ100V), which are
available on every Deriv account. Symbols are sent to Deriv exactly as written
(they are case-sensitive). Gold and forex need a different contract type
(CALL/PUT) and longer durations — ask for that configuration separately.

## The number to watch

The dashboard shows **Expectancy / trade**:

    (win rate × avg win) − (loss rate × avg loss)

Positive expectancy over a meaningful sample means the system works.
Negative expectancy means no stake progression will fix it.

## Risk notice

This software can submit demo or real-money orders. It does not guarantee
signals, fills, uptime, or profit. Run it on demo first and let the expectancy
number — not a single good day — tell you whether it's ready.