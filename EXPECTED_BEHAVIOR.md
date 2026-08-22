# Expected Behavior — MomentumMaster Digit

## Startup

When the dashboard opens, it loads the live Deriv account list if `DERIV_APP_ID` and `DERIV_API_TOKEN` are available. After an account is selected, it attempts to load active **derived indices only** from Deriv. Forex, commodities, stocks, and crypto are excluded. If the live catalogue cannot be loaded, the sidebar uses the local indices-only market list. Selecting a market does not place an order.

The dashboard shows numeric digit counts and rolling 7–9 versus 1–6 percentages only. It does not render the removed digit chart and does not fetch candle charts for the digit strategy.

## Review and entry sequence

The bot subscribes to raw ticks for the selected market and seeds its rolling buffer from recent tick history. It reviews the buffer once per minute. The default windows are 20, 50, and 200 ticks.

A review arms the Over 6 setup when all of the following are true:

| Gate | Default behavior |
|---|---|
| Minimum 7–9 share | At least 31% by default in the fast and medium windows. The dashboard shows this as `31%`, not `0.31`. |
| User comparison | The average 7–9 digit frequency (`7–9 share ÷ 3`) is greater than the average 1–6 digit frequency (`1–6 share ÷ 6`) in the fast and medium windows. |
| Slow support | The 200-tick window has at least 30% 7–9 and the average 7–9 digit frequency exceeds the average 1–6 digit frequency there. |
| Review cadence | One evaluation per minute bucket. Each qualifying review resets the lower-confirmation sequence and establishes the boundary at the actual timestamp when that review executes. |
| Lower confirmation | The review tick, any tick already received before the review callback, and any tick from an unreviewed new minute do not count. Only ticks with an epoch strictly greater than the actual review boundary count toward the configured number of consecutive digits from 0 through 6; the default remains one lower digit. A 7–9 digit after the boundary resets the sequence. |
| Entry tick | The final required lower digit itself queues the Over 6 entry immediately; there is no extra higher-digit gap. For `N=3`, a review executing at `38:00` requires eligible lower ticks at `38:01`, `38:02`, and `38:03`, with entry triggered on `38:03`. A signal left pending from the preceding review window is discarded at the new boundary rather than executed at `:00`. |

The default contract request is `DIGITOVER` with barrier `6`, duration `1` tick. The user can select `2` ticks. A two-tick contract settles on the last digit of its final expiry tick; it does not provide two separate chances.

The qualifying direction is Over 6. If the percentages do not qualify, it waits. Under 6 remains represented in the diagnostics and comparison logic but is not forced as a counter-trade. The lower digits are only an entry-timing trigger; they do not replace the 7–9 concentration condition. The final required lower digit is both the confirmation and the entry trigger. After a signal is consumed, the strategy is reserved for that execution and cannot queue another signal until the trade result is finalized. After a contract finishes, it re-arms for a new confirmation sequence only when the last minute-reviewed condition is still valid; the repeat sequence starts strictly after the post-settlement boundary, so prior ticks cannot satisfy it. A later invalid review disarms it.

## Quote and order sequence

After a signal is queued, the engine requests a fresh Deriv proposal and validates the proposal ID, ask price, payout, and contract response. Ask price and payout are recorded for audit and are used to submit the actual buy, but the bot does not estimate or gate on an edge percentage. A missing, invalid, unsupported, or rejected proposal prevents the buy and is recorded as a cancellation; it does not count as a successful entry.

## Recovery behavior

The digit profile defaults to a 1.10 recovery multiplier with a maximum of **10 recovery steps**. The sequence from a 1.00 starting stake is approximately 1.00 → 1.10 → 1.21 → 1.33 and continues through step 10; a win resets to the starting stake. Recovery never opens a second trade while the first is unresolved, and a loss result is applied before any later stake is read. The user can disable recovery and use fixed stake.

The bot has no session loss-stop. The default session take-profit target is **1.00** in account currency; when positive session P&L reaches that target, the bot stops before a new entry. Setting the target to zero disables only the take-profit target. The daily filled-trade cap remains 10. Cooldowns are 30 seconds after normal outcomes, 90 seconds after one consecutive loss, and 180 seconds after two or more consecutive losses. Every entry passes both the local and shared account/market cooldown gates; a cooldown rejection cannot be bypassed by recovery or a second dashboard session. In addition, each account/market pair permits at most one entry attempt per UTC minute. A second same-minute signal is blocked as a duplicate, even if the first proposal was cancelled or the first contract has already settled, and is not mislabeled as ordinary cooldown.

## Account safety

Demo accounts can place virtual-fund orders. Real accounts remain blocked unless the account is recognized as real and `LIVE` is typed exactly. Unknown account types are monitoring-only. A connection error, invalid proposal, unsupported digit contract, or quote rejection does not silently become a buy.

If a buy receipt or contract settlement cannot be confirmed, the position is classified as `UNKNOWN`, the recovery plan is not advanced, and the engine stops for manual statement verification. The dashboard watchdog does not restart after this manual-intervention stop.

## Journaling

Every minute review is recorded, including stand-asides. Digit records include the selected index, strategy mode, barrier, tick duration, quote precision, exact rolling counts, combined 7–9 and 1–6 percentages, per-digit average percentages, group and per-digit dominance, review timestamp and boundary epoch, required and observed lower-confirmation count, lower confirmation digit, entry digit, confirmation tick epoch, proposal ask/payout, and rejection reason. Shared reservation rejections identify duplicate, cooldown, daily-cap, or unresolved-settlement protection.

Settled trades merge their outcome, P&L, stake, recovery step, contract ID, and execution mode into the corresponding signal row. The append-only archive remains the master record for backup and historical review.

## Validation status

The delivered source has been statically compiled, the digit state machine has been tested for minute cadence, strict post-review boundary timing, N=1/2/3 lower-tick confirmation, cross-review sequence reset, higher-digit reset, repeated entry while the condition remains valid, disarming below the threshold, quote precision, and no-arm behavior, the fake-client integration path has been tested for `DIGITOVER`, tick duration, proposal validation, buying, settlement, and 1.10 recovery sizing, the journal merge has been tested, shared coordination has been tested for same-market exclusivity, same-minute attempt blocking, cooldown, recovery stake, and daily-cap sharing, and the Streamlit health endpoint has responded successfully in headless mode.

This guide describes the implemented behavior; it is not a promise of profitability. The first real-world run should use a demo account, fixed stake, and a sufficiently long journal sample before enabling recovery sizing or real orders. The positive take-profit is an entry stop, not a guarantee that every open contract will settle profitably.
