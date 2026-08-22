# MomentumMaster Digit

MomentumMaster Digit is a Streamlit terminal for Deriv digit contracts. It supports manual selection from the live market catalogue, rolling last-digit reviews, and automated **Over 6** entries for one- or two-tick contracts.

## Strategy behavior

The bot collects raw quotes and extracts the final displayed digit using the selected symbol’s quote precision. Every minute it reviews fast, medium, and slow rolling windows. The default windows are 20, 50, and 200 ticks.

A review arms the Over 6 setup when digits 7, 8, and 9 meet the configured minimum combined share in the fast and medium windows, remain supported by the slow window, and have a higher average frequency per digit than digits 1–6. Because 7–9 contains three digits and 1–6 contains six, the comparison is `7–9 share ÷ 3` versus `1–6 share ÷ 6`. For example, 60% means 12 of 20 recent ticks ended in 7, 8, or 9; it does not mean each of the three digits individually appeared 60% of the time.

After arming, the bot waits for one lower digit from 0 through 6, then the next tick becomes eligible for a `DIGITOVER` contract with barrier `6`. When that contract finishes, the bot re-arms for another lower-tick confirmation if the most recent minute-reviewed condition is still valid. A later review below the configured threshold disarms it. The selected duration is one or two ticks; a two-tick contract settles on its final tick and is not two independent attempts.

The bot requests a fresh Deriv proposal for the actual stake and buy price. It validates the proposal and records ask price and payout, but it does not calculate or use an estimated-edge signal to decide whether to enter. The digit condition, confirmation sequence, account safeguards, and valid proposal control entry.

## Markets and controls

The sidebar loads active **derived indices only** from Deriv when credentials and an account are available. Forex, commodities, stocks, and crypto are excluded. A local indices-only catalogue remains available as a fallback. You can manually select the market, one- or two-tick duration, minimum 7–9 share, starting stake, recovery multiplier, maximum recovery steps up to 10, and a positive session take-profit target.

The strategy threshold defaults to **31%** and is displayed as an integer percentage. This means at least 31 out of every 100 recent ticks in the relevant window end in 7, 8, or 9; the dashboard also shows the exact count and the per-digit averages. The recovery multiplier defaults to **1.10**, with up to **10** selectable recovery steps, and can be disabled for fixed stake. There is no session loss-stop. The default positive take-profit target is one account-currency unit; setting it to zero disables only that target. The daily filled-trade cap, cooldowns, ambiguous-settlement stop, and real-account confirmation remain active.

## Safety behavior

Demo accounts are allowed to trade virtual funds. Real accounts remain blocked unless the account is recognized as real and `LIVE` is typed exactly in the sidebar. Unknown account types remain monitoring-only. If a buy or settlement cannot be confirmed, the trade is classified as unknown and the engine stops for manual statement verification.

## Setup

Install the dependencies in `requirements.txt`, provide `DERIV_APP_ID` and `DERIV_API_TOKEN` through Streamlit Secrets or the project environment, and start the dashboard with:

```bash
streamlit run dashboard.py
```

The first recommended run is monitoring or demo mode. The dashboard makes the 31% share, exact counts, per-digit averages, recovery steps, and positive take-profit target explicit. The journal records every minute review, rolling digit counts, lower-tick confirmation, proposal ask/payout, execution result, and settlement outcome in the `logs` directory.

## Project structure

| File | Purpose |
|---|---|
| `dashboard.py` | Streamlit controls, market selector, digit review display, trade ledger, journal download, and safety controls. |
| `src/digit_strategy.py` | Rolling digit counts, minute review cadence, 7–9 versus 1–6 condition, lower-tick confirmation, and signal state machine. |
| `src/trading_engine.py` | Deriv connection, tick subscription, proposal/buy flow, repeated-entry state, recovery sizing, settlement monitoring, and reconnection. |
| `src/api_client.py` | Current Deriv account, market, tick, proposal, buy, and open-contract requests. |
| `src/state_manager.py` | Thread-safe runtime state, trade accounting, cooldowns, and recovery state. |
| `src/journal.py` | Append-only decision and outcome journal with digit-specific fields. |
| `EXPECTED_BEHAVIOR.md` | Exact operational behavior and entry-state sequence. |

The lightweight build intentionally excludes the former candle-indicator strategy, candle chart pages, AI brain/advisory modules, Plotly dependency, Supabase mirror, and candle-learning backtests. These components do not participate in the selected-market digit execution path; journal backup and restore remain because they protect the digit evidence and settlement history.

This software is experimental and does not guarantee profit. A high recent 7–9 percentage can be useful as a hypothesis, but every contract must still be evaluated against the live quote, tested out of sample, and operated with funds you can afford to lose.
