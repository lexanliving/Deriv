# MomentumMaster TF

A multi-market trend terminal for Deriv with a full read-only trading-journal cockpit
(the Performance Scope) and an offline research loop (the Research Lab).

It trades Up / Down as Call / Put on a selectable market — Gold by default, plus major
forex pairs and the synthetic 1-second indices — using a higher-timeframe trend strategy:
a duration-aware candle-close trigger confirmed by 30m + 1h agreement, scored across
several independent factors with adaptive quality gates and an express lane for
overwhelming candles.

No barriers. Minute lengths 1 / 2 / 5 / 15 / 30 / 60. At most 10 trades per day, per tab.

## Duration-aware trigger

The trigger candle matches the contract length so timing stays sharp at every duration,
while the higher-timeframe confirmation is unchanged:

- 1m contract  -> 5m candle-close trigger
- 2m contract  -> 5m candle-close trigger
- 5m contract  -> 5m candle-close trigger
- 15m contract -> 5m candle-close trigger
- 30m contract -> 15m candle-close trigger
- 60m contract -> 15m candle-close trigger

Trend confirmation is always 30m + 1h. Signals still fire only on a new trigger-candle close.

## Hard gates vs. confidence stack

The flat 25-point score answers “how good is this setup”; a separate hard-gate layer
answers “is this setup even allowed”.

Hard gates cannot be overridden by a high score:
- trend agreement,
- trigger break,
- close beyond the fast EMA,
- express-aware exhaustion limit,
- RSI/price divergence,
- entry-timeframe structure,
- regime gates.

The express lane widens the exhaustion band only when the candle’s own conviction is
overwhelming, so a power breakout that is far from its EMA is taken instead of
chased-and-rejected.

## Views

### Terminal (`dashboard.py`)
Configure account, market, stake plan and selectivity; start/stop the engine; watch a
themed 5-minute candlestick chart (with a tick-sparkline fallback for the first ~30s),
the live trend, status, trades, and the Decision Log.

### Performance Scope (`pages/bubbles.py`)
A read-only cockpit with four tabs: Overview / Calendar / Trades / Analytics.

The Trades tab shows every trade as a full BEFORE/AFTER report: the 10 confluence factors,
entry ADX/RSI/MACD/ATR/close, per-timeframe bias snapshot, and a narrative of why it was
placed and why it won or lost, including MAE/MFE.

### Research Lab (`pages/research.py`)
An offline learning page with four tabs:

- **SEND TO Q** — download the learning bundle and postmortem JSON.
- **BACKUP** — export master archive CSV / merged JSON and import backups idempotently.
- **GATE BACKTEST** — offline what-if sweep of weight variants and thresholds.
- **MISSED & AVOIDABLE** — avoidable losses, fragile wins, gatekeeper factors, and edges.

## Offline learning loop

This project now includes a safe, additive offline learning loop.

### What is recorded
- The journal already records every evaluation and outcome.
- On taken signals, the engine now also appends a snapshot line to:
  - `logs/trade_snapshots.jsonl`

Each snapshot contains:
- `signal_id`
- timestamp
- symbol
- direction
- score
- threshold
- regime
- entry price
- last ~40 candles per timeframe as compact `[epoch,o,h,l,c,v]`

The recorder is isolated and error-swallowing. It cannot affect proposal, buy, or monitoring.

### What the loop produces
`src/persistence.py` can compute:

- **avoidable_losses**  
  LOST contracts where `MFE > MAE * 1.0`.  
  Interpretation: price moved in favour, then reversed before expiry.  
  Lever: duration / exit.

- **fragile_wins**  
  WON contracts where `MAE > MFE * 1.0`.  
  Interpretation: the trade survived more drawdown than favourable excursion.  
  Lever: entry timing / noise filter.

- **gatekeeper_factors**  
  For trending stand-asides within 8 points of threshold, counts the weakest soft factor.  
  Lever: re-test only that gate.

- **edges**  
  By symbol, hour-of-day (UTC), and regime.  
  Lever: selection and scheduling.

- **gate backtest**  
  Offline recomputation of variants and thresholds against recorded history.

### Cadence
- Review the bundle weekly, not after every trade.
- Change only one thing at a time.
- Forward-test on demo before manual opt-in.
- A blank day is legitimate; stand-asides still produce useful gatekeeper data.

## The honest ceiling

This bot does **not** rewrite its own strategy, weights, or thresholds at runtime.

A system that self-tunes from a handful of weekly binary trades is overfitting on noise.
That is the most reliable way to lose.

The correct professional substitute is:
1. record everything,
2. review offline,
3. propose one preset,
4. forward-test on demo,
5. opt in manually.

That is exactly what this loop provides.

## Credentials

Streamlit Cloud → App settings → Secrets:

```toml
DERIV_APP_ID = "..."
DERIV_API_TOKEN = "..."
