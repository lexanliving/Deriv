# MomentumMaster TF

A multi-market trend terminal for Deriv with a full read-only trading-journal
cockpit (the Performance Scope). It trades Up / Down as Call / Put on a
selectable market — Gold by default, plus major forex pairs and the synthetic
1-second indices — using a higher-timeframe trend strategy: a duration-aware
candle-close trigger confirmed by 30m + 1h agreement, scored across several
independent factors with adaptive quality gates and an express lane for
overwhelming candles. No barriers. Minute lengths 1 / 2 / 5 / 15 / 30 / 60.
At most 10 trades per day, per tab.

## Duration-aware trigger

The trigger candle matches the contract length so timing stays sharp at every
duration, while the higher-timeframe confirmation is unchanged:

- 1m  contract -> 1m  candle-close trigger (scalp)
- 2m  contract -> 1m  candle-close trigger (scalp)
- 5m  contract -> 5m  candle-close trigger
- 15m contract -> 5m  candle-close trigger
- 30m contract -> 15m candle-close trigger (original edge)
- 60m contract -> 15m candle-close trigger

Trend confirmation is always 30m + 1h. Signals still fire only on a new
trigger-candle close.

## Hard gates vs. confidence stack

The flat 25-point score answers "how good is this setup"; a separate hard-gate
layer answers "is this setup even allowed". Hard gates (trend agreement,
trigger break, close beyond the fast EMA, the express-aware exhaustion limit,
RSI/price divergence, entry-timeframe structure, plus the regime gates) cannot
be overridden by a high score. The express lane widens the exhaustion band only
when the candle's own conviction is overwhelming, so a power breakout that is
far from its EMA is taken instead of chased-and-rejected.

## Two views

**Terminal (`dashboard.py`)** — configure account, market, stake plan and
selectivity; start/stop the engine; watch a themed 5-minute candlestick chart
(with a tick-sparkline fallback for the first ~30s), the live trend, status,
trades, and the Decision Log (every trigger-candle review, with the market,
result and reason of every setup). The CSV export lives here.

**Performance Scope (`pages/bubbles.py`)** — a read-only cockpit with four tabs
(Overview / Calendar / Trades / Analytics). The Trades tab shows every trade as
a full BEFORE/AFTER report (the 10 confluence factors, the entry
ADX/RSI/MACD/ATR/close with a plain read, the per-timeframe bias snapshot, and a
narrative of why it was placed and why it won or lost, including MAE/MFE).

Switch between the two views with the sidebar or the ← Back to Terminal button.
The Scope reads an append-only archive, so a cleared live log never erases a
past day.

## How it behaves

- The engine runs in a background thread, independent of the browser. Closing a
  tab does not stop it.
- A watchdog relaunches the engine automatically if it dies while you intend it
  to run — resuming the stake plan, results, and daily cap.
- Every terminal outcome (won, lost, cancelled, skipped, unresolved) is written
  to the log with a reason and a signal id, so nothing is silently dropped.
- Multiple tabs trade concurrently and independently (own strategy, stake plan
  and martingale each). Avoid mirror-image markets (a pair and its USD-inverse)
  on different tabs — they move as one doubled bet.
- On a real account, orders stay blocked until you type LIVE.

## Credentials

Streamlit Cloud → App settings → Secrets:
