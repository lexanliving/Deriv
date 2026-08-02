"""src/brain_kb.py — the brain's memory, library, retrieval, and analytics.

This is the "retrieval + augmentation" half of the RAG blueprint, rebuilt for
free over the bot's OWN data:

  * lessons   (logs/lessons.jsonl)  — your observations + the brain's diary
  * documents (logs/brain_docs.jsonl) — a chunked knowledge library you drop in
  * live post-mortem — computed from the Deriv journal (avoidable losses,
    fragile wins, gatekeepers, edges by symbol/hour/regime/duration)
  * rulebook  — the strategy's gates/weights/regimes transcribed verbatim, so
    the brain's grounding always matches the live bot

retrieve() pulls the most relevant lessons + doc chunks for a question; the
page assembles them with the post-mortem + recent reviews into the prompt.
Everything here is read-only w.r.t. live trading; the only writes are to the
two jsonl memory files (append-only, crash-safe, export/import-merge by id).
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

from config import LOG_DIR
from src.journal import get_journal
from src.logger import get_logger

logger = get_logger("brain_kb")

LESSONS_PATH = os.path.join(LOG_DIR, "lessons.jsonl")
DOCS_PATH = os.path.join(LOG_DIR, "brain_docs.jsonl")
_LESSONS_LOCK = threading.Lock()
_DOCS_LOCK = threading.Lock()
_STOP = {"the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "is", "it", "my", "i",
         "was", "were", "with", "that", "this", "but", "not", "are", "be", "we", "you", "as", "at"}

# --- the strategy rulebook, transcribed from src/strategy.py + config.py so the
#     brain's advice is grounded in the EXACT live logic (25-pt confluence). ---
RULEBOOK = """You are the Trading Brain for "MomentumMaster TF", a Deriv binary-options
(Up/Down = Call/Put) trend bot. You ADVISE only; you NEVER change the live strategy.
Any parameter change must be a PROPOSAL the human validates with the gate-backtest.

SCORING (out of 25). Ten integer factors in [0, max]:
  trend=5, trigger=3, momentum=3, volatility=2, alignment=1, adx=3, macd=2,
  rsi_zone=2, pattern=2, structure=2.
  A stored factor value / its max recovers the 0..1 strength used live, so a
  re-weight is sum( round( (stored_i / max_i) * new_weight_i ) ).

HARD GATES (all must pass; a high score cannot override any):
  1 higher-timeframe trend agreement for the contract length;
  2 a real breakout of the prior candle (the trigger);
  3 close beyond the fast EMA;
  4 the express-aware exhaustion limit (a powerful candle widens the band; a
    weak candle far from the EMA is rejected as exhaustion);
  5 no RSI/price divergence against the trade;
  6 entry-timeframe market structure >= 1 (a swing is respected);
  plus regime gates: SHORT needs a decisive trigger body and a trending 5m;
  LONG needs intact 1h structure, a 1h ADX floor, and 1h MACD aligned.

EXPRESS LANE: the candle's own conviction (body + close position + ADX + MACD
  acceleration + pattern) is measured before the exhaustion gate; an overwhelming
  candle is taken instead of chased-and-rejected.

DURATION-AWARE TRIGGER: 1m/2m -> 1m candle; 5m/15m -> 5m; 30m/60m -> 15m. Trend
  confirmation is always 30m + 1h. Signals fire only on a closed trigger candle.

REGIMES: SHORT (<=15m), MEDIUM (<=30m), LONG (>30m). A review's `trend` is
  UP/DOWN/- ; a trending review has trend in {UP, DOWN}.

SELECTIVITY PRESETS (threshold / ADX floor): Conservative 20/18, Balanced 16/15,
  Aggressive 13/12.

POST-MORTEM LENSES (how to read the numbers provided):
  avoidable_losses = LOST trades with MFE>0 and MFE>MAE*0.5 -> price LED in your
    favour then reversed before expiry. The ENTRY was right; the HOLD / DURATION
    was too long. Recommend a shorter contract or banking at 1R — NOT an entry change.
  fragile_wins = WON trades with MAE>0 and MAE>MFE*0.5 -> survived on timing.
    Watch entry timing / a stop sitting in normal candle noise.
  gatekeepers = the factor most often weakest on near-miss stand-asides in a
    trending market -> the prime re-calibration candidate (backtest before changing).
  A blank day is legitimate; stand-asides still carry regime + near-miss data.

OUTPUT DISCIPLINE: ground every claim in the provided context; if evidence is thin,
  say so and recommend collecting more data rather than changing parameters. When
  and only when evidence is strong, you MAY emit exactly one proposal as a fenced
  ```json block:
  {"type":"preset","name":"brain-<short>","weights":{"trend":..,"trigger":..,
   "momentum":..,"volatility":..,"alignment":..,"adx":..,"macd":..,"rsi_zone":..,
   "pattern":..,"structure":..},"threshold":<int>,"rationale":".."}
  Weights are positive ints; threshold an int (typical 13..23). Never emit >1
  proposal. Never tell the user to edit live code."""

FACTORS: List[Tuple[str, str, int]] = [
    ("s_trend", "trend", 5), ("s_trigger", "trigger", 3), ("s_momentum", "momentum", 3),
    ("s_volatility", "volatility", 2), ("s_alignment", "alignment", 1), ("s_adx", "adx", 3),
    ("s_macd", "macd", 2), ("s_rsi_zone", "rsi_zone", 2), ("s_pattern", "pattern", 2),
    ("s_structure", "structure", 2),
]
FACTOR_KEYS = [k for k, _, _ in FACTORS]
FACTOR_MAX = {k: m for k, _, m in FACTORS}
DEFAULT_WEIGHTS = dict(FACTOR_MAX)

PRESETS: Dict[str, Dict[str, int]] = {
    "current (as-built)": dict(DEFAULT_WEIGHTS),
    "trend_heavy": {**DEFAULT_WEIGHTS, "trend": 8, "adx": 5, "structure": 4, "trigger": 2, "momentum": 2},
    "execution_heavy": {**DEFAULT_WEIGHTS, "trigger": 6, "momentum": 5, "pattern": 4, "structure": 4, "trend": 3},
    "structure_patient": {**DEFAULT_WEIGHTS, "structure": 5, "pattern": 4, "rsi_zone": 4, "trigger": 2, "momentum": 2},
}
THRESHOLD_OPTIONS: List[int] = [13, 16, 20, 23]


# --------------------------------------------------------------------------- #
#  text helpers                                                               #
# --------------------------------------------------------------------------- #
def _tokenize(text: str) -> List[str]:
    return [t for t in re.findall(r"[a-z0-9]+", (text or "").lower()) if t not in _STOP and len(t) > 1]


def _f(d: Dict[str, Any], k: str) -> Optional[float]:
    v = d.get(k)
    if v is None or str(v).strip() == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _b(d: Dict[str, Any], k: str) -> bool:
    return str(d.get(k, "")).strip().upper() == "TRUE"


def _hour_utc(d: Dict[str, Any]) -> Optional[int]:
    s = str(d.get("timestamp_utc", "")).strip()
    if len(s) >= 13 and s[10] in (" ", "T"):
        try:
            return int(s[11:13])
        except Exception:
            return None
    return None


# --------------------------------------------------------------------------- #
#  lessons store                                                              #
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
    lesson = {
        "id": uuid.uuid4().hex[:10],
        "ts_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "text": text,
        "tags": [str(t).strip() for t in (tags or []) if str(t).strip()],
        "source": source,
        "confirmed": bool(confirmed),
    }
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


# --------------------------------------------------------------------------- #
#  document library (the free "knowledge base")                               #
# --------------------------------------------------------------------------- #
def _chunk(text: str, title: str, max_chars: int = 1400) -> List[Dict[str, Any]]:
    paras = [p.strip() for p in re.split(r"\n\s*\n", text or "") if p.strip()]
    if not paras:
        paras = [text.strip()] if text and text.strip() else []
    chunks: List[Dict[str, Any]] = []
    buf, buf_len = [], 0
    doc_id = uuid.uuid4().hex[:10]
    idx = 0

    def flush():
        nonlocal buf, buf_len, idx
        if buf:
            chunks.append({"id": f"{doc_id}-{idx}", "title": title, "text": "\n\n".join(buf)})
            idx += 1
            buf, buf_len = [], 0

    for p in paras:
        if buf_len + len(p) > max_chars and buf:
            flush()
        buf.append(p)
        buf_len += len(p) + 2
    flush()
    return chunks


def add_document(text: str, title: str = "") -> int:
    title = (title or "untitled").strip()
    chunks = _chunk(text, title)
    if not chunks:
        return 0
    os.makedirs(os.path.dirname(DOCS_PATH), exist_ok=True)
    with _DOCS_LOCK:
        with open(DOCS_PATH, "a", encoding="utf-8") as fh:
            for c in chunks:
                c["ts_utc"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                fh.write(json.dumps(c, ensure_ascii=False) + "\n")
    return len(chunks)


def list_documents() -> List[Dict[str, Any]]:
    rows = _read_jsonl(DOCS_PATH)
    by_title: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        t = r.get("title", "untitled")
        d = by_title.setdefault(t, {"title": t, "chunks": 0, "chars": 0, "ts_utc": r.get("ts_utc", "")})
        d["chunks"] += 1
        d["chars"] += len(r.get("text", ""))
    return sorted(by_title.values(), key=lambda d: d["ts_utc"], reverse=True)


def docs_bytes() -> bytes:
    if not os.path.exists(DOCS_PATH):
        return b""
    with open(DOCS_PATH, "rb") as fh:
        return fh.read()


def import_kb(data: bytes, title: str = "") -> int:
    """Import a .md/.txt/.jsonl as library documents (chunked)."""
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
    """Export the whole brain corpus as one markdown doc (drop into any KB)."""
    lines = ["# Trading Brain — memory + knowledge corpus",
             "## Lessons (observations + brain diary)"]
    for l in load_lessons():
        tags = ",".join(l.get("tags", [])) or "-"
        lines.append(f"- [{l.get('ts_utc','')[:10]}|{tags}|{l.get('source','')}] {l.get('text','')}")
    lines.append("\n## Library documents")
    for r in _read_jsonl(DOCS_PATH):
        lines.append(f"\n### {r.get('title','untitled')} ({r.get('id','')})\n{r.get('text','')}")
    return ("\n".join(lines)).encode("utf-8")


# --------------------------------------------------------------------------- #
#  retrieval                                                                  #
# --------------------------------------------------------------------------- #
def _score(query_tokens: set, text: str, tags: List[str]) -> int:
    tt = set(_tokenize(text))
    s = len(query_tokens & tt)
    s += 3 * sum(1 for tag in tags if tag.lower() in query_tokens)
    return s


def retrieve(question: str, k_lessons: int = 6, k_docs: int = 4) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    qt = set(_tokenize(question))
    if not qt:
        return load_lessons()[-k_lessons:], _read_jsonl(DOCS_PATH)[-k_docs:]
    lessons = load_lessons()
    lscored = sorted(
        [(_score(qt, l.get("text", ""), l.get("tags", [])) - (0 if l.get("confirmed", True) else 1), -i, l)
         for i, l in enumerate(lessons)],
        key=lambda x: (x[0], x[1]), reverse=True)
    chosen_l = [l for s, _, l in lscored if s > 0][:k_lessons] or lessons[-k_lessons:]
    docs = _read_jsonl(DOCS_PATH)
    dscored = sorted([(_score(qt, d.get("text", "") + " " + d.get("title", ""), []), -i, d)
                      for i, d in enumerate(docs)], key=lambda x: (x[0], x[1]), reverse=True)
    chosen_d = [d for s, _, d in dscored if s > 0][:k_docs]
    return chosen_l, chosen_d


# --------------------------------------------------------------------------- #
#  post-mortem (computed from the Deriv journal only)                         #
# --------------------------------------------------------------------------- #
def compute_postmortem(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    closed = [r for r in rows if str(r.get("outcome", "")).strip() in ("WON", "LOST")]
    wins = [r for r in closed if r.get("outcome") == "WON"]
    losses = [r for r in closed if r.get("outcome") == "LOST"]
    net = sum(_f(r, "pnl") or 0.0 for r in closed)
    gw = sum(_f(r, "pnl") or 0.0 for r in wins)
    gl = abs(sum(_f(r, "pnl") or 0.0 for r in losses))
    wr = (len(wins) / len(closed) * 100.0) if closed else 0.0
    pf = (gw / gl) if gl > 0 else (float("inf") if gw > 0 else 0.0)
    avg_w = (gw / len(wins)) if wins else 0.0
    avg_l = (gl / len(losses)) if losses else 0.0
    expect = (wr / 100.0 * avg_w) - ((1 - wr / 100.0) * avg_l)

    avoidable: List[Dict[str, Any]] = []
    for r in losses:
        mae, mfe = _f(r, "mae"), _f(r, "mfe")
        if mfe is not None and mfe > 0 and (mae is None or mfe > mae * 0.5):
            avoidable.append({"symbol": r.get("symbol", ""), "ts": r.get("timestamp_utc", ""),
                              "mfe": round(mfe, 5), "mae": round(mae, 5) if mae is not None else None,
                              "pnl": _f(r, "pnl"), "regime": r.get("regime", ""), "dur": r.get("duration_min", "")})
    fragile: List[Dict[str, Any]] = []
    for r in wins:
        mae, mfe = _f(r, "mae"), _f(r, "mfe")
        if mae is not None and mae > 0 and (mfe is None or mae > mfe * 0.5):
            fragile.append({"symbol": r.get("symbol", ""), "ts": r.get("timestamp_utc", ""),
                            "mae": round(mae, 5), "mfe": round(mfe, 5) if mfe is not None else None,
                            "pnl": _f(r, "pnl"), "regime": r.get("regime", ""), "dur": r.get("duration_min", "")})
    gatekeeper: Counter = Counter()
    for r in rows:
        if _b(r, "taken") or str(r.get("trend", "")).strip() not in ("UP", "DOWN"):
            continue
        sc, thr = _f(r, "score") or 0.0, _f(r, "threshold") or 20.0
        if sc < thr - 8:
            continue
        vals = [(k, _f(r, k)) for k in FACTOR_KEYS]
        vals = [(k, v) for k, v in vals if v is not None]
        if vals:
            gatekeeper[min(vals, key=lambda kv: kv[1] / FACTOR_MAX[kv[0]])[0]] += 1

    def _edges(fn):
        g: Dict[Any, Dict[str, float]] = defaultdict(lambda: {"n": 0.0, "w": 0.0, "pnl": 0.0})
        for r in closed:
            k = fn(r)
            if k is None:
                continue
            g[k]["n"] += 1
            g[k]["pnl"] += _f(r, "pnl") or 0.0
            if r.get("outcome") == "WON":
                g[k]["w"] += 1
        return {str(k): {"trades": int(v["n"]), "wins": int(v["w"]),
                         "win_rate": round(v["w"] / v["n"] * 100.0, 1) if v["n"] else 0.0,
                         "pnl": round(v["pnl"], 2)} for k, v in g.items()}

    return {
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "summary": {"reviews": len(rows), "taken": sum(1 for r in rows if _b(r, "taken")),
                    "closed": len(closed), "wins": len(wins), "losses": len(losses),
                    "win_rate": round(wr, 1), "net_pnl": round(net, 2),
                    "profit_factor": round(pf, 2) if pf != float("inf") else "inf",
                    "expectancy": round(expect, 2), "lessons": len(load_lessons()),
                    "doc_chunks": len(_read_jsonl(DOCS_PATH))},
        "avoidable_losses": avoidable[:60], "fragile_wins": fragile[:60],
        "gatekeeper_factors": gatekeeper.most_common(),
        "by_symbol": _edges(lambda r: r.get("symbol") or None),
        "by_hour": _edges(_hour_utc),
        "by_regime": _edges(lambda r: r.get("regime") or None),
        "by_duration": _edges(lambda r: r.get("duration_min") or None),
    }


def postmortem_text(pm: Dict[str, Any]) -> str:
    s = pm["summary"]
    L = [f"REVIEWS {s['reviews']} | TAKEN {s['taken']} | CLOSED {s['closed']} "
         f"(W {s['wins']} / L {s['losses']}) | WINRATE {s['win_rate']}% | NET {s['net_pnl']:+.2f} | "
         f"PF {s['profit_factor']} | EXPECTANCY {s['expectancy']:+.2f} | LESSONS {s['lessons']} | DOC_CHUNKS {s['doc_chunks']}"]
    L.append(f"AVOIDABLE LOSSES (in profit, then reversed -> exit/duration problem): {len(pm['avoidable_losses'])}")
    for a in pm["avoidable_losses"][:8]:
        L.append(f"   - {a['symbol']} {a['ts']} MFE {a['mfe']} MAE {a['mae']} pnl {a['pnl']} ({a['regime']}/{a['dur']}m)")
    L.append(f"FRAGILE WINS (won after a full-risk scare -> entry timing): {len(pm['fragile_wins'])}")
    for w in pm["fragile_wins"][:8]:
        L.append(f"   - {w['symbol']} {w['ts']} MAE {w['mae']} MFE {w['mfe']} pnl {w['pnl']} ({w['regime']}/{w['dur']}m)")
    L.append("GATEKEEPERS (factor blocking near-miss setups in trends): " +
             (", ".join(f"{k}={v}" for k, v in pm["gatekeeper_factors"][:6]) or "none"))
    for label, key in (("BY SYMBOL", "by_symbol"), ("BY HOUR UTC", "by_hour"),
                       ("BY REGIME", "by_regime"), ("BY DURATION", "by_duration")):
        items = sorted(pm[key].items(), key=lambda kv: kv[1]["pnl"])
        if items:
            L.append(label + ": " + ", ".join(f"{k}(n{v['trades']},wr{v['win_rate']}%,{v['pnl']:+.2f})" for k, v in items[:8]))
    return "\n".join(L)


# --------------------------------------------------------------------------- #
#  grounding + backtest                                                       #
# --------------------------------------------------------------------------- #
def _recent_text(rows: List[Dict[str, Any]], n: int = 12) -> str:
    out = []
    for r in rows[-n:]:
        out.append(f"{str(r.get('timestamp_utc',''))[:16]} {r.get('symbol','')} {r.get('direction','')} "
                   f"trend={r.get('trend','')} reg={r.get('regime','')} score={r.get('score','')}/"
                   f"{r.get('threshold','')} taken={r.get('taken','')} -> {r.get('outcome','') or 'aside'} "
                   f"pnl={r.get('pnl','')} rej={r.get('rejection_reason','')}")
    return "\n".join(out)


def build_messages(question: str, pm: Dict[str, Any], rows: List[Dict[str, Any]], include_recent: bool = True) -> List[Dict[str, str]]:
    lessons, docs = retrieve(question)
    lesson_block = "\n".join(f"- [{l.get('ts_utc','')[:10]}|{','.join(l.get('tags',[])) or '-'}] {l.get('text','')}"
                             for l in lessons) or "(none yet — save observations in the Memory tab)"
    doc_block = "\n".join(f"- [{d.get('title','')}] {d.get('text','')[:600]}" for d in docs) or "(library empty — add notes/docs in the Library tab)"
    context = ("=== POST-MORTEM (live from the journal) ===\n" + postmortem_text(pm) +
               "\n\n=== YOUR LESSONS (most relevant) ===\n" + lesson_block +
               "\n\n=== KNOWLEDGE LIBRARY (most relevant chunks) ===\n" + doc_block)
    if include_recent:
        context += "\n\n=== RECENT REVIEWS ===\n" + _recent_text(rows)
    system = RULEBOOK + "\n\n=== GROUNDING (use this; do not invent numbers) ===\n" + context
    return [{"role": "system", "content": system}, {"role": "user", "content": question}]


def reweight_confidence(row: Dict[str, Any], weights: Dict[str, int]) -> int:
    total = 0
    for k, _, mx in FACTORS:
        v = _f(row, k)
        if v is None:
            continue
        total += int(round((v / mx) * float(weights.get(k, mx))))
    return total


def backtest(rows: List[Dict[str, Any]], weights: Dict[str, int], threshold: int) -> Dict[str, Any]:
    kept = kept_w = dropped = dropped_loss = dropped_win = added = scored = 0
    kept_pnl = dropped_pnl = 0.0
    for r in rows:
        taken = _b(r, "taken")
        outcome = str(r.get("outcome", "")).strip()
        pnl = _f(r, "pnl") or 0.0
        if all(_f(r, k) is None for k in FACTOR_KEYS):
            continue
        scored += 1
        take_new = reweight_confidence(r, weights) >= threshold
        if take_new and taken:
            kept += 1
            if outcome in ("WON", "LOST"):
                kept_pnl += pnl
                if outcome == "WON":
                    kept_w += 1
        elif take_new and not taken:
            added += 1
        elif (not take_new) and taken:
            dropped += 1
            if outcome in ("WON", "LOST"):
                dropped_pnl += pnl
                if outcome == "LOST":
                    dropped_loss += 1
                elif outcome == "WON":
                    dropped_win += 1
    return {"weights": dict(weights), "threshold": int(threshold), "scored_rows": scored,
            "kept": kept, "kept_pnl": round(kept_pnl, 2),
            "kept_win_rate": round(kept_w / kept * 100.0, 1) if kept else 0.0,
            "dropped": dropped, "dropped_pnl": round(dropped_pnl, 2),
            "dropped_losses_avoided": dropped_loss, "dropped_wins_lost": dropped_win,
            "added_unknown": added}


def baseline(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    kept = kept_w = 0
    kept_pnl = 0.0
    for r in rows:
        if not _b(r, "taken"):
            continue
        outcome = str(r.get("outcome", "")).strip()
        kept += 1
        if outcome in ("WON", "LOST"):
            kept_pnl += _f(r, "pnl") or 0.0
            if outcome == "WON":
                kept_w += 1
    return {"weights": "as-built (recorded)", "threshold": "recorded", "scored_rows": kept,
            "kept": kept, "kept_pnl": round(kept_pnl, 2),
            "kept_win_rate": round(kept_w / kept * 100.0, 1) if kept else 0.0,
            "dropped": 0, "dropped_pnl": 0.0, "dropped_losses_avoided": 0,
            "dropped_wins_lost": 0, "added_unknown": 0}


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
    weights = {k: int(w.get(k, DEFAULT_WEIGHTS[k])) for k in FACTOR_KEYS}
    try:
        thr = int(obj.get("threshold", 20))
    except Exception:
        thr = 20
    return {"name": str(obj.get("name", "brain-proposal")), "weights": weights,
            "threshold": thr, "rationale": str(obj.get("rationale", ""))}


def preset_text(name: str, weights: Dict[str, int], threshold: int, rationale: str = "") -> str:
    body = (f"# Strategy preset: {name}\n"
            f"# Generated by the Trading Brain. VALIDATE with the gate-backtest on paper\n"
            f"# before using. The live bot never auto-applies this.\n"
            f"# Opt in: add as a new entry in config.STRATEGY_SENSITIVITY_PRESETS (or map\n"
            f"# onto an existing preset name), then select it in the terminal sidebar.\n"
            f"THRESHOLD = {int(threshold)}\nWEIGHTS = {weights}\n")
    if rationale:
        body += f"# Rationale: {rationale}\n"
    return body
