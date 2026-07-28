# MomentumMaster TF

Multi-market candle-trend trading terminal for Deriv.

Trades CALL/PUT contracts on a selectable market (Gold, forex pairs, or
synthetic indices) using a higher-timeframe trend strategy:

- 1h + 30m must agree on direction (hard requirement).
- 15m closed candle provides the entry trigger (no mid-candle repainting).
- 5m adds an alignment bonus.
- Adaptive ATR-normalised gates: EMA separation, volatility percentile band,
  extension guard.
- Max 10 trades/day. Signals expire after 120s.

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

## The number to watch

The dashboard shows **Expectancy / trade**:

    (win rate × avg win) − (loss rate × avg loss)

Positive expectancy over a meaningful sample = the system works.
Negative expectancy = no stake progression, however clever, will fix it.

Martingale is enabled as configured (max steps + reset after win). It changes
how you survive losing streaks; it does not change expectancy.

## Risk notice

This software can submit demo or real-money orders. It does not guarantee
signals, fills, uptime, or profit. Run it on demo first and let the expectancy
number — not a single good day — tell you whether it's ready.