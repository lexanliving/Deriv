# MomentumMaster Digit

MomentumMaster Digit is a Streamlit terminal for Deriv digit contracts. It supports manual selection from the live market catalogue, rolling last-digit reviews, and automated **Over 6** entries for one- or two-tick contracts.

## Strategy behavior

The bot collects raw quotes and extracts the final displayed digit using the selected symbol’s quote precision. Every minute it reviews fast, medium, and slow rolling windows. The default windows are 20, 50, and 200 ticks.

A review arms the Over 6 setup only when digits 7, 8, and 9 have at least the configured minimum share, exceed the combined appearance of digits 1–6 in the fast and medium windows, and remain supported by the slow window. After the review arms, the bot waits for one lower digit from 0 through 6. The next tick becomes eligible for a `DIGITOVER` contract with barrier `6`. The selected duration is one or two ticks; a two-tick contract settles on its final tick and is not two independent attempts.

The bot does not alternate blindly between Over and Under. It uses the actual live proposal quote and calculates:

```text
break_even_probability = ask_price / payout
estimated_edge = estimated_probability - break_even_probability
```

With quote-aware filtering enabled, the bot refuses the entry unless the estimated edge meets the configured minimum. This prevents a headline return percentage from being treated as a guaranteed advantage.

## Markets and controls

The sidebar loads all currently active markets from Deriv when credentials and an account are available. A broad local catalogue remains available as a fallback. You can manually select the market, one- or two-tick duration, minimum 7–9 share, quote-edge threshold, starting stake, recovery multiplier, maximum recovery steps, and optional session-loss stop.

The recovery multiplier accepts values starting at **1.01**, including the requested **1.10**. The digit profile defaults to a mild **1.10 multiplier** with a maximum of three recovery steps, and it can be disabled from the sidebar for fixed-stake testing. The session-loss stop, daily filled-trade cap, cooldowns, ambiguous-settlement stop, and real-account confirmation remain active. Fixed-stake demo or paper validation is still the safest way to verify the signal before using recovery sizing.

## Safety behavior

Demo accounts are allowed to trade virtual funds. Real accounts remain blocked unless the account is recognized as real and `LIVE` is typed exactly in the sidebar. Unknown account types remain monitoring-only. If a buy or settlement cannot be confirmed, the trade is classified as unknown and the engine stops for manual statement verification.

## Setup

Install the dependencies in `requirements.txt`, provide `DERIV_APP_ID` and `DERIV_API_TOKEN` through Streamlit Secrets or the project environment, and start the dashboard with:

```bash
streamlit run dashboard.py
```

The first recommended run is monitoring or paper/demo mode with quote-aware filtering enabled, fixed stake, and zero recovery steps. The journal records every minute review, rolling digit counts, lower-tick confirmation, quote fields, estimated edge, execution result, and settlement outcome in the `logs` directory.

## Project structure

| File | Purpose |
|---|---|
| `dashboard.py` | Streamlit controls, market selector, digit review display, trade ledger, journal download, and safety controls. |
| `src/digit_strategy.py` | Rolling digit counts, minute review cadence, 7–9 versus 1–6 condition, lower-tick confirmation, and signal state machine. |
| `src/trading_engine.py` | Deriv connection, tick subscription, proposal/buy flow, quote-aware gate, recovery sizing, settlement monitoring, and reconnection. |
| `src/api_client.py` | Current Deriv account, market, tick, proposal, buy, and open-contract requests. |
| `src/state_manager.py` | Thread-safe runtime state, trade accounting, cooldowns, and recovery state. |
| `src/journal.py` | Append-only decision and outcome journal with digit-specific fields. |
| `EXPECTED_BEHAVIOR.md` | Exact operational behavior and entry-state sequence. |

The lightweight build intentionally excludes the former candle-indicator strategy, candle chart pages, AI brain/advisory modules, Plotly dependency, Supabase mirror, and candle-learning backtests. These components do not participate in the selected-market digit execution path; journal backup and restore remain because they protect the digit evidence and settlement history.

This software is experimental and does not guarantee profit. A high recent 7–9 percentage can be useful as a hypothesis, but every contract must still be evaluated against the live quote, tested out of sample, and operated with funds you can afford to lose.
