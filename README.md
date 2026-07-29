# MomentumMaster TF

A multi-market trend terminal for Deriv. It trades **Up / Down** as **Call / Put**
on a selectable market — **Gold by default**, plus major forex pairs and the
synthetic 1-second indices — using a higher-timeframe trend strategy: a **15m
candle trigger** confirmed by **30m + 1h agreement**, scored across several
independent factors with adaptive quality gates. **No barriers.** Minute lengths
(**5 / 15 / 30 / 60**). At most **10 trades per day**.

The entry gates are **duration-aware**, not just the trend agreement: a 5/15m
contract additionally requires the 5m timeframe to be genuinely *trending* (its
own ADX, not merely biased) and a decisive trigger candle, since the trade
expires fast. A 60m contract additionally requires the 1h ADX and MACD to
confirm the trend, since the position needs to survive a full hour. See
`src/strategy.py` for the exact per-regime gates.

## Two views

- **Terminal** (`dashboard.py`) — configure the account, market, stake plan and
  selectivity; start and stop the engine; watch the live price, trend, status,
  trades, and the **Decision Log** (every 15-minute review, with the result and
  reason of every setup). The CSV export lives here.
- **Performance Scope** (`pages/2_📊_Performance_Scope.py`) — a read-only bubble
  map of your real results (per trade, by day, by month, by market) plus the
  equity baseline and rollups. Switch between the two views with the sidebar, or
  the **← Back to Terminal** button on the Scope.

The Scope reads an append-only history archive, so a cleared live log never
erases a past day from the charts.

## How it behaves

- The engine runs in a background thread, independent of the browser. Closing a
  tab does not stop it.
- A watchdog relaunches the engine automatically if it dies while you intend it
  to run — resuming the stake plan, results, and daily cap.
- Every terminal outcome (won, lost, cancelled, skipped, unresolved) is written
  to the log with a reason, so nothing is silently dropped.
- On a real account, orders stay blocked until you type **LIVE**.

## Credentials

Streamlit Cloud → **App settings → Secrets**:

```toml
DERIV_APP_ID = "YOUR_PAT_APP_ID"
DERIV_API_TOKEN = "YOUR_DERIV_PAT"