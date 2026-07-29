# MomentumMaster TF

A multi-market candle-trend terminal for Deriv. It trades **UP / DOWN** as
**CALL / PUT** on a selectable market — **Gold by default**, plus major forex
pairs and the synthetic 1-second indices — using a higher-timeframe trend
strategy: a **15m candle trigger** confirmed by **30m + 1h agreement**, with a
multi-factor confluence score and adaptive ATR-normalised quality gates.
**No barriers.** Minute durations (**5 / 15 / 30 / 60**). At most **10 trades
per day**.

---

## What it does

- **Market selector (Gold default).** The dropdown lists forex, gold, and
  synthetics; the engine validates the pick at runtime.
- **No barriers.** Rise/fall contracts only need direction.
- **Candle-trend engine.** Trades only when 30m and 1h agree on direction
  *and* a closed 15m candle confirms the move — few setups, high selectivity.
- **Confluence scoring.** Each setup is scored out of 25 across trend,
  trigger, momentum, volatility, ADX, MACD, RSI zone, price-action pattern,
  and market structure, with an RSI/price divergence guard. It only trades
  when the score clears the sensitivity threshold.
- **Honest symbol handling.** Some Deriv accounts return an empty symbol
  catalogue; the bot treats that as a hint, not a block, and lets Deriv's own
  tick/proposal response give the verdict.
- **Martingale** exactly as configured (multiplier, max steps, initial stake).
- **Auto-restart watchdog.** If the engine thread dies while the bot is meant
  to be running, it is relaunched automatically — resuming martingale, P&L,
  and the daily trade cap instead of resetting them.

---

## Files

```
config.py            # all settings: markets, timeframes, scoring, martingale, logging
requirements.txt
dashboard.py         # Streamlit terminal UI + background engine + watchdog
README.md
run_247.py           # optional headless runner for 24/7 on a VPS
src/
  logger.py          # non-blocking file/console logging
  state_manager.py   # thread-safe shared state (UI <-> engine)
  api_client.py      # Deriv Options API client (PAT -> OTP WebSocket)
  strategy.py        # multi-factor confluence engine ("Meridian")
  trading_engine.py  # order execution, martingale, reconciliation, monitoring
```

---

## Credentials

### Streamlit Cloud → App settings → Secrets

```toml
DERIV_APP_ID = "YOUR_PAT_APP_ID"
DERIV_API_TOKEN = "YOUR_DERIV_PAT"
```

### Local `.env`

```
DERIV_APP_ID=YOUR_PAT_APP_ID
DERIV_API_TOKEN=YOUR_DERIV_PAT
```

The PAT is only ever sent to the Deriv REST API — never in a WebSocket
message or URL.

---

## Run (dashboard)

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
python3 -m compileall -q .      # should print nothing
streamlit run dashboard.py
```

---

## Auto-restart watchdog

The engine runs in a background thread, independent of the browser. The
dashboard checks it every 2 seconds:

| Situation | Result |
|---|---|
| Engine thread crashes | Relaunched in ~2s; martingale, P&L, and daily cap preserved |
| Connection drops and the engine exits | Treated as a crash → auto-relaunched |
| Repeated failures (5 in a row) | Auto-restart pauses; a clear error is shown |
| You press STOP | Stays stopped — the watchdog will not revive it |
| You press START | Fresh session (resets P&L/martingale) and clears the failure count |

On a restart, the daily trade cap is rebuilt from today's filled trades so a
relaunch can't bypass the 10/day limit.

---

## Running 24/7 (VPS)

The dashboard is a great control panel, but Streamlit hosting can recycle an
idle container, which stops the engine. For true unattended operation, run
the headless runner on a small VPS under `systemd`:

```bash
# engine1.env / engine2.env hold the per-engine settings, e.g.
#   ENGINE_NAME=engine1
#   ENGINE_ACCOUNT_ID=VRTC1234567
#   ENGINE_ACCOUNT_TYPE=DEMO
#   ENGINE_MARKET=Gold (XAU/USD)
#   ENGINE_STAKE=1.0
#   DERIV_APP_ID=...
#   DERIV_API_TOKEN=...

sudo systemctl enable --now mmbot@engine1 mmbot@engine2
journalctl -u mmbot@engine1 -f
```

`run_247.py` restarts the engine on crash and on reboot, and shuts down
cleanly on `SIGTERM` (finishing any active contract first).