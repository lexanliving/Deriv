"""src/brain_kb.py — the brain's memory, retrieval, deep analytics, and grounding.

Reads the LIVE Deriv journal (25-point / 10-factor MomentumMaster TF engine) and
turns it into a deep, schema-correct post-mortem that the LLM is forced to
ground on. Also owns the trainable memory (lessons + document library), the
grounding/focus builder, the honest gate-backtest, and deterministic
lesson-proposals.

This module is READ-ONLY w.r.t. live trading and never imports the LLM client.
"""
from __future__ import annotations

import json
import os
import re
import threading
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from config import LOG_DIR
from src.journal import get_journal
from src.logger import get_logger

logger = get_logger("brain_kb")

LESSONS_PATH = os.path.join(LOG_DIR, "brain_lessons.jsonl")
DOCS_PATH = os.path.join(LOG_DIR, "brain_docs.jsonl")
_LESSONS_LOCK = threading.Lock()
_DOCS_LOCK = threading.Lock()
_STOP = {"the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "is", "it", "my",
         "i", "was", "were", "with", "that", "this", "but", "not", "are", "be", "we",
         "you", "as", "at", "so", "if", "do", "does", "did", "has", "have", "had"}

# --- the 10 confluence factors exactly as the live Deriv engine scores them ---
# (journal column, short name, max integer score)
FACTORS: List[Tuple[str, str, int]] = [
    ("s_trend", "trend", 5), ("s_trigger", "trigger", 3), ("s_momentum", "momentum", 3),
    ("s_volatility", "volatility", 2), ("s_alignment", "alignment", 1), ("s_adx", "adx", 3),
    ("s_macd", "macd", 2), ("s_rsi_zone", "rsi_zone", 2), ("s_pattern", "pattern", 2),
    ("s_structure", "structure", 2),
]
FACTOR_KEYS = [k for k, _, _ in FACTORS]
SHORT_KEYS = [s for _, s, _ in FACTORS]
FACTOR_MAX = {s: m for _, s, m in FACTORS}
SHORT_TO_COL = {s: k for k, s, _ in FACTORS}
DEFAULT_WEIGHTS = {s: 1 for s in SHORT_KEYS}  # multipliers on stored sub-scores; 1 = as-built

# Backtest preset library (multipliers; missing keys default to 1).
PRESETS: Dict[str, Dict[str, int]] = {
    "as-built (recorded)": dict(DEFAULT_WEIGHTS),
    "trend & structure": {"trend": 2, "structure": 2, "alignment": 2},
    "execution (trigger/momentum/pattern)": {"trigger": 2, "momentum": 2, "pattern": 2},
    "momentum & oscillators": {"momentum": 2, "macd": 2, "rsi_zone": 2},
}
THRESHOLD_OPTIONS: List[int] = [13, 16, 18, 20, 22]

# Map a hard-gate rejection string (from src/strategy.py) to a named gate.
GATE_PATTERNS: List[Tuple[str, str]] = [
    ("trend agreement", "no trend agreement"),
    ("EMA flat", "EMAs too flat"),
    ("entry-tf ADX", "entry-tf ADX"),
    ("vol too quiet", "too quiet"),
    ("vol spiking", "volatility spiking"),
    ("trigger break", "no trigger break"),
    ("close vs EMA", "not beyond fast EMA"),
    ("exhaustion", "move exhausted"),
    ("divergence", "divergence"),
    ("structure", "structure broken"),
    ("trigger body", "trigger candle too weak"),
    ("5m alignment", "5m not aligned"),
    ("5m trending", "5m not actually trending"),
    ("1h structure", "1h market structure not intact"),
    ("1h ADX", "1h ADX below"),
    ("1h MACD", "1h MACD not aligned"),
    ("score < threshold", "below threshold"),
]

# Common aliases so a chat like "how is gold doing?" resolves to the journal code.
SYMBOL_ALIASES: Dict[str, str] = {
    "gold": "frxXAUUSD", "xauusd": "frxXAUUSD", "xau": "frxXAUUSD",
    "silver": "frxXAGUSD", "xagusd": "frxXAGUSD",
    "eurusd": "frxEURUSD", "eur": "frxEURUSD",
    "gbpusd": "frxGBPUSD", "gbp": "frxGBPUSD", "cable": "frxGBPUSD",
    "usdjpy": "frxUSDJPY", "jpy": "frxUSDJPY", "yen": "frxUSDJPY",
    "audusd": "frxAUDUSD", "aud": "frxAUDUSD",
    "usdchf": "frxUSDCHF", "chf": "frxUSDCHF",
    "usdcad": "frxUSDCAD", "cad": "frxUSDCAD",
    "nzdusd": "frxNZDUSD", "nzd": "frxNZDUSD",
    "v10": "1HZ10V", "v25": "1HZ25V", "v50": "1HZ50V", "v75": "1HZ75V", "v100": "1HZ100V",
    "vol75": "1HZ75V", "vol100": "1HZ100V",
}
try:
    from config import AVAILABLE_MARKETS
    for _disp, _code in AVAILABLE_MARKETS.items():
        SYMBOL_ALIASES[_code.lower()] = _code
        for _w in re.findall(r"[a-z0-9]+", _disp.lower()):
            if len(_w) > 2:
                SYMBOL_ALIASES.setdefault(_w, _code)
except Exception:
    pass

RULEBOOK = """You are the Trading Brain for "MomentumMaster TF", a Deriv binary-options
(Up/Down = Call/Put) higher-timeframe trend bot. You ADVISE only; you NEVER change the
live strategy. Any parameter change must be a PROPOSAL the human validates with the
gate-backtest (the Lab tab) before opting in.

HOW THE LIVE ENGINE WORKS (ground your advice in this):
- SCORING out of 25 = sum of ten integer sub-scores, each in [0, max]:
  trend(5) trigger(3) momentum(3) volatility(2) alignment(1) adx(3) macd(2)
  rsi_zone(2) pattern(2) structure(2).
- HARD GATES (must ALL pass; a high score cannot override any): higher-timeframe
  trend agreement for the contract length; EMAs not flat; entry-tf ADX floor (except
  1m scalps); volatility within band; a real trigger break of the prior candle; close
  beyond the fast EMA; the express-aware exhaustion limit; no RSI/price divergence;
  entry-tf structure >= 1; plus regime gates (SHORT needs a decisive trigger body and
  a trending 5m; LONG needs intact 1h structure + 1h ADX floor + 1h MACD aligned).
- DURATION-AWARE TRIGGER: 1m/2m -> 1m candle; 5m/15m -> 5m; 30m/60m -> 15m. Trend
  confirmation is always 30m + 1h. Signals fire only on a closed trigger candle.
- SELECTIVITY PRESETS (score threshold / entry ADX floor): Conservative 20/18,
  Balanced 16/15, Aggressive 13/12.
- MARTINGALE recovers losses by stepping the stake; it can amplify both recovery and
  damage, so judge it by the per-step win rate / P&L in the grounding.

HOW TO READ THE GROUNDING NUMBERS (the post-mortem I provide):
- "score separates winners" = avg score of WON minus avg score of LOST. If near zero,
  the 25-pt score is NOT discriminating -> a re-weight / threshold review is warranted.
- "avoidable losses" = LOST trades that were IN PROFIT at some point (MFE>0 and
  MFE>MAE*0.5). The ENTRY was right; the HOLD/DURATION gave it back. Recommend a
  shorter contract or banking at 1R for these — NOT an entry change.
- "fragile wins" = WON trades that took a full-risk scare (MAE>MFE*0.5). Lucky, not
  robust -> review entry timing / stop sitting in candle noise.
- "gatekeeper" = the factor most often weakest on near-miss stand-asides in a trending
  market -> the prime re-calibration candidate (test it in the Lab, don't change live).
- "hard-gate funnel" = which hard gate blocks most reviews. "trigger break" dominating
  on a ranging day is NORMAL; a structure/EMA gate dominating in a trend means the trend
  filter may be mis-calibrated for current conditions.
- A blank/quiet day is legitimate; if a slice has <5 samples, say "insufficient data"
  rather than opine.

OUTPUT DISCIPLINE: cite only numbers present in the grounding. When evidence is strong
you MAY emit exactly ONE proposal as a fenced ```json block using this schema (weights
are MULTIPLIERS on the stored sub-scores, 1 = unchanged; threshold an int in
[13,16,18,20,22]):
{"type":"preset","name":"brain-<short>","weights":{"trend":1,"trigger":1,"momentum":1,
 "volatility":1,"alignment":1,"adx":1,"macd":1,"rsi_zone":1,"pattern":1,"structure":1},
 "threshold":20,"rationale":"..."}
Never emit more than one proposal. If evidence is weak, say "no change warranted — hold
the line" and name the specific data to collect. Never tell the user to edit live code."""

SYNTHESIS_PROMPT = """Produce today's trading-brain synthesis as a structured report. Use
markdown ## headers for each section below, in this order, and keep every section tight
and number-cited (pull numbers ONLY from the grounding post-mortem I provided):

## Headline
One or two sentences: the current state of the edge (win rate, PF, expectancy, whether the
score is separating winners, and whether performance is improving or decaying vs the
rolling last-20 window).

## Biggest leak
Name the single largest source of lost money with its number, and classify the lever:
ENTRY (wrong direction/timing), EXIT/DURATION (was in profit then reversed — cite the
avoidable-loss fraction), or SELECTION (a symbol/duration/hour/regime that is negative
expectancy — cite it).

## What is working
The strongest positive edge with its number (a factor with positive edge, a profitable
regime/duration/symbol, or the express/structure behaviour if the data supports it).

## Recalibration candidate
The gatekeeper factor or the weakest score-separation signal, phrased as ONE testable
hypothesis for the Lab backtest (not a live change). Cite the number.

## Session & selection edges
List the winning and losing buckets by hour, weekday, symbol, duration, and regime that
have >=5 samples, each with win rate and P&L. Mark any bucket with <5 samples as
"insufficient data".

## Excursion read
State the avoidable-loss and fragile-win fractions and what each implies for exit vs
entry tuning. Cite avg MFE on wins and avg MAE on losses.

## Martingale read
One line on whether stepping up is recovering or digging deeper (cite per-step win rate).

## Two observations to remember
Two concrete, save-worthy lessons (these map to one-click saves in the UI). Each must
include the supporting number.

## Proposed change
If and only if the evidence is strong, emit exactly one ```json preset proposal
(multipliers + threshold). Otherwise write: "No change warranted — hold the line" and list
the 1-2 specific data points to collect before deciding.

Be specific, quantitative, and honest about uncertainty. Do not invent numbers."""


# --------------------------------------------------------------------------- #
#  tiny parsers                                                               #
# --------------------------------------------------------------------------- #
def _g(d: Dict[str, Any], k: str) -> str:
    v = d.get(k)
    return "" if v is None else str(v).strip()


def _f(d: Dict[str, Any], k: str) -> Optional[float]:
    v = _g(d, k)
    if v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _b(d: Dict[str, Any], k: str) -> bool:
    return _g(d, k).upper() == "TRUE"


def _row_ts(row: Dict[str, Any]):
    return pd.to_datetime(_g(row, "timestamp_utc"), utc=True, errors="coerce")


def _tokenize(text: str) -> List[str]:
    return [t for t in re.findall(r"[a-z0-9]+", (text or "").lower()) if t not in _STOP and len(t) > 1]


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "", (text or "").lower())
    return s[:10] or "x"


# --------------------------------------------------------------------------- #
#  slice statistics                                                           #
# --------------------------------------------------------------------------- #
def _slice_stats(closed_sub: List[Dict[str, Any]]) -> Dict[str, Any]:
    n = len(closed_sub)
    wins = sum(1 for r in closed_sub if _g(r, "outcome") == "WON")
    pnl = sum(_f(r, "pnl") or 0.0 for r in closed_sub)
    scores = [_f(r, "score") for r in closed_sub]
    scores = [x for x in scores if x is not None]
    return {
        "trades": n, "wins": wins,
        "win_rate": round(wins / n * 100.0, 1) if n else 0.0,
        "pnl": round(pnl, 2),
        "avg_score": round(sum(scores) / len(scores), 1) if scores else None,
    }


def _closed(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [r for r in rows if _g(r, "outcome") in ("WON", "LOST")]


# --------------------------------------------------------------------------- #
#  post-mortem                                                                #
# --------------------------------------------------------------------------- #
def compute_postmortem(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    rows = rows or []
    empty = {
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "n_reviews": 0, "n_taken": 0, "n_executed": 0, "n_closed": 0,
        "wins": 0, "losses": 0, "win_rate": 0.0, "taken_rate": 0.0,
        "net_pnl": 0.0, "profit_factor": 0.0, "expectancy": 0.0,
        "avg_win": 0.0, "avg_loss": 0.0, "payoff": 0.0,
        "best_trade": None, "worst_trade": None,
        "max_win_streak": 0, "max_loss_streak": 0, "cur_streak": 0, "cur_streak_kind": "-",
        "roll20_win_rate": 0.0,
        "score_wins_avg": None, "score_losses_avg": None, "score_gap": None,
        "score_separates": None,
        "avoidable": [], "avoidable_frac": 0.0, "fragile": [], "fragile_frac": 0.0,
        "avg_mfe_wins": None, "avg_mae_losses": None,
        "gate_funnel": {}, "factor_edge": [],
        "by_symbol": {}, "by_duration": {}, "by_regime": {}, "by_hour": {}, "by_weekday": {},
        "by_step": {},
    }
    if not rows:
        return empty

    n_reviews = len(rows)
    n_taken = sum(1 for r in rows if _b(r, "taken"))
    n_executed = sum(1 for r in rows if _b(r, "executed"))
    closed = _closed(rows)
    n_closed = len(closed)
    wins = [r for r in closed if _g(r, "outcome") == "WON"]
    losses = [r for r in closed if _g(r, "outcome") == "LOST"]
    nw, nl = len(wins), len(losses)
    wr = (nw / n_closed * 100.0) if n_closed else 0.0
    net = sum(_f(r, "pnl") or 0.0 for r in closed)
    gw = sum(_f(r, "pnl") or 0.0 for r in wins)
    gl = abs(sum(_f(r, "pnl") or 0.0 for r in losses))
    pf = (gw / gl) if gl > 0 else (float("inf") if gw > 0 else 0.0)
    avg_w = (gw / nw) if nw else 0.0
    avg_l = (gl / nl) if nl else 0.0
    payoff = (avg_w / avg_l) if avg_l > 0 else 0.0
    expect = (wr / 100.0 * avg_w) - ((1 - wr / 100.0) * avg_l)

    pnl_vals = [(_f(r, "pnl") or 0.0) for r in closed]
    best = max(closed, key=lambda r: _f(r, "pnl") or -1e9) if closed else None
    worst = min(closed, key=lambda r: _f(r, "pnl") or 1e9) if closed else None

    # streaks over time-ordered closed trades
    ordered = sorted(closed, key=lambda r: _row_ts(r) if pd.notna(_row_ts(r)) else pd.Timestamp.min.tz_localize("UTC"))
    max_w = max_l = cur = 0
    kind = "-"
    cur_kind = "-"
    for r in ordered:
        oc = _g(r, "outcome")
        if oc == "WON":
            if cur_kind != "W":
                cur_kind, cur = "W", 0
            cur += 1
            max_w = max(max_w, cur)
        else:
            if cur_kind != "L":
                cur_kind, cur = "L", 0
            cur += 1
            max_l = max(max_l, cur)
    cur_streak, cur_streak_kind = cur, cur_kind

    roll20 = ordered[-20:]
    roll20_wr = (sum(1 for r in roll20 if _g(r, "outcome") == "WON") / len(roll20) * 100.0) if roll20 else 0.0

    sw = [_f(r, "score") for r in wins]
    sw = [x for x in sw if x is not None]
    sl = [_f(r, "score") for r in losses]
    sl = [x for x in sl if x is not None]
    sw_avg = round(sum(sw) / len(sw), 2) if sw else None
    sl_avg = round(sum(sl) / len(sl), 2) if sl else None
    gap = round(sw_avg - sl_avg, 2) if (sw_avg is not None and sl_avg is not None) else None
    separates = (abs(gap) >= 1.0) if gap is not None else None

    # excursion analysis
    avoidable, fragile = [], []
    mfe_wins, mae_losses = [], []
    for r in wins:
        mae, mfe = _f(r, "mae"), _f(r, "mfe")
        if mfe is not None:
            mfe_wins.append(mfe)
        if mae is not None and mae > 0 and (mfe is None or mae > mfe * 0.5):
            fragile.append({"symbol": _g(r, "symbol"), "ts": _g(r, "timestamp_utc"),
                            "mae": round(mae, 5), "mfe": round(mfe, 5) if mfe is not None else None,
                            "pnl": _f(r, "pnl"), "regime": _g(r, "regime"), "dur": _g(r, "duration_min")})
    for r in losses:
        mae, mfe = _f(r, "mae"), _f(r, "mfe")
        if mae is not None:
            mae_losses.append(mae)
        if mfe is not None and mfe > 0 and (mae is None or mfe > mae * 0.5):
            avoidable.append({"symbol": _g(r, "symbol"), "ts": _g(r, "timestamp_utc"),
                              "mfe": round(mfe, 5), "mae": round(mae, 5) if mae is not None else None,
                              "pnl": _f(r, "pnl"), "regime": _g(r, "regime"), "dur": _g(r, "duration_min")})
    avoidable_frac = round(len(avoidable) / nl * 100.0, 1) if nl else 0.0
    fragile_frac = round(len(fragile) / nw * 100.0, 1) if nw else 0.0

    # hard-gate funnel (over stand-asides)
    funnel: Counter = Counter()
    for r in rows:
        if _b(r, "taken"):
            continue
        reason = _g(r, "rejection_reason").lower()
        if not reason:
            continue
        for label, sub in GATE_PATTERNS:
            if sub in reason:
                funnel[label] += 1
                break

    # factor edge table
    edge = []
    for col, short, mx in FACTORS:
        wv = [_f(r, col) for r in wins]
        wv = [x for x in wv if x is not None]
        lv = [_f(r, col) for r in losses]
        lv = [x for x in lv if x is not None]
        wa = round(sum(wv) / len(wv), 2) if wv else None
        la = round(sum(lv) / len(lv), 2) if lv else None
        ed = round(wa - la, 2) if (wa is not None and la is not None) else None
        edge.append({"factor": short, "win_avg": wa, "loss_avg": la, "edge": ed, "max": mx})

    # grouped slices (closed only)
    def _group(keyfn):
        g: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for r in closed:
            k = keyfn(r)
            if k is None or k == "":
                continue
            g[str(k)].append(r)
        return {k: _slice_stats(v) for k, v in g.items()}

    by_symbol = _group(lambda r: _g(r, "symbol"))
    by_duration = _group(lambda r: _g(r, "duration_min"))
    by_regime = _group(lambda r: _g(r, "regime"))
    by_hour = _group(lambda r: (f"{_row_ts(r).hour:02d}" if pd.notna(_row_ts(r)) else None))
    by_weekday = _group(lambda r: (_row_ts(r).day_name()[:3] if pd.notna(_row_ts(r)) else None))
    by_step = _group(lambda r: _g(r, "martingale_step"))

    return {
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "n_reviews": n_reviews, "n_taken": n_taken, "n_executed": n_executed, "n_closed": n_closed,
        "wins": nw, "losses": nl, "win_rate": round(wr, 1),
        "taken_rate": round(n_taken / n_reviews * 100.0, 1) if n_reviews else 0.0,
        "net_pnl": round(net, 2),
        "profit_factor": round(pf, 2) if pf != float("inf") else "inf",
        "expectancy": round(expect, 2), "avg_win": round(avg_w, 2), "avg_loss": round(avg_l, 2),
        "payoff": round(payoff, 2),
        "best_trade": round((_f(best, "pnl") or 0.0), 2) if best else None,
        "worst_trade": round((_f(worst, "pnl") or 0.0), 2) if worst else None,
        "max_win_streak": max_w, "max_loss_streak": max_l,
        "cur_streak": cur_streak, "cur_streak_kind": cur_streak_kind,
        "roll20_win_rate": round(roll20_wr, 1),
        "score_wins_avg": sw_avg, "score_losses_avg": sl_avg, "score_gap": gap, "score_separates": separates,
        "avoidable": avoidable[:10], "avoidable_frac": avoidable_frac,
        "fragile": fragile[:10], "fragile_frac": fragile_frac,
        "avg_mfe_wins": round(sum(mfe_wins) / len(mfe_wins), 5) if mfe_wins else None,
        "avg_mae_losses": round(sum(mae_losses) / len(mae_losses), 5) if mae_losses else None,
        "gate_funnel": dict(funnel.most_common()),
        "factor_edge": edge,
        "by_symbol": by_symbol, "by_duration": by_duration, "by_regime": by_regime,
        "by_hour": by_hour, "by_weekday": by_weekday, "by_step": by_step,
    }


# --------------------------------------------------------------------------- #
#  grounding text + focus slice                                               #
# --------------------------------------------------------------------------- #
def _fmt_slice(name: str, d: Dict[str, Any]) -> str:
    return (f"{name}: trades={d['trades']} wins={d['wins']} wr={d['win_rate']}% "
            f"pnl={d['pnl']:+.2f}" + (f" avg_score={d['avg_score']}" if d['avg_score'] is not None else ""))


def grounding_digest(pm: Dict[str, Any]) -> str:
    L = ["=== OVERVIEW ===",
         f"reviews={pm['n_reviews']} taken={pm['n_taken']} ({pm['taken_rate']}%) executed={pm['n_executed']} "
         f"closed={pm['n_closed']} (W{pm['wins']}/L{pm['losses']}) win_rate={pm['win_rate']}%",
         f"net_pnl={pm['net_pnl']:+.2f} profit_factor={pm['profit_factor']} expectancy={pm['expectancy']:+.2f} "
         f"payoff={pm['payoff']} avg_win={pm['avg_win']} avg_loss={pm['avg_loss']}",
         f"best={pm['best_trade']} worst={pm['worst_trade']} | streaks W{pm['max_win_streak']}/L{pm['max_loss_streak']} "
         f"current={pm['cur_streak']}{pm['cur_streak_kind']} | rolling_last20_wr={pm['roll20_win_rate']}%",
         f"score_separates_winners={pm['score_separates']} (wins_avg={pm['score_wins_avg']} vs "
         f"losses_avg={pm['score_losses_avg']}, gap={pm['score_gap']})"]
    L.append("=== EXCURSION ===")
    L.append(f"avoidable_losses={len(pm['avoidable'])} ({pm['avoidable_frac']}% of losses) | "
             f"fragile_wins={len(pm['fragile'])} ({pm['fragile_frac']}% of wins) | "
             f"avg_MFE_on_wins={pm['avg_mfe_wins']} avg_MAE_on_losses={pm['avg_mae_losses']}")
    L.append("=== FACTOR EDGE (win_avg - loss_avg; positive = factor discriminates winners) ===")
    for e in pm["factor_edge"]:
        L.append(f"  {e['factor']:9} win={e['win_avg']} loss={e['loss_avg']} edge={e['edge']} (max {e['max']})")
    L.append("=== HARD-GATE FUNNEL (what blocks stand-asides) ===")
    L.append("  " + (", ".join(f"{k}={v}" for k, v in pm["gate_funnel"].items()) or "none"))
    L.append("=== BY REGIME ===")
    L += ["  " + _fmt_slice(k, v) for k, v in sorted(pm["by_regime"].items(), key=lambda kv: kv[1]["pnl"])] or ["  none"]
    L.append("=== BY DURATION (minutes) ===")
    L += ["  " + _fmt_slice(k, v) for k, v in sorted(pm["by_duration"].items(), key=lambda kv: kv[1]["pnl"])] or ["  none"]
    L.append("=== BY SYMBOL ===")
    L += ["  " + _fmt_slice(k, v) for k, v in sorted(pm["by_symbol"].items(), key=lambda kv: kv[1]["pnl"])] or ["  none"]
    L.append("=== BY HOUR (UTC) ===")
    L += ["  " + _fmt_slice(k, v) for k, v in sorted(pm["by_hour"].items())] or ["  none"]
    L.append("=== BY WEEKDAY ===")
    L += ["  " + _fmt_slice(k, v) for k, v in sorted(pm["by_weekday"].items())] or ["  none"]
    L.append("=== BY MARTINGALE STEP ===")
    L += ["  " + _fmt_slice(k, v) for k, v in sorted(pm["by_step"].items())] or ["  none"]
    return "\n".join(L)


def _resolve_symbol(question: str) -> Optional[str]:
    q = question.lower()
    for alias, code in sorted(SYMBOL_ALIASES.items(), key=lambda kv: -len(kv[0])):
        if alias and alias in q:
            return code
    return None


def _resolve_duration(question: str) -> Optional[str]:
    q = " " + question.lower() + " "
    table = [(r"\b1\s*m(in(ute)?s?)?\b", "1"), (r"\b2\s*m(in(ute)?s?)?\b", "2"),
             (r"\b5\s*m(in(ute)?s?)?\b", "5"), (r"\b15\s*m(in(ute)?s?)?\b", "15"),
             (r"\b30\s*m(in(ute)?s?)?\b", "30"), (r"\b60\s*m(in(ute)?s?)?\b", "60"),
             (r"\bone[- ]minute\b", "1"), (r"\btwo[- ]minute\b", "2"), (r"\bfive[- ]minute\b", "5")]
    for pat, val in table:
        if re.search(pat, q):
            return val
    return None


def _resolve_hour(question: str) -> Optional[str]:
    q = question.lower()
    m = re.search(r"(\d{1,2})\s*:\s*00", q)
    if m:
        return f"{int(m.group(1)) % 24:02d}"
    m = re.search(r"(\d{1,2})\s*h\b", q)
    if m:
        return f"{int(m.group(1)) % 24:02d}"
    m = re.search(r"(\d{1,2})\s*(am|pm)", q)
    if m:
        h = int(m.group(1)) % 12
        if m.group(2) == "pm":
            h += 12
        return f"{h:02d}"
    return None


def _resolve_factor(question: str) -> Optional[str]:
    q = " " + question.lower() + " "
    for short in SHORT_KEYS:
        if re.search(r"\b" + re.escape(short) + r"\b", q):
            return short
    if "rsi" in q:
        return "rsi_zone"
    return None


def focus_slice(pm: Dict[str, Any], rows: List[Dict[str, Any]], question: str) -> str:
    parts: List[str] = []
    sym = _resolve_symbol(question)
    if sym:
        sub = [r for r in _closed(rows) if _g(r, "symbol") == sym]
        if sub:
            st_ = _slice_stats(sub)
            aside = [r for r in rows if _g(r, "symbol") == sym and not _b(r, "taken")]
            top_rej = Counter(_g(r, "rejection_reason") for r in aside if _g(r, "rejection_reason")).most_common(1)
            parts.append(f"SYMBOL {sym} -> " + _fmt_slice(sym, st_) +
                         (f" | top stand-aside reason: {top_rej[0][0]}" if top_rej else ""))
    dur = _resolve_duration(question)
    if dur and dur in pm["by_duration"]:
        parts.append("DURATION " + _fmt_slice(dur + "m", pm["by_duration"][dur]))
    hr = _resolve_hour(question)
    if hr and hr in pm["by_hour"]:
        parts.append("HOUR " + _fmt_slice(hr + ":00 UTC", pm["by_hour"][hr]))
    fac = _resolve_factor(question)
    if fac:
        row = next((e for e in pm["factor_edge"] if e["factor"] == fac), None)
        if row:
            parts.append(f"FACTOR {fac} -> win_avg={row['win_avg']} loss_avg={row['loss_avg']} edge={row['edge']}")
    if "regime" in question.lower():
        for k, v in pm["by_regime"].items():
            parts.append("REGIME " + _fmt_slice(k, v))
    return "\n".join(parts)


# --------------------------------------------------------------------------- #
#  memory: lessons + document library (jsonl, crash-safe, merge-by-id)        #
# --------------------------------------------------------------------------- #
def _read_jsonl(path: str) -> List[Dict[str, Any]]:
    if not os.path.exists(path):
        return []
    out: List[Dict[str, Any]] = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except Exception:
                        continue
    except Exception as exc:
        logger.warning("read %s failed: %s", path, exc)
    return out


def _append_jsonl(path: str, lock: threading.Lock, obj: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with lock:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(obj, ensure_ascii=False) + "\n")


def load_lessons() -> List[Dict[str, Any]]:
    return _read_jsonl(LESSONS_PATH)


def add_lesson(text: str, tags: Optional[List[str]] = None, source: str = "user", confirmed: bool = True) -> Dict[str, Any]:
    text = (text or "").strip()
    if not text:
        raise ValueError("lesson text is empty")
    lesson = {"id": uuid.uuid4().hex[:10],
              "ts_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
              "text": text, "tags": [str(t).strip() for t in (tags or []) if str(t).strip()],
              "source": source, "confirmed": bool(confirmed)}
    _append_jsonl(LESSONS_PATH, _LESSONS_LOCK, lesson)
    return lesson


def lessons_bytes() -> bytes:
    if not os.path.exists(LESSONS_PATH):
        return b""
    with open(LESSONS_PATH, "rb") as fh:
        return fh.read()


def import_lessons(data: bytes) -> Dict[str, int]:
    stats = {"added": 0, "skipped": 0, "errors": 0}
    have = {l.get("id") for l in load_lessons()}
    text = data.decode("utf-8", "replace")
    items: List[Any] = []
    try:
        parsed = json.loads(text)
        items = parsed if isinstance(parsed, list) else [parsed]
    except Exception:
        for line in text.splitlines():
            line = line.strip()
            if line:
                try:
                    items.append(json.loads(line))
                except Exception:
                    stats["errors"] += 1
    os.makedirs(os.path.dirname(LESSONS_PATH), exist_ok=True)
    with _LESSONS_LOCK:
        with open(LESSONS_PATH, "a", encoding="utf-8") as fh:
            for it in items:
                if not isinstance(it, dict) or not it.get("text"):
                    stats["errors"] += 1
                    continue
                lid = it.get("id") or uuid.uuid4().hex[:10]
                if lid in have:
                    stats["skipped"] += 1
                    continue
                it["id"] = lid
                fh.write(json.dumps(it, ensure_ascii=False) + "\n")
                have.add(lid)
                stats["added"] += 1
    return stats


def list_documents() -> List[Dict[str, Any]]:
    rows = _read_jsonl(DOCS_PATH)
    by_title: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        t = r.get("title", "untitled")
        d = by_title.setdefault(t, {"title": t, "chunks": 0, "chars": 0, "ts_utc": r.get("ts_utc", "")})
        d["chunks"] += 1
        d["chars"] += len(r.get("text", ""))
    return sorted(by_title.values(), key=lambda d: d["ts_utc"], reverse=True)


def add_document(text: str, title: str = "") -> int:
    title = (title or "untitled").strip()
    paras = [p.strip() for p in re.split(r"\n\s*\n", text or "") if p.strip()] or ([text.strip()] if text and text.strip() else [])
    doc_id = uuid.uuid4().hex[:10]
    chunks: List[Dict[str, Any]] = []
    buf, buf_len, idx = [], 0, 0
    for p in paras:
        if buf_len + len(p) > 1400 and buf:
            chunks.append({"id": f"{doc_id}-{idx}", "title": title, "text": "\n\n".join(buf),
                           "ts_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")})
            idx += 1
            buf, buf_len = [], 0
        buf.append(p)
        buf_len += len(p) + 2
    if buf:
        chunks.append({"id": f"{doc_id}-{idx}", "title": title, "text": "\n\n".join(buf),
                       "ts_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")})
    if not chunks:
        return 0
    os.makedirs(os.path.dirname(DOCS_PATH), exist_ok=True)
    with _DOCS_LOCK:
        with open(DOCS_PATH, "a", encoding="utf-8") as fh:
            for c in chunks:
                fh.write(json.dumps(c, ensure_ascii=False) + "\n")
    return len(chunks)


def docs_bytes() -> bytes:
    if not os.path.exists(DOCS_PATH):
        return b""
    with open(DOCS_PATH, "rb") as fh:
        return fh.read()


def import_kb(data: bytes, title: str = "") -> int:
    text = data.decode("utf-8", "replace")
    title = (title or "imported").strip()
    if text.lstrip().startswith("{") or text.lstrip().startswith("["):
        try:
            rows = json.loads(text)
            if isinstance(rows, list):
                n = 0
                for r in rows:
                    if isinstance(r, dict) and r.get("text"):
                        n += add_document(r["text"], r.get("title", title))
                return n
        except Exception:
            pass
    return add_document(text, title)


def kb_markdown_bytes() -> bytes:
    lines = ["# Trading Brain — memory + knowledge corpus", "## Lessons"]
    for l in load_lessons():
        tags = ",".join(l.get("tags", [])) or "-"
        lines.append(f"- [{l.get('ts_utc','')[:10]}|{tags}|{l.get('source','')}] {l.get('text','')}")
    lines.append("\n## Library documents")
    for r in _read_jsonl(DOCS_PATH):
        lines.append(f"\n### {r.get('title','untitled')} ({r.get('id','')})\n{r.get('text','')}")
    return ("\n".join(lines)).encode("utf-8")


def retrieve(question: str, k_lessons: int = 6, k_docs: int = 4) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    qt = set(_tokenize(question))
    if not qt:
        return load_lessons()[-k_lessons:], _read_jsonl(DOCS_PATH)[-k_docs:]
    lessons = load_lessons()
    lscored = sorted([(_score(qt, l.get("text", ""), l.get("tags", [])), -i, l)
                      for i, l in enumerate(lessons)], key=lambda x: (x[0], x[1]), reverse=True)
    chosen_l = [l for s, _, l in lscored if s > 0][:k_lessons] or lessons[-k_lessons:]
    docs = _read_jsonl(DOCS_PATH)
    dscored = sorted([(_score(qt, d.get("text", "") + " " + d.get("title", ""), []), -i, d)
                      for i, d in enumerate(docs)], key=lambda x: (x[0], x[1]), reverse=True)
    chosen_d = [d for s, _, d in dscored if s > 0][:k_docs]
    return chosen_l, chosen_d


def _score(qt: set, text: str, tags: List[str]) -> int:
    tt = set(_tokenize(text))
    s = len(qt & tt)
    s += 3 * sum(1 for tag in tags if tag.lower() in qt)
    return s


# --------------------------------------------------------------------------- #
#  grounding builder                                                          #
# --------------------------------------------------------------------------- #
def _recent_text(rows: List[Dict[str, Any]], n: int = 12) -> str:
    out = []
    for r in rows[-n:]:
        out.append(f"{_g(r,'timestamp_utc')[:16]} {_g(r,'symbol')} {_g(r,'direction')} "
                   f"trend={_g(r,'trend')} reg={_g(r,'regime')} score={_g(r,'score')}/{_g(r,'threshold')} "
                   f"taken={_g(r,'taken')} -> {_g(r,'outcome') or 'aside'} pnl={_g(r,'pnl')} "
                   f"rej={_g(r,'rejection_reason')}")
    return "\n".join(out)


def build_messages(question: str, pm: Dict[str, Any], rows: List[Dict[str, Any]],
                   include_recent: bool = True, focus: str = "") -> List[Dict[str, str]]:
    lessons, docs = retrieve(question)
    lesson_block = "\n".join(f"- [{l.get('ts_utc','')[:10]}|{','.join(l.get('tags',[])) or '-'}] {l.get('text','')}"
                             for l in lessons) or "(none yet — save observations in the Memory tab)"
    doc_block = "\n".join(f"- [{d.get('title','')}] {d.get('text','')[:600]}" for d in docs) or "(library empty)"
    system = (RULEBOOK + "\n\n=== GROUNDING (live post-mortem — cite these numbers; do not invent) ===\n"
              + grounding_digest(pm))
    if focus:
        system += "\n\n=== FOCUS (the user's question is about this slice — answer with these specifics) ===\n" + focus
    system += ("\n\n=== YOUR LESSONS (most relevant) ===\n" + lesson_block +
               "\n\n=== KNOWLEDGE LIBRARY (most relevant chunks) ===\n" + doc_block)
    if include_recent:
        system += "\n\n=== RECENT REVIEWS ===\n" + _recent_text(rows)
    return [{"role": "system", "content": system}, {"role": "user", "content": question}]


# --------------------------------------------------------------------------- #
#  proposal parsing + preset export                                           #
# --------------------------------------------------------------------------- #
_PROPOSAL_RE = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)


def find_proposal(text: str) -> Optional[Dict[str, Any]]:
    m = _PROPOSAL_RE.search(text or "")
    if not m:
        return None
    try:
        obj = json.loads(m.group(1))
    except Exception:
        return None
    if not isinstance(obj, dict) or obj.get("type") != "preset" or not isinstance(obj.get("weights"), dict):
        return None
    w = obj["weights"]
    weights = {s: max(0, int(w.get(s, 1))) for s in SHORT_KEYS}
    try:
        thr = int(obj.get("threshold", 20))
    except Exception:
        thr = 20
    thr = max(min(thr, 25), 0)
    return {"name": str(obj.get("name", "brain-proposal")), "weights": weights,
            "threshold": thr, "rationale": str(obj.get("rationale", ""))}


def preset_text(name: str, weights: Dict[str, int], threshold: int, rationale: str = "") -> str:
    body = (f"# Strategy preset: {name}\n"
            f"# Generated by the Trading Brain. VALIDATE in the Lab backtest on paper before using.\n"
            f"# Weights are MULTIPLIERS on the stored sub-scores (1 = unchanged).\n"
            f"# Opt in: add as a new entry in config.STRATEGY_SENSITIVITY_PRESETS, then select it.\n"
            f"THRESHOLD = {int(threshold)}\nWEIGHTS = {weights}\n")
    if rationale:
        body += f"# Rationale: {rationale}\n"
    return body


# --------------------------------------------------------------------------- #
#  honest gate-backtest                                                       #
# --------------------------------------------------------------------------- #
def _raw_sum(row: Dict[str, Any]) -> int:
    total = 0
    for col, short, mx in FACTORS:
        v = _f(row, col)
        if v is not None:
            total += int(round(v))
    return total


def _reweighted(row: Dict[str, Any], weights: Dict[str, int]) -> int:
    total = 0
    for col, short, mx in FACTORS:
        v = _f(row, col)
        if v is not None:
            total += int(round(v * float(weights.get(short, 1))))
    return total


def baseline(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    taken = [r for r in rows if _b(r, "taken")]
    nw = sum(1 for r in taken if _g(r, "outcome") == "WON")
    pnl = sum(_f(r, "pnl") or 0.0 for r in taken if _g(r, "outcome") in ("WON", "LOST"))
    return {"variant": "as-built (recorded)", "threshold": "recorded", "scored_rows": len(taken),
            "kept": len(taken), "kept_pnl": round(pnl, 2),
            "kept_win_rate": round(nw / len(taken) * 100.0, 1) if taken else 0.0,
            "dropped": 0, "dropped_pnl": 0.0, "dropped_losses_avoided": 0,
            "dropped_wins_lost": 0, "added_unknown": 0}


def backtest(rows: List[Dict[str, Any]], weights: Dict[str, int], threshold: int) -> Dict[str, Any]:
    kept = kept_w = dropped = dropped_loss = dropped_win = added = scored = 0
    kept_pnl = dropped_pnl = 0.0
    for r in rows:
        taken = _b(r, "taken")
        outcome = _g(r, "outcome")
        pnl = _f(r, "pnl") or 0.0
        raw = _raw_sum(r)
        conf = _reweighted(r, weights)
        if raw == 0 and not taken:
            continue  # hard-gate fail: re-weighting the score cannot fix it; never count as added
        scored += 1
        take_new = conf >= threshold
        if take_new and taken:
            kept += 1
            if outcome in ("WON", "LOST"):
                kept_pnl += pnl
                if outcome == "WON":
                    kept_w += 1
        elif take_new and not taken:
            added += 1  # soft score-rejection the variant would now fire (outcome unknown)
        elif (not take_new) and taken:
            dropped += 1
            if outcome in ("WON", "LOST"):
                dropped_pnl += pnl
                if outcome == "LOST":
                    dropped_loss += 1
                elif outcome == "WON":
                    dropped_win += 1
    return {"variant": "proposed", "threshold": int(threshold), "scored_rows": scored,
            "kept": kept, "kept_pnl": round(kept_pnl, 2),
            "kept_win_rate": round(kept_w / kept * 100.0, 1) if kept else 0.0,
            "dropped": dropped, "dropped_pnl": round(dropped_pnl, 2),
            "dropped_losses_avoided": dropped_loss, "dropped_wins_lost": dropped_win,
            "added_unknown": added}


# --------------------------------------------------------------------------- #
#  deterministic, number-cited lesson proposals                               #
# --------------------------------------------------------------------------- #
def propose_lessons(pm: Dict[str, Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []

    def _add(text: str, tags: List[str]):
        out.append({"text": text, "tags": tags})

    if pm["n_closed"] == 0 and pm["n_reviews"] > 0:
        _add(f"No settled trades yet across {pm['n_reviews']} reviews — the gates are doing their job waiting; keep collecting.",
             ["data", "patience"])
        return out

    if pm["score_separates"] is False and pm["n_closed"] >= 10:
        _add(f"Score is NOT separating winners (wins avg {pm['score_wins_avg']} vs losses avg "
             f"{pm['score_losses_avg']}, gap {pm['score_gap']}). The 25-pt score may need re-weighting or the "
             f"threshold is mis-set — test variants in the Lab backtest before trusting selectivity.",
             ["score", "calibration"])

    if pm["n_closed"] >= 10 and pm["win_rate"] >= 55:
        _add(f"Edge is real this window (win rate {pm['win_rate']}% over {pm['n_closed']} closed) — trust the "
             f"selectivity and resist over-tuning.", ["edge", "discipline"])

    if pm["avoidable_frac"] >= 40 and pm["losses"] >= 5:
        _add(f"{pm['avoidable_frac']}% of losses were IN PROFIT first (avg MFE on wins {pm['avg_mfe_wins']}) — an "
             f"EXIT/DURATION problem, not an entry one. Test a shorter contract or banking at 1R; do NOT change "
             f"entry logic for these.", ["exit", "duration"])

    if pm["fragile_frac"] >= 40 and pm["wins"] >= 5:
        _add(f"{pm['fragile_frac']}% of wins took a full-risk scare (avg MAE on losses {pm['avg_mae_losses']}) — "
             f"lucky, not robust. Review entry timing / whether the stop sits in normal candle noise.",
             ["entry", "timing"])

    for label, cnt in list(pm["gate_funnel"].items())[:1]:
        total_rej = sum(pm["gate_funnel"].values()) or 1
        if cnt / total_rej >= 0.4:
            note = ("a ranging market (normal — hold the line)" if label in ("trigger break", "EMA flat")
                    else "the trend filter may be too tight for current conditions — test loosening it in the Lab")
            _add(f"Most stand-asides come from the '{label}' gate ({cnt} of {total_rej} rejections) — {note}.",
                 ["gate", label.replace(" ", "_")])

    for code, st_ in pm["by_duration"].items():
        if st_["trades"] >= 8 and st_["win_rate"] < 40:
            _add(f"Duration {code}m win rate {st_['win_rate']}% over {st_['trades']} trades (pnl {st_['pnl']:+.2f}) "
                 f"is weak — consider pausing {code}m or tightening its gates.", ["duration", f"{code}m"])

    for code, st_ in pm["by_symbol"].items():
        if st_["trades"] >= 8 and st_["win_rate"] < 40:
            _add(f"Symbol {code} win rate {st_['win_rate']}% over {st_['trades']} trades (pnl {st_['pnl']:+.2f}) "
                 f"is negative expectancy — consider dropping it from the universe.", ["symbol", code])

    for e in pm["factor_edge"]:
        if e["edge"] is not None and e["edge"] <= -0.4 and pm["n_closed"] >= 10:
            _add(f"Factor '{e['factor']}' scores HIGHER on losses than wins (edge {e['edge']}) — it is not "
                 f"filtering; consider trimming its weight in the Lab.", ["factor", e["factor"]])

    if pm["taken_rate"] < 3 and pm["n_closed"] > 0 and pm["net_pnl"] > 0:
        _add(f"Extremely selective and profitable (taken {pm['taken_rate']}% of reviews, net {pm['net_pnl']:+.2f}) "
             f"— hold the line; do not loosen the gates.", ["selectivity", "discipline"])

    # dedupe by slug, cap
    seen, final = set(), []
    for o in out:
        key = _slug(o["text"])
        if key in seen:
            continue
        seen.add(key)
        final.append(o)
        if len(final) >= 6:
            break
    return final
