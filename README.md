# MomentumMaster TF

A multi-market trend terminal for Deriv with a full read-only **trading-journal
cockpit** (the Performance Scope). It trades **Up / Down** as **Call / Put** on a
selectable market — Gold by default, plus major forex pairs and the synthetic
1-second indices — using a higher-timeframe trend strategy: a **15m candle
trigger** confirmed by **30m + 1h agreement**, scored across several independent
factors with adaptive, duration-aware quality gates. **No barriers.** Minute
lengths (5 / 15 / 30 / 60). At most 10 trades per day, per tab.

## Two views

- **Terminal** (`dashboard.py`) — configure account, market, stake plan and
  selectivity; start/stop the engine; live price, trend, status, trades, and the
  Decision Log (every 15-minute review with result + reason). CSV export lives
  here.
- **Performance Scope** (`pages/2_📊_Performance_Scope.py`) — a dark trading
  cockpit built entirely from the journal CSV, with four tabs:
  - **Overview** — KPI strip, equity curve, monthly bars, calendar heatmap,
    month detail, asset donut, recent trades, win/loss streaks.
  - **Calendar** — month navigator + heatmap; pick a day to see its reviews.
  - **Trades** — every trade as a card with *why it was placed* (confluence
    breakdown + entry readings) and *why it won/lost* (MAE / MFE excursion).
  - **Analytics** — the weekly tuning loop: rejection funnel, component edge,
    score-bucket win rate, best/worst hours & weekdays, and per-week report
    cards with a plain-English "look at X" note.

  Filterable by market and period; a user-set starting balance makes the equity
  curve honest. The Scope never writes anything and never touches the engine.

## What the journal now records (treating every row as gold)

Each 15-minute review stores the symbol, signal id, direction, trend, whether it
was taken, the confluence score + the 10-factor breakdown, the rejection reason,
the regime and duration, the entry ADX/RSI/MACD/ATR/close, and — for executed
trades — the outcome, P&L, and **MAE / MFE** (max adverse / favourable
excursion, captured live while the contract is open). Old files are migrated in
place, so nothing is lost when columns are added.

## Running several markets

Each open tab is a fully independent engine (own strategy, stake plan,
martingale) and tabs trade simultaneously with no restriction. Note: two tabs on
**mirror-image markets** (a pair and its USD-inverse) move as one doubled bet —
mix uncorrelated markets (e.g. one forex pair + one Volatility index) to keep
them independent.

## How it behaves

- The engine runs in a background thread, independent of the browser; closing a
  tab does not stop it.
- A watchdog relaunches the engine if it dies while you intend it to run —
  resuming the stake plan, results, and daily cap.
- Every terminal outcome is logged with a reason, so nothing is silently dropped.
- On a real account, orders stay blocked until you type **LIVE**.

## Credentials

Streamlit Cloud → **App settings → Secrets**:

```toml
DERIV_APP_ID = "YOUR_PAT_APP_ID"
DERIV_API_TOKEN = "YOUR_DERIV_PAT"