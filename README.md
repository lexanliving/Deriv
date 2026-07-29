# MomentumMaster TF

A multi-market candle-trend terminal for Deriv. It trades **UP / DOWN as
CALL / PUT** on a selectable market — **Gold by default**, plus major forex
pairs and the synthetic 1-second indices — using a higher-timeframe trend
strategy: a **15m candle trigger** confirmed by **30m + 1h agreement**, with
adaptive ATR-normalised quality gates. No barriers. Minute durations
(5 / 15 / 30 / 60). At most 10 trades per day.

## What it does

- **Market selector** (Gold default). The dropdown lists forex, gold, and
  synthetics; the engine validates the pick at runtime.
- **No barriers.** Rise/fall contracts only need direction.
- **Candle-trend engine.** Trades only when 30m and 1h agree on direction and
  a closed 15m candle confirms the move — few setups, high selectivity.
- **Honest symbol handling.** Some Deriv accounts return an *empty* symbol
  catalogue; the bot no longer treats that as a hard block. It proceeds and
  surfaces **Deriv's own verdict** from the tick subscription or proposal.
- **Martingale** exactly as configured (unchanged by design).

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

Streamlit Cloud → App settings → Secrets:

    DERIV_APP_ID = "YOUR_PAT_APP_ID"
    DERIV_API_TOKEN = "YOUR_DERIV_PAT"

## Run

    python3 -m venv .venv
    source .venv/bin/activate
    python3 -m pip install -r requirements.txt
    python3 -m compileall -q .      # should print nothing
    streamlit run dashboard.py

## The number to watch

The dashboard shows **Expectancy / trade** = (win rate × avg win) −
(loss rate × avg loss). Positive over a real sample means the setup works;
negative means no stake plan fixes it. Martingale changes how you *survive* a
losing streak — it does not change expectancy.

## Risk notice

This software can submit demo or real-money orders and does not guarantee
profit. Start on demo. An unresolved contract never advances Martingale.