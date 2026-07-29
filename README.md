# MomentumMaster TF

A multi-market candle-trend terminal for Deriv.

It trades **UP / DOWN** as **CALL / PUT** on a selectable market — **Gold by default**, plus major forex pairs and synthetic 1-second indices — using a higher-timeframe trend strategy:

- **15m candle trigger**
- Confirmed by **30m + 1h agreement**
- Adaptive ATR-normalised quality gates
- No barriers
- Minute durations: **5 / 15 / 30 / 60**
- Maximum **10 trades per day**

---

## What it does

### Market selector
Gold is the default market. The dropdown also lists forex pairs and synthetic indices. The engine validates the selected market at runtime and reports Deriv’s own response if the symbol cannot trade on the account.

### No barriers
Rise/fall contracts only need direction, so no barrier is used.

### Candle-trend engine
The bot trades only when:

- 30m and 1h agree on direction
- A closed 15m candle confirms the move
- The setup passes confluence filters:
  - trend strength
  - ADX
  - MACD
  - RSI zone
  - market structure
  - price-action pattern
  - divergence guard

This keeps the bot selective and avoids low-quality chop.

### Honest symbol handling
Some Deriv accounts return an empty or incomplete symbol catalogue. The bot no longer treats that as a hard block. It proceeds and lets Deriv’s tick subscription or proposal response provide the final verdict.

### Martingale
Martingale is used exactly as configured.

---

## Files

```text
config.py
requirements.txt
dashboard.py
README.md
src/logger.py
src/state_manager.py
src/api_client.py
src/strategy.py
src/trading_engine.py