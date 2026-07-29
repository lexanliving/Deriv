# MomentumMaster TF

Multi-market candle-trend terminal for Deriv. Trades **UP/DOWN (CALL/PUT)** on
forex & gold (and synthetic indices if the account offers them).

## What it does

- Market selector (Gold default) — pick whatever your Deriv account can trade.
- Contract type **CALL/PUT** with a **5/15/30/60-minute** duration. **No barriers.**
- Strategy: trade only when **30m + 1h agree** on direction and a **closed 15m candle**
  confirms the move (clear-trend, high-selectivity, few trades).
- At startup the engine checks the symbol against your account's live symbol list.
  If a pick isn't available, the banner lists the symbols that ARE — no dead ends.

## Files

    config.py
    requirements.txt        (keep as-is)
    dashboard.py
    README.md
    src/logger.py           (keep as-is)
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

`compileall` should print nothing. If it prints errors, a file still has invalid
Python (usually a separator line missing its `#`).

## The number to watch

The dashboard shows **Expectancy / trade** = (win rate × avg win) − (loss rate × avg loss).
Positive over a real sample means the setup works; negative means no stake plan fixes it.

## Risk notice

This software can submit demo or real-money orders and does not guarantee profit.
Martingale is enabled by configuration and can grow losses fast — manage it yourself.
Start on demo. An unresolved contract never advances Martingale.