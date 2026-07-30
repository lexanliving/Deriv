# MomentumMaster TF

A multi-market trend terminal for Deriv with a full read-only **trading-journal
cockpit** (the Performance Scope). It trades **Up / Down** as **Call / Put** on a
selectable market — **Gold by default**, plus major forex pairs and the synthetic
1-second indices — using a higher-timeframe trend strategy: a **15m candle
trigger** confirmed by **30m + 1h agreement**, scored across several independent
factors with adaptive, **duration-aware** quality gates. **No barriers.** Minute
lengths (5 / 15 / 30 / 60). At most 10 trades per day, per tab.

## Two views

- **Terminal** (`dashboard.py`) — configure account, market, stake plan and
  selectivity; start/stop the engine; watch a themed **5-minute candlestick
  chart** (with a tick-sparkline fallback for the first ~30s), the live trend,
  status, trades, and the **Decision Log** (every 15-minute review, with the
  market, result and reason of every setup). The CSV export lives here.
- **Performance Scope** (`pages/2_📊_Performance_Scope.py`) — a read-only cockpit
  with four tabs (Overview / Calendar / Trades / Analytics). The Trades tab shows
  every trade as a full BEFORE/AFTER report (the 10 confluence factors, the entry
  ADX/RSI/MACD/ATR/close with a plain read, the per-timeframe bias snapshot, and a
  narrative of *why it was placed* and *why it won or lost*, including MAE/MFE).
  Switch between the two views with the sidebar or the **← Back to Terminal**
  button. The Scope reads an append-only archive, so a cleared live log never
  erases a past day.

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
- On a real account, orders stay blocked until you type **LIVE**.

## Credentials

Streamlit Cloud → **App settings → Secrets**:

```toml
DERIV_APP_ID = "YOUR_PAT_APP_ID"
DERIV_API_TOKEN = "YOUR_DERIV_PAT"