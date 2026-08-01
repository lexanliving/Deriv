## Offline Learning Loop

MomentumMaster TF now includes an **offline learning loop**.

This is deliberately conservative.

The bot **does not** rewrite its own strategy at runtime.  
It records decisions, then a human reviews the evidence offline and proposes changes that must be forward-tested on demo before being manually opted into.

### New page

A new Streamlit page is available:

- `pages/research.py` → **Research Loop**

It appears automatically in the sidebar.

It has four read-only tabs:

1. **SEND TO Q**
   - Download the full learning bundle.
   - Download `postmortem.json`.
   - Read the plain-language lenses:
     - avoidable losses → duration / exit
     - fragile wins → entry timing
     - gatekeeper factor → the one gate to re-test
     - edges → symbol / hour / regime selection

2. **BACKUP**
   - Export the master archive CSV.
   - Export the merged JSON view.
   - Import a backup idempotently.
   - Re-importing the same file adds nothing the second time.

3. **GATE BACKTEST**
   - Non-destructive offline replay of recorded reviews.
   - Compare weight variants and thresholds against the real AS-RECORDED baseline.
   - Export a plain-text preset proposal.
   - Nothing is auto-applied.

4. **MISSED & AVOIDABLE**
   - Avoidable losses table.
   - Fragile wins table.
   - Gatekeeper factor chart.
   - Edge tables by symbol, hour, and regime.

### Flight recorder

When a trade is taken, the engine appends one compact line to:

```text
logs/trade_snapshots.jsonl
