# MomentumMaster TF

A multi-market trend terminal for Deriv with a full read-only trading-journal cockpit and an offline learning loop.

It trades **Up / Down** as **Call / Put** on a selectable market — **Gold by default**, plus major forex pairs and the synthetic 1-second indices — using a higher-timeframe trend strategy:

- a duration-aware candle-close trigger
- confirmation by **30m + 1h** agreement
- a 25-point confluence score
- adaptive quality gates
- an express lane for overwhelming candles

No barriers.  
Minute lengths: **1 / 2 / 5 / 15 / 30 / 60**.  
At most **10 trades per day, per tab**.

---

## Duration-aware trigger

The trigger candle matches the contract length so timing stays sharp at every duration, while the higher-timeframe confirmation is unchanged:

| Contract | Trigger candle |
|---|---|
| 1m  | 5m candle-close trigger |
| 2m  | 5m candle-close trigger |
| 5m  | 5m candle-close trigger |
| 15m | 5m candle-close trigger |
| 30m | 15m candle-close trigger |
| 60m | 15m candle-close trigger |

Trend confirmation is always **30m + 1h**.

Signals fire only on a **new trigger-candle close**.

---

## Hard gates vs. confidence stack

The flat **25-point score** answers:

> “How good is this setup?”

A separate **hard-gate layer** answers:

> “Is this setup even allowed?”

Hard gates cannot be overridden by a high score.

They include:

- trend agreement
- trigger break
- close beyond the fast EMA
- the express-aware exhaustion limit
- RSI/price divergence
- entry-timeframe structure
- regime-specific gates

The **express lane** widens the exhaustion band only when the candle’s own conviction is overwhelming, so a power breakout that is far from its EMA is taken instead of being chased-and-rejected.

---

## Three views

### 1. Terminal — `dashboard.py`

Configure:

- account
- market
- stake plan
- selectivity

Then:

- start / stop the engine
- watch a themed 5-minute candlestick chart
- see live trend, status, trades, and the Decision Log

The Decision Log records **every trigger-candle review** — whether it traded or stood aside — along with the market, result, and reason.

The CSV export lives here.

---

### 2. Performance Scope — `pages/bubbles.py`

A read-only cockpit with four tabs:

- **Overview**
- **Calendar**
- **Trades**
- **Analytics**

The **Trades** tab shows every trade as a full **BEFORE / AFTER** report:

- the 10 confluence factors
- entry ADX / RSI / MACD / ATR / close
- a plain-language read of each entry reading
- the per-timeframe bias snapshot
- a narrative of why it was placed
- a narrative of why it won or lost, including MAE / MFE

Switch between the Terminal and the Scope using the sidebar or the **← Back to Terminal** button.

The Scope reads an append-only archive, so a cleared live log never erases a past day.

---

### 3. Research Loop — `pages/research.py`

A read-only offline learning page.

It appears automatically in the sidebar.

It has four tabs:

- **SEND TO Q**
- **BACKUP**
- **GATE BACKTEST**
- **MISSED & AVOIDABLE**

This page:

- places no trades
- mutates nothing live
- never changes the strategy by itself

It exists to turn recorded history into sharper future decisions.

---

## Offline learning loop

MomentumMaster TF now includes an **offline learning loop**.

This is deliberately conservative.

The bot **does not** rewrite its own strategy at runtime.

It records decisions, then a human reviews the evidence offline and proposes changes that must be forward-tested on demo before being manually opted into.

### What gets recorded

Every trigger-candle review is journaled.

That includes:

- taken setups
- stand-asides
- rejection reasons
- score and threshold
- factor breakdown
- regime
- duration
- MTF biases
- outcome
- P&L
- MAE / MFE

When a trade is taken, the engine also appends one compact line to:

```text
logs/trade_snapshots.jsonl
```

Each snapshot contains:

- signal metadata
- score / threshold / regime
- entry price
- analytical stop / target equivalents
- last ~40 candles per timeframe

This recorder is isolated and fully swallowed.

It cannot affect:

- proposal
- buy
- monitoring
- the async loop

If snapshot writing fails, the bot continues normally.

---

## Research Loop tabs

### SEND TO Q

Download:

- the full learning bundle as a zip
- `postmortem.json`

The bundle contains:

- `trade_journal.csv`
- `journal_archive.csv`
- `trade_snapshots.jsonl`
- `postmortem.json`
- `gate_backtest.json`
- `READ_ME_FIRST.txt`

This tab also shows the plain-language lenses:

| Lens | Meaning | Lever |
|---|---|---|
| avoidable losses | lost after price was in favour | duration / exit |
| fragile wins | won but spent too much time against entry | entry timing |
| gatekeeper factor | weakest soft factor in near-miss trending stand-asides | the one gate to re-test |
| edges | where the closed sample actually made money | symbol / hour / regime selection |

---

### BACKUP

Export:

- master archive CSV
- merged JSON view

Import:

- archive CSV
- merged JSON

Import is **idempotent**.

That means:

> importing the same backup twice adds nothing the second time.

---

### GATE BACKTEST

A non-destructive offline replay of recorded reviews.

It compares:

- weight variants
- thresholds

against the real **AS-RECORDED** baseline.

You can export a plain-text preset proposal.

Nothing is auto-applied.

---

### MISSED & AVOIDABLE

This tab shows:

- avoidable losses table
- fragile wins table
- gatekeeper factor chart
- edge tables by:
  - symbol
  - hour UTC
  - regime

---

## The honest ceiling

This loop is designed to avoid the most common failure mode:

> self-tuning on a tiny sample of noisy binary-option outcomes.

So the system intentionally stops at:

- recording
- explaining
- backtesting offline
- proposing a preset

The human still decides.

The bot never self-modifies.

### Suggested cadence

- Review the bundle **weekly**, not after every trade.
- Change **one thing at a time**.
- Forward-test on demo before opting in manually.
- Trust blank days. Even stand-asides produce useful gate data.

---

## How it behaves

The engine runs in a background thread, independent of the browser.

Closing a tab does not stop it.

A watchdog relaunches the engine automatically if it dies while you intend it to run — resuming the stake plan, results, and daily cap.

Every terminal outcome is written to the log with a reason and a signal id:

- won
- lost
- cancelled
- skipped
- unresolved

Nothing is silently dropped.

Multiple tabs trade concurrently and independently:

- own strategy
- own stake plan
- own martingale

Avoid mirror-image markets on different tabs, such as a pair and its USD-inverse, because they move as one doubled bet.

On a real account, orders stay blocked until you type:

```text
LIVE
```

---

## Project structure

```text
dashboard.py
pages/
  bubbles.py
  research.py
src/
  api_client.py
  journal.py
  logger.py
  persistence.py
  state_manager.py
  strategy.py
  trading_engine.py
config.py
requirements.txt
logs/
  trade_journal.csv
  journal_archive.csv
  trade_snapshots.jsonl
  deriv_bot.log
```

---

## Credentials

### Streamlit Cloud

Go to:

```text
App settings → Secrets
```

Add:

```toml
DERIV_APP_ID = "your_app_id"
DERIV_API_TOKEN = "your_pat_token"
```

### Local `.env`

For local runs, you can also use:

```env
DERIV_APP_ID=your_app_id
DERIV_API_TOKEN=your_pat_token
```

---

## Local setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
streamlit run dashboard.py
```

---

## Logs and data

The bot writes:

```text
logs/trade_journal.csv
logs/journal_archive.csv
logs/trade_snapshots.jsonl
logs/deriv_bot.log
```

The journal archive is the lossless master copy.

The snapshots file is only appended on taken trades and is used for offline review.

---

## Safety notes

- Demo accounts use virtual funds.
- Real accounts require explicit confirmation.
- Real orders remain blocked until you type `LIVE`.
- The offline learning loop never auto-applies changes.
- Any proposed preset should be forward-tested on demo first.

---

## Summary

MomentumMaster TF is:

- a live Deriv trend terminal
- a full decision journal
- a read-only performance cockpit
- an offline learning loop

It is built around one principle:

> record everything, learn offline, change one thing at a time, and never let the bot rewrite itself live.
