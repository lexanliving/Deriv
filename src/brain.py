"""src/brain.py — the Trading Brain: a grounded, trainable intelligence layer.

This module wraps the existing Deriv bot with an LLM "brain" (a DigitalOcean
managed GenAI agent, OpenAI-compatible) WITHOUT touching the trading engine,
strategy, risk, journal, or UI that already work. It is stdlib-only (urllib),
so it adds no pip dependency and cannot break installation.

It does four things, all read-only with respect to live trading:

  1. BRAIN CLIENT  — talks to the DO managed agent. Mirrors chat-ui/main.py
     (discover deployment URL + create a secret key), but ALSO accepts a
     pinned AGENT_ENDPOINT + AGENT_API_KEY so a VPS can create the key once
     and never call the DO API again. If no creds are present the client is
     simply "not configured" — it never raises at import or page load.

  2. LESSONS STORE — logs/lessons.jsonl, an append-only, crash-safe corpus of
     observations ("gold chops 14:00-15:00 UTC, avoid", "my losses on XAUUSD
     were all duration problems"). This is the trainable memory: every saved
     lesson is retrieved into future answers' context, so the brain compounds
     your wisdom. Export/import merge by id (idempotent) so it survives host
     resets and is portable to a VPS.

  3. POST-MORTEM   — computed from the Deriv journal alone:
       avoidable_losses : LOST trades that were IN PROFIT (mfe>0) then reversed
                          -> an EXIT / DURATION problem, not an entry one.
       fragile_wins     : WON trades that took a full-risk scare (mae>0, big)
                          -> ENTRY TIMING / stop-in-noise, survived not robust.
       gatekeepers      : for near-miss stand-asides in a trending market, the
                          single factor most often blocking them.
       edges            : win rate / pnl by symbol, hour(UTC), regime, duration.

  4. GATE BACKTEST — replay recorded reviews through re-weighted factors and a
     new threshold; report kept / dropped / added with the REAL pnl of dropped
     trades. This is how a brain proposal is validated before any opt-in.
     The live strategy is NEVER mutated by this module.

The brain PROPOSES (a preset = new weights + threshold, or a rule note). The
human routes a proposal through the backtest and opts in by editing a preset.
That closed loop (observe -> ask -> propose -> backtest -> opt-in) is the whole
"trainable within the app" story, done so it cannot break the running bot.
"""
from __future__ import annotations

import json
import os
import re
import ssl
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from config import LOG_DIR
from src.journal import get_journal
from src.logger import get_logger

logger = get_logger("brain")

# --------------------------------------------------------------------------- #
#  The strategy's rulebook, transcribed faithfully from src/strategy.py +      #
#  config.py so the brain's grounding matches the live bot exactly. If the     #
#  strategy changes, update this text — the brain's advice is only as good as  #
#  this transcript.                                                            #
# --------------------------------------------------------------------------- #
RULEBOOK = """You are the Trading Brain for "MomentumMaster TF", a Deriv
binary-options (Up/Down = Call/Put) trend bot. You advise; you NEVER change the
live strategy. Any parameter change must be expressed as a PROPOSAL that the
human validates with a gate-backtest before opting in.

SCORING (out of 25). Ten independent factors, each an integer in [0, max]:
  trend=5 (constant 5 when the higher-timeframe trend agrees; else the setup
    is hard-rejected before scoring), trigger=3, momentum=3, volatility=2,
  alignment=1, adx=3, macd=2, rsi_zone=2, pattern=2, structure=2.
  A stored factor value divided by its max recovers the 0..1 strength the bot
  used, so a re-weight is sum( round( (stored_i / max_i) * new_weight_i ) ).

HARD GATES (must ALL pass; a high score cannot override any of them):
  1 higher-timeframe trend agreement for the contract length;
  2 a real breakout of the prior candle (the trigger);
  3 close beyond the fast EMA;
  4 the express-aware exhaustion limit (a powerful candle widens the band, a
    weak candle far from the EMA is rejected as exhaustion);
  5 no RSI/price divergence against the trade;
  6 entry-timeframe market structure >= 1 (a swing is respected);
  plus regime gates: SHORT regime needs a decisive trigger body and a trending
  5m; LONG regime needs intact 1h structure, 1h ADX floor, and 1h MACD aligned.

EXPRESS LANE: the candle's own conviction (body + close position + ADX + MACD
  acceleration + pattern) is measured before the exhaustion gate; an
  overwhelming candle is taken instead of chased-and-rejected.

DURATION-AWARE TRIGGER: 1m/2m contracts use a 1m trigger candle; 5m/15m use 5m;
  30m/60m use 15m. Trend confirmation is always 30m + 1h. Signals fire only on
  a closed trigger candle (no live-tick entry).

REGIMES: SHORT (<=15m), MEDIUM (<=30m), LONG (>30m). A review's `trend` column
  is UP/DOWN/- ; a trending review has trend in {UP, DOWN}.

SELECTIVITY PRESETS (entry score threshold / entry ADX floor):
  Conservative 20/18, Balanced 16/15, Aggressive 13/12.

POST-MORTEM LENSES (how to read the numbers I give you):
  avoidable_losses = LOST trades whose MFE>0 and MFE>MAE*0.5 -> price LED in
    your favour then reversed before expiry. The ENTRY was right; the HOLD /
    DURATION was too long. Recommend a shorter contract or banking at 1R, NOT
    an entry change.
  fragile_wins = WON trades whose MAE>0 and MAE>MFE*0.5 -> it nearly went
    against you and survived on timing. Watch entry timing / stop sitting in
    normal candle noise.
  gatekeepers = the factor most often the weakest on near-miss stand-asides in
    a trending market -> the prime re-calibration candidate (test it in the
    backtest before changing anything).
  A blank day is legitimate; even stand-asides carry regime + near-miss data.

OUTPUT DISCIPLINE: ground every claim in the provided context; if the evidence
  is thin, say so and recommend collecting more data rather than changing
  parameters. When and only when the evidence is strong, you MAY emit exactly
  one proposal as a fenced ```json block with this schema:
  {"type":"preset","name":"brain-<short>","weights":{"trend":..,"trigger":..,
   "momentum":..,"volatility":..,"alignment":..,"adx":..,"macd":..,
   "rsi_zone":..,"pattern":..,"structure":..},"threshold":<int>,"rationale":".."}
  Weights are positive ints (relative importance; the current bot's implicit
  weights equal the factor maxes above). Threshold is an int (typical 13..23).
  Never emit more than one proposal. Never tell the user to edit live code."""

# Factor keys in journal order, with their max (= the bot's implicit weight).
FACTORS: List[Tuple[str, str, int]] = [
    ("s_trend", "trend", 5), ("s_trigger", "trigger", 3), ("s_momentum", "momentum", 3),
    ("s_volatility", "volatility", 2), ("s_alignment", "alignment", 1), ("s_adx", "adx", 3),
    ("s_macd", "macd", 2), ("s_rsi_zone", "rsi_zone", 2), ("s_pattern", "pattern", 2),
    ("s_structure", "structure", 2),
]
FACTOR_KEYS = [k for k, _, _ in FACTORS]
FACTOR_MAX = {k: m for k, _, m in FACTORS}
DEFAULT_WEIGHTS = dict(FACTOR_MAX)  # current bot = each factor weighted by its max

# Backtest preset library (relative re-weightings over the 10 factors).
PRESETS: Dict[str, Dict[str, int]] = {
    "current (as-built)": dict(DEFAULT_WEIGHTS),
    "trend_heavy": {**DEFAULT_WEIGHTS, "trend": 8, "adx": 5, "structure": 4, "trigger": 2, "momentum": 2},
    "execution_heavy": {**DEFAULT_WEIGHTS, "trigger": 6, "momentum": 5, "pattern": 4, "structure": 4, "trend": 3},
    "structure_patient": {**DEFAULT_WEIGHTS, "structure": 5, "pattern": 4, "rsi_zone": 4, "trigger": 2, "momentum": 2},
}
THRESHOLD_OPTIONS: List[int] = [13, 16, 20, 23]

LESSONS_PATH = os.path.join(LOG_DIR, "lessons.jsonl")
_LESSONS_LOCK = threading.Lock()
_STOP = {"the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "is", "it", "my", "i", "was", "were", "with", "that", "this", "but", "not", "are", "be"}


# --------------------------------------------------------------------------- #
#  errors + tiny http helper (stdlib only)                                    #
# --------------------------------------------------------------------------- #
class BrainError(Exception):
    pass


def _http(method: str, url: str, headers: Dict[str, str], body: Optional[Any] = None, timeout: float = 120.0) -> Tuple[int, str]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except urllib.error.URLError as exc:
        raise BrainError(f"network error reaching {url}: {exc.reason}") from exc
    except Exception as exc:  # timeout / socket / etc.
        raise BrainError(f"request failed: {exc}") from exc


def _secret(name: str) -> str:
    val = os.getenv(name)
    if val and str(val).strip():
        return str(val).strip()
    try:
        import streamlit as st
        v = st.secrets.get(name, "")
        return str(v).strip() if v else ""
    except Exception:
        return ""


# --------------------------------------------------------------------------- #
#  Brain client                                                               #
# --------------------------------------------------------------------------- #
class BrainClient:
    """Lazy, fault-tolerant client. Importing this never touches the network."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._endpoint: Optional[str] = None
        self._api_key: Optional[str] = None
        self._discovered = False
        self._last_error: str = ""

        pinned_ep = _secret("AGENT_ENDPOINT")
        pinned_key = _secret("AGENT_API_KEY")
        if pinned_ep and pinned_key:
            self._endpoint = pinned_ep.rstrip("/")
            if not self._endpoint.endswith("/api/v1/chat/completions"):
                if self._endpoint.endswith("/api/v1"):
                    self._endpoint = self._endpoint + "/chat/completions"
                elif "/api/v1/chat/completions" not in self._endpoint:
                    self._endpoint = self._endpoint + "/api/v1/chat/completions"
            self._api_key = pinned_key
            self._discovered = True

        self._agent_uuid = _secret("AGENT_UUID")
        self._do_token = _secret("DO_API_TOKEN")
        self._do_base = _secret("DO_API_BASE") or "https://api.digitalocean.com"

    @property
    def configured(self) -> bool:
        return bool(self._endpoint and self._api_key) or bool(self._agent_uuid and self._do_token)

    def status(self) -> str:
        if self._endpoint and self._api_key:
            return "ready (pinned endpoint)"
        if self._agent_uuid and self._do_token:
            return "ready (will connect on first message)"
        return "not configured — see BRAIN_SETUP.md"

    def _ensure(self) -> None:
        if self._endpoint and self._api_key:
            return
        if not (self._agent_uuid and self._do_token):
            raise BrainError("Brain not configured: set AGENT_UUID+DO_API_TOKEN or AGENT_ENDPOINT+AGENT_API_KEY.")
        with self._lock:
            if self._discovered:
                return
            headers = {"Authorization": f"Bearer {self._do_token}", "Content-Type": "application/json"}
            status, raw = _http("GET", f"{self._do_base}/v2/gen-ai/agents/{self._agent_uuid}", headers, timeout=30)
            if not (200 <= status < 300):
                raise BrainError(f"agent lookup failed ({status}): {raw[:200]}")
            deploy_url = (json.loads(raw).get("agent", {}) or {}).get("deployment", {}).get("url")
            if not deploy_url:
                raise BrainError("agent has no deployment URL yet (still provisioning?).")
            self._endpoint = deploy_url.rstrip("/") + "/api/v1/chat/completions"
            status, raw = _http("POST", f"{self._do_base}/v2/gen-ai/agents/{self._agent_uuid}/api_keys",
                                headers, body={"name": "deriv-brain"}, timeout=30)
            if not (200 <= status < 300):
                raise BrainError(f"api key creation failed ({status}): {raw[:200]}")
            self._api_key = json.loads(raw).get("api_key_info", {}).get("secret_key")
            if not self._api_key:
                raise BrainError("api key creation returned no secret_key.")
            self._discovered = True
            logger.info("Trading Brain connected to managed agent.")

    def chat(self, messages: List[Dict[str, str]]) -> str:
        self._ensure()
        headers = {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}
        status, raw = _http("POST", self._endpoint or "", headers, body={"messages": messages}, timeout=150)
        if not (200 <= status < 300):
            raise BrainError(f"chat failed ({status}): {raw[:300]}")
        try:
            data = json.loads(raw)
            return data["choices"][0]["message"]["content"]
        except Exception as exc:
            raise BrainError(f"unexpected chat response: {raw[:300]}") from exc

    def ping(self) -> str:
        """Cheap connectivity test for the UI; returns ok-message or error text."""
        try:
            reply = self.chat([{"role": "user", "content": "Reply with the single word: ready"}])
            return f"ok — brain replied: {reply.strip()[:60]}"
        except BrainError as exc:
            return f"error: {exc}"


# --------------------------------------------------------------------------- #
#  Lessons store (the trainable memory)                                       #
# --------------------------------------------------------------------------- #
def _tokenize(text: str) -> List[str]:
    return [t for t in re.findall(r"[a-z0-9]+", (text or "").lower()) if t not in _STOP and len(t) > 1]


def load_lessons() -> List[Dict[str, Any]]:
    if not os.path.exists(LESSONS_PATH):
        return []
    out: List[Dict[str, Any]] = []
    try:
        with open(LESSONS_PATH, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except Exception:
                        continue
    except Exception as exc:
        logger.warning("lessons load failed: %s", exc)
    return out


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
    os.makedirs(os.path.dirname(LESSONS_PATH), exist_ok=True)
    with _LESSONS_LOCK:
        with open(LESSONS_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(lesson, ensure_ascii=False) + "\n")
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
    items: List[Any]
    try:
        parsed = json.loads(text)
        items = parsed if isinstance(parsed, list) else [parsed]
    except Exception:
        items = []
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


def retrieve_lessons(question: str, k: int = 6) -> List[Dict[str, Any]]:
    lessons = load_lessons()
    if not lessons:
        return []
    qt = set(_tokenize(question))
    scored: List[Tuple[int, float, Dict[str, Any]]] = []
    for i, l in enumerate(lessons):
        lt = set(_tokenize((l.get("text", "") + " " + " ".join(l.get("tags", [])))))
        score = len(qt & lt)
        score += 3 * sum(1 for tag in l.get("tags", []) if tag.lower() in qt)
        if not l.get("confirmed", True):
            score -= 1
        scored.append((score, -i, l))  # -i keeps most-recent first on ties
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    chosen = [l for s, _, l in scored if s > 0][:k]
    return chosen or lessons[-k:]


# --------------------------------------------------------------------------- #
#  parsing helpers                                                            #
# --------------------------------------------------------------------------- #
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
        if _b(r, "taken"):
            continue
        if str(r.get("trend", "")).strip() not in ("UP", "DOWN"):
            continue
        sc = _f(r, "score") or 0.0
        thr = _f(r, "threshold") or 20.0
        if sc < thr - 8:
            continue
        vals = [(k, _f(r, k)) for k in FACTOR_KEYS]
        vals = [(k, v) for k, v in vals if v is not None]
        if not vals:
            continue
        worst = min(vals, key=lambda kv: kv[1] / FACTOR_MAX[kv[0]])
        gatekeeper[worst[0]] += 1

    def _edges(group_fn):
        g: Dict[Any, Dict[str, float]] = defaultdict(lambda: {"n": 0.0, "w": 0.0, "pnl": 0.0})
        for r in closed:
            key = group_fn(r)
            if key is None:
                continue
            g[key]["n"] += 1
            g[key]["pnl"] += _f(r, "pnl") or 0.0
            if r.get("outcome") == "WON":
                g[key]["w"] += 1
        return {str(k): {"trades": int(v["n"]), "wins": int(v["w"]),
                         "win_rate": round(v["w"] / v["n"] * 100.0, 1) if v["n"] else 0.0,
                         "pnl": round(v["pnl"], 2)} for k, v in g.items()}

    return {
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "summary": {"reviews": len(rows), "taken": sum(1 for r in rows if _b(r, "taken")),
                    "closed": len(closed), "wins": len(wins), "losses": len(losses),
                    "win_rate": round(wr, 1), "net_pnl": round(net, 2),
                    "profit_factor": round(pf, 2) if pf != float("inf") else "inf",
                    "expectancy": round(expect, 2), "lessons": len(load_lessons())},
        "avoidable_losses": avoidable[:60], "fragile_wins": fragile[:60],
        "gatekeeper_factors": gatekeeper.most_common(),
        "by_symbol": _edges(lambda r: r.get("symbol") or None),
        "by_hour": _edges(_hour_utc),
        "by_regime": _edges(lambda r: r.get("regime") or None),
        "by_duration": _edges(lambda r: r.get("duration_min") or None),
    }


def postmortem_text(pm: Dict[str, Any]) -> str:
    s = pm["summary"]
    lines = [f"REVIEWS {s['reviews']} | TAKEN {s['taken']} | CLOSED {s['closed']} "
             f"(W {s['wins']} / L {s['losses']}) | WINRATE {s['win_rate']}% | "
             f"NET {s['net_pnl']:+.2f} | PF {s['profit_factor']} | EXPECTANCY {s['expectancy']:+.2f} | "
             f"LESSONS {s['lessons']}"]
    lines.append(f"AVOIDABLE LOSSES (were in profit, then reversed -> exit/duration problem): {len(pm['avoidable_losses'])}")
    for a in pm["avoidable_losses"][:8]:
        lines.append(f"   - {a['symbol']} {a['ts']} MFE {a['mfe']} MAE {a['mae']} pnl {a['pnl']} ({a['regime']}/{a['dur']}m)")
    lines.append(f"FRAGILE WINS (won after a full-risk scare -> entry timing): {len(pm['fragile_wins'])}")
    for w in pm["fragile_wins"][:8]:
        lines.append(f"   - {w['symbol']} {w['ts']} MAE {w['mae']} MFE {w['mfe']} pnl {w['pnl']} ({w['regime']}/{w['dur']}m)")
    lines.append("GATEKEEPERS (factor blocking near-miss setups in trends): " +
                 (", ".join(f"{k}={v}" for k, v in pm["gatekeeper_factors"][:6]) or "none"))
    for label, key in (("BY SYMBOL", "by_symbol"), ("BY HOUR UTC", "by_hour"),
                       ("BY REGIME", "by_regime"), ("BY DURATION", "by_duration")):
        items = sorted(pm[key].items(), key=lambda kv: kv[1]["pnl"])
        if items:
            lines.append(label + ": " + ", ".join(f"{k}(n{v['trades']},wr{v['win_rate']}%,{v['pnl']:+.2f})" for k, v in items[:8]))
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
#  grounding + backtest                                                       #
# --------------------------------------------------------------------------- #
def _recent_text(rows: List[Dict[str, Any]], n: int = 12) -> str:
    tail = rows[-n:]
    out = []
    for r in tail:
        out.append(f"{r.get('timestamp_utc','')[:16]} {r.get('symbol','')} {r.get('direction','')} "
                   f"trend={r.get('trend','')} reg={r.get('regime','')} score={r.get('score','')}/"
                   f"{r.get('threshold','')} taken={r.get('taken','')} -> {r.get('outcome','') or 'aside'} "
                   f"pnl={r.get('pnl','')} rej={r.get('rejection_reason','')}")
    return "\n".join(out)


def build_messages(question: str, pm: Dict[str, Any], rows: List[Dict[str, Any]],
                   include_recent: bool = True) -> List[Dict[str, str]]:
    lessons = retrieve_lessons(question)
    lesson_block = "\n".join(f"- [{l.get('ts_utc','')[:10]}|{','.join(l.get('tags',[])) or '-'}] {l.get('text','')}"
                             for l in lessons) or "(none yet — save observations in the Lessons tab)"
    context = (
        "=== POST-MORTEM (computed live from the journal) ===\n" + postmortem_text(pm) +
        "\n\n=== YOUR LESSONS (most relevant to this question) ===\n" + lesson_block
    )
    if include_recent:
        context += "\n\n=== RECENT REVIEWS ===\n" + _recent_text(rows)
    system = RULEBOOK + "\n\n=== GROUNDING (use this; do not invent numbers) ===\n" + context
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": question},
    ]


def reweight_confidence(row: Dict[str, Any], weights: Dict[str, int]) -> int:
    total = 0
    for k, _, mx in FACTORS:
        v = _f(row, k)
        if v is None:
            continue
        total += int(round((v / mx) * float(weights.get(k, mx))))
    return total


def backtest(rows: List[Dict[str, Any]], weights: Dict[str, int], threshold: int) -> Dict[str, Any]:
    kept = kept_w = 0
    kept_pnl = 0.0
    dropped = dropped_loss = dropped_win = 0
    dropped_pnl = 0.0
    added = 0
    scored_rows = 0
    for r in rows:
        taken = _b(r, "taken")
        outcome = str(r.get("outcome", "")).strip()
        pnl = _f(r, "pnl") or 0.0
        conf = reweight_confidence(r, weights)
        if all(_f(r, k) is None for k in FACTOR_KEYS):
            continue  # no breakdown recorded (hard-gate rejection) -> skip reweight
        scored_rows += 1
        take_new = conf >= threshold
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
    return {
        "weights": dict(weights), "threshold": int(threshold), "scored_rows": scored_rows,
        "kept": kept, "kept_pnl": round(kept_pnl, 2),
        "kept_win_rate": round(kept_w / kept * 100.0, 1) if kept else 0.0,
        "dropped": dropped, "dropped_pnl": round(dropped_pnl, 2),
        "dropped_losses_avoided": dropped_loss, "dropped_wins_lost": dropped_win,
        "added_unknown": added,
    }


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
    if not isinstance(obj, dict) or obj.get("type") != "preset":
        return None
    w = obj.get("weights")
    if not isinstance(w, dict):
        return None
    weights = {k: int(w.get(k, DEFAULT_WEIGHTS[k])) for k in FACTOR_KEYS}
    try:
        thr = int(obj.get("threshold", 20))
    except Exception:
        thr = 20
    return {"name": str(obj.get("name", "brain-proposal")), "weights": weights,
            "threshold": thr, "rationale": str(obj.get("rationale", ""))}


def preset_text(name: str, weights: Dict[str, int], threshold: int, rationale: str = "") -> str:
    body = (
        f"# Strategy preset: {name}\n"
        f"# Generated by the Trading Brain. VALIDATE with the gate-backtest on paper\n"
        f"# before using. The live bot never auto-applies this.\n"
        f"# To opt in: add this as a new entry in config.STRATEGY_SENSITIVITY_PRESETS\n"
        f"# (or map it onto an existing preset name), then select it in the sidebar.\n"
        f"THRESHOLD = {int(threshold)}\n"
        f"WEIGHTS = {weights}\n"
    )
    if rationale:
        body += f"# Rationale: {rationale}\n"
    return body
