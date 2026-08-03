"""src/venture_engine.py — the Venture Council (AI 'discusser' gate) over the live market.

Powered by the SAME failover chain as the Trading Brain (src.brain_llm:
Groq -> OpenRouter -> Cerebras -> OpenAI). Whichever API keys are configured
become the council members; every stage fails over across providers, so a
council verdict is produced as long as ANY configured key answers.

Council sequence (runs in the background every VENTURE_INTERVAL_SECONDS, never
on the order path):

  Stage 1  ANALYST  - neutral technical read of the current chart snapshot
  Stage 2  COUNCIL  - BULL and BEAR argue the next trend entry
  Stage 3  CHAIR    - RISK chair issues the final verdict JSON

Judgement basis: CURRENT MARKET CONDITIONS ONLY - trend, multi-timeframe
agreement, momentum, volatility regime, candle structure, price vs EMAs -
plus the contract DURATION and general trading knowledge. The bot's own trade
history is deliberately NOT provided and can never cause a veto.

Control switch (dashboard toggle):
  ON  -> council may block (verdict POOR) or shrink the stake (risk_multiplier)
  OFF -> advisory only: the council still reads the market and its reasoning is
         visible, but get_venture_advice() returns NEUTRAL / 1.0 so nothing is
         ever blocked or shrunk.

Safety rails (always enforced):
  * risk_multiplier clamped to [0, 1]; verdict whitelisted; confidence 0-100.
  * Stage failures degrade gracefully; total failure -> NEUTRAL (never blocks).
  * Malformed council output -> NEUTRAL.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime, timezone

from config import LOG_DIR
from src.logger import get_logger
from src.supabase_service import get_supabase

logger = get_logger("venture")

ADVICE_FILE = os.path.join(LOG_DIR, "venture_advice.json")
VENTURE_INTERVAL_SECONDS = 240

DEFAULT_ADVICE = {
    "verdict": "NEUTRAL",
    "risk_multiplier": 1.0,
    "max_risk_pct": None,
    "discussion": {},
    "reasoning": "No council read yet.",
    "confidence": 0,
    "period_days": 30,
    "created_at": "",
}

_enabled = True  # runtime control switch (dashboard toggle)
_trade_context = {"symbol": None, "duration_minutes": None, "sensitivity": None}


def set_venture_enabled(on: bool) -> None:
    global _enabled
    _enabled = bool(on)


def is_venture_enabled() -> bool:
    return _enabled


def set_trade_context(symbol=None, duration_minutes=None, sensitivity=None) -> None:
    """Dashboard feeds the current setup so the council judges the right duration."""
    try:
        if symbol:
            _trade_context["symbol"] = str(symbol)
        if duration_minutes:
            _trade_context["duration_minutes"] = int(duration_minutes)
        if sensitivity:
            _trade_context["sensitivity"] = str(sensitivity)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _extract_json(text):
    if not text:
        return None
    cleaned = re.sub(r"```(?:json)?", "", str(text))
    m = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def _ema(values, period):
    if not values:
        return None
    k = 2.0 / (period + 1)
    e = values[0]
    for v in values[1:]:
        e += k * (v - e)
    return e


def _duration_guidance(minutes):
    try:
        minutes = int(minutes)
    except (TypeError, ValueError):
        return ("Duration unknown: judge for a generic short binary-option trend entry; "
                "prefer CAUTION over POOR when unsure.")
    if minutes <= 2:
        return (f"Duration {minutes}m (scalp): entry quality is everything. Needs clean immediate "
                "momentum, a decisive trigger candle, normal-or-lower noise, and no exhaustion. "
                "Chop or dead volatility kills these fast; a healthy impulse usually carries 1-2 minutes easily.")
    if minutes <= 15:
        return (f"Duration {minutes}m (short): needs a genuine, trending 5m tape with follow-through. "
                "Mild pullbacks can recover inside 15m, but flat EMAs, opposite higher-timeframe bias, "
                "or volatility spikes are serious threats.")
    if minutes <= 30:
        return (f"Duration {minutes}m (medium): 30m and 1h agreement matters more than the last candle. "
                "Normal noise is fine; the threat is a late-stage, stretched trend or a regime flip mid-contract.")
    return (f"Duration {minutes}m (long): higher-timeframe structure dominates. Requires a sustained trend "
            "with intact structure and aligned momentum; short-term wiggles are irrelevant, exhaustion and "
            "divergence are not.")


class VentureEngine:
    def __init__(self):
        self._state = None
        self._stop = threading.Event()
        self._started = False
        self._lock = threading.Lock()
        self._advice = self._load_local()

    def attach(self, state):
        with self._lock:
            self._state = state
        if self._started:
            return
        self._started = True
        threading.Thread(target=self._run, daemon=True, name="venture-council").start()
        logger.info("Venture council started (market-condition judgement over the LLM chain).")

    def _load_local(self):
        try:
            if os.path.exists(ADVICE_FILE):
                with open(ADVICE_FILE, "r", encoding="utf-8") as f:
                    return {**DEFAULT_ADVICE, **json.load(f)}
        except Exception:
            pass
        return dict(DEFAULT_ADVICE)

    def current_advice(self):
        with self._lock:
            return dict(self._advice)

    # ---- live market snapshot --------------------------------------------
    def _market_snapshot(self):
        snap = {"generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")}
        snap.update({k: v for k, v in _trade_context.items() if v is not None})
        try:
            from config import ENTRY_TIMEFRAME_BY_DURATION
            dur = _trade_context.get("duration_minutes")
            if dur:
                snap["entry_trigger_tf"] = ENTRY_TIMEFRAME_BY_DURATION.get(int(dur), "5m")
        except Exception:
            pass
        st = self._state
        if st is None:
            return snap
        try:
            s = st.get_strategy_state()
            snap.update({
                "trend_direction": s.get("trend_direction"),
                "mtf_bias": s.get("mtf_bias"),
                "mtf_agreement": s.get("mtf_agreement"),
                "mtf_tf_biases": s.get("mtf_tf_biases", {}),
                "micro_bias": s.get("micro_bias"),
                "pattern_stage": s.get("pattern_stage"),
                "last_entry_mode": s.get("last_entry_mode"),
                "last_signal_score": s.get("last_signal_score"),
                "last_signal_score_breakdown": s.get("last_signal_score_breakdown", {}),
            })
        except Exception:
            pass
        try:
            snap["current_price"] = st.current_price
        except Exception:
            pass
        try:
            raw = st.get_candles_5m() or []
            candles = []
            for c in raw:
                try:
                    candles.append({
                        "o": float(c.get("open")), "h": float(c.get("high")),
                        "l": float(c.get("low")), "c": float(c.get("close")),
                    })
                except (TypeError, ValueError):
                    continue
            if len(candles) >= 20:
                closes = [c["c"] for c in candles]
                highs = [c["h"] for c in candles]
                lows = [c["l"] for c in candles]
                last = closes[-1]
                ema20 = _ema(closes, 20)
                ema50 = _ema(closes, 50)
                if ema20:
                    snap["price_vs_ema20"] = "above" if last > ema20 else "below"
                    snap["stretch_from_ema20_pct"] = round((last - ema20) / ema20 * 100.0, 4)
                if ema20 and ema50:
                    snap["ema_trend"] = "bullish" if ema20 > ema50 else "bearish"
                # ATR + volatility regime vs the symbol's own norm
                trs = []
                for i in range(1, len(candles)):
                    trs.append(max(highs[i] - lows[i],
                                   abs(highs[i] - closes[i - 1]),
                                   abs(lows[i] - closes[i - 1])))
                if trs and last > 0:
                    atr14 = sum(trs[-14:]) / len(trs[-14:])
                    atr_base = sum(trs[-40:]) / len(trs[-40:])
                    snap["atr_pct"] = round(atr14 / last * 100.0, 4)
                    ratio = (atr14 / atr_base) if atr_base > 0 else 1.0
                    snap["vol_regime"] = ("quiet" if ratio < 0.7 else
                                          "normal" if ratio < 1.3 else
                                          "elevated" if ratio < 2.0 else "spiking")
                    snap["vol_ratio_vs_norm"] = round(ratio, 2)
                # momentum
                if len(closes) >= 13 and closes[-13] > 0:
                    snap["momentum_1h_pct"] = round((closes[-1] - closes[-13]) / closes[-13] * 100.0, 4)
                if len(closes) >= 4 and closes[-4] > 0:
                    snap["momentum_15m_pct"] = round((closes[-1] - closes[-4]) / closes[-4] * 100.0, 4)
                # last CLOSED candle (the final element may still be forming)
                if len(candles) >= 2:
                    lc = candles[-2]
                    rng = max(lc["h"] - lc["l"], 1e-12)
                    body = abs(lc["c"] - lc["o"])
                    direction = "bullish" if lc["c"] > lc["o"] else ("bearish" if lc["c"] < lc["o"] else "doji")
                    snap["last_closed_candle"] = {
                        "direction": direction,
                        "body_ratio": round(body / rng, 2),
                        "close_position": round((lc["c"] - lc["l"]) / rng, 2),
                    }
                    streak = 0
                    for c in reversed(candles[:-1]):
                        d = "bullish" if c["c"] > c["o"] else ("bearish" if c["c"] < c["o"] else "doji")
                        if d == direction and direction in ("bullish", "bearish"):
                            streak += 1
                        else:
                            break
                    if streak:
                        snap["candle_streak"] = f"{streak} {direction}"
                # simple structure over the last ~12 closed candles
                if len(candles) >= 14:
                    seg_h = highs[-13:-1]
                    seg_l = lows[-13:-1]
                    half = len(seg_h) // 2
                    up = sum(seg_h[half:]) / (len(seg_h) - half) > sum(seg_h[:half]) / half
                    dn = sum(seg_l[half:]) / (len(seg_l) - half) < sum(seg_l[:half]) / half
                    snap["structure_5m"] = "rising" if up else ("falling" if dn else "flat/mixed")
        except Exception:
            pass
        return snap

    # ---- council stages ----------------------------------------------------
    def _call(self, prompt, max_tokens, temperature):
        from src import brain_llm
        return brain_llm.chat_with_chain(
            [{"role": "user", "content": prompt}],
            temperature=temperature,
            max_tokens=max_tokens,
        )

    @staticmethod
    def _last_ok_provider():
        try:
            from src import brain_llm
            for t in reversed(brain_llm.chain_trace()):
                if t.get("status") == "ok":
                    return str(t.get("provider", "")) + " · " + str(t.get("detail", ""))
        except Exception:
            pass
        return ""

    def _run(self):
        time.sleep(8)
        while not self._stop.is_set():
            try:
                self._discuss()
            except Exception as exc:
                logger.error("Venture council cycle failed: %s", exc)
            self._stop.wait(VENTURE_INTERVAL_SECONDS)

    def _discuss(self):
        started = time.time()
        snap = self._market_snapshot()
        data = json.dumps(snap, default=str)
        duration = _trade_context.get("duration_minutes")
        guidance = _duration_guidance(duration)
        dur_txt = f"{duration} minutes" if duration else "the configured duration"

        base = (
            "CONTEXT: You are part of the Venture Council for 'MomentumMaster TF', a Deriv binary-options "
            "trend-following bot. A contract is UP/DOWN (CALL/PUT): it wins if price is above/below the entry "
            "at expiry. There is NO stop-loss and NO early exit - entry quality and duration fit are everything. "
            "The bot enters only in the higher-timeframe trend direction, on a closed trigger candle, after hard "
            "gates already passed. You are given LIVE MARKET CONDITIONS ONLY. You receive NO trade history and "
            "must never reason from, or ask for, past results.\n"
        )

        trace = []
        analyst = bull = bear = chair_summary = ""

        # ---- Stage 1: ANALYST ------------------------------------------------
        try:
            analyst = self._call(
                base +
                "ROLE: Stage 1 of 3 - neutral technical analyst.\n"
                "TASK: Read the MARKET SNAPSHOT below and give a crisp, neutral technical read. Exactly one short "
                "line for each: (1) trend quality and multi-timeframe agreement, (2) momentum - expanding or "
                "fading, (3) volatility regime vs the symbol's own norm, (4) candle structure - impulse vs chop "
                "and wick pressure, (5) stretch vs the EMAs - healthy or extended, (6) the single biggest risk "
                f"for a trend entry of {dur_txt} right now. Facts only. No verdict. Plain text, max 180 words.\n"
                f"CONTRACT DURATION: {dur_txt}. {guidance}\n"
                f"MARKET SNAPSHOT: {data}",
                max_tokens=300, temperature=0.2,
            ).strip()
            trace.append("analyst: " + (self._last_ok_provider() or "failed"))
        except Exception as exc:
            logger.warning("Council stage 1 (analyst) failed: %s", exc)
            trace.append("analyst: failed")

        # ---- Stage 2: COUNCIL (bull vs bear) ----------------------------------
        try:
            raw = self._call(
                base +
                "ROLE: Stage 2 of 3 - council debate between two members.\n"
                "Using the MARKET SNAPSHOT and the ANALYST READ below, argue the next trend entry:\n"
                '- "bull": the strongest HONEST case that a trend entry now settles in favour within ' + dur_txt + ".\n"
                '- "bear": the strongest HONEST case that it fails, chops out, or expires on the wrong side.\n'
                "Rules: cite numbers from the snapshot; invent nothing; max 90 words per side; be concrete about "
                "what a trend-following entry needs (clean continuation, follow-through, room to run before expiry).\n"
                f"ANALYST READ: {analyst or 'unavailable'}\n"
                f"MARKET SNAPSHOT: {data}\n"
                'ANSWER ONLY JSON: {"bull": "...", "bear": "..."}',
                max_tokens=420, temperature=0.4,
            )
            obj = _extract_json(raw) or {}
            bull = str(obj.get("bull", ""))[:500]
            bear = str(obj.get("bear", ""))[:500]
            trace.append("council: " + (self._last_ok_provider() or "failed"))
        except Exception as exc:
            logger.warning("Council stage 2 (bull/bear) failed: %s", exc)
            trace.append("council: failed")

        # ---- Stage 3: CHAIR (final verdict) ------------------------------------
        final = None
        try:
            raw = self._call(
                base +
                "ROLE: Stage 3 of 3 - you are the RISK CHAIR and issue the final, binding verdict on whether the "
                f"bot may take its next trend entry of {dur_txt}, and at what fraction of the planned stake.\n"
                "VERDICT RULES:\n"
                "- GOOD: clean trend, timeframes agree, healthy momentum, normal volatility, room to run. "
                "risk_multiplier 0.9-1.0.\n"
                "- CAUTION: choppy/mixed tape, late-stage or stretched trend, volatility too dead or too hot, "
                "conflicting signals. risk_multiplier 0.5-0.85.\n"
                "- POOR: ONLY when conditions are clearly hostile to trend-following right now (flat EMAs without "
                "agreement, volatility spike or collapse, blow-off exhaustion, hard divergence against the trend). "
                "risk_multiplier 0.0-0.4. NEVER output POOR out of general caution, ambiguity, or missing history.\n"
                "- If the snapshot is incomplete or ambiguous, prefer CAUTION (multiplier >= 0.7) over POOR.\n"
                f"DURATION FIT: {guidance}\n"
                f"ANALYST READ: {analyst or 'unavailable'}\n"
                f"BULL CASE: {bull or 'unavailable'}\n"
                f"BEAR CASE: {bear or 'unavailable'}\n"
                f"MARKET SNAPSHOT: {data}\n"
                "ANSWER ONLY JSON:\n"
                '{"verdict":"GOOD|CAUTION|POOR","risk_multiplier":0.0,"max_risk_pct":null,'
                '"chair_summary":"one sentence","reasoning":"2-3 sentences citing snapshot numbers",'
                '"confidence":0,"period_days":30}',
                max_tokens=520, temperature=0.1,
            )
            final = _extract_json(raw)
            trace.append("chair: " + (self._last_ok_provider() or "failed"))
        except Exception as exc:
            logger.warning("Council stage 3 (chair) failed: %s", exc)
            trace.append("chair: failed")

        # ---- single-shot fallback if the staged council collapsed --------------
        if not final:
            try:
                raw = self._call(
                    base +
                    "The staged council could not reach a verdict. Give the final verdict yourself, judging ONLY "
                    f"the snapshot below for a {dur_txt} trend entry. {guidance}\n"
                    "POOR is reserved for clearly hostile conditions; when unsure choose CAUTION with "
                    "risk_multiplier >= 0.7.\n"
                    f"MARKET SNAPSHOT: {data}\n"
                    "ANSWER ONLY JSON:\n"
                    '{"verdict":"GOOD|CAUTION|POOR","risk_multiplier":0.0,"max_risk_pct":null,'
                    '"chair_summary":"one sentence","reasoning":"2-3 sentences citing snapshot numbers",'
                    '"confidence":0,"period_days":30}',
                    max_tokens=400, temperature=0.1,
                )
                final = _extract_json(raw)
                trace.append("fallback: " + (self._last_ok_provider() or "failed"))
            except Exception as exc:
                logger.warning("Council fallback failed; staying NEUTRAL: %s", exc)
                trace.append("fallback: failed")

        # ---- assemble + clamp ---------------------------------------------------
        advice = dict(DEFAULT_ADVICE)
        if isinstance(final, dict):
            verdict = str(final.get("verdict", "")).strip().upper()
            advice["verdict"] = verdict if verdict in ("GOOD", "CAUTION", "POOR") else "NEUTRAL"
            try:
                advice["risk_multiplier"] = max(0.0, min(1.0, float(final.get("risk_multiplier", 1.0))))
            except (TypeError, ValueError):
                advice["risk_multiplier"] = 1.0
            mrp = final.get("max_risk_pct")
            try:
                advice["max_risk_pct"] = max(0.0, min(100.0, float(mrp))) if mrp is not None else None
            except (TypeError, ValueError):
                advice["max_risk_pct"] = None
            try:
                advice["confidence"] = max(0, min(100, int(float(final.get("confidence", 0)))))
            except (TypeError, ValueError):
                advice["confidence"] = 0
            chair_summary = str(final.get("chair_summary", "")).strip()
            reasoning = str(final.get("reasoning", "")).strip()
            if reasoning:
                advice["reasoning"] = reasoning
        else:
            chair_summary = ""
            advice["reasoning"] = "Council unavailable - neutral by default (never blocks on an error)."

        if advice["verdict"] == "POOR":
            advice["risk_multiplier"] = min(advice["risk_multiplier"], 0.4)

        advice["discussion"] = {
            "analyst": analyst[:800],
            "bull": bull,
            "bear": bear,
            "risk": chair_summary[:400],
        }
        advice["trace"] = trace
        advice["period_days"] = 30
        advice["created_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        with self._lock:
            self._advice = advice
        try:
            os.makedirs(os.path.dirname(ADVICE_FILE), exist_ok=True)
            with open(ADVICE_FILE, "w", encoding="utf-8") as f:
                json.dump(advice, f, ensure_ascii=False)
        except Exception:
            pass
        sb = get_supabase()
        if sb.enabled:
            try:
                sb.upsert_venture_advice(advice)
            except Exception:
                pass
        logger.info(
            "Council verdict: %s (mult %.2f, conf %d) in %.1fs [%s]",
            advice.get("verdict"), advice.get("risk_multiplier", 1.0),
            advice.get("confidence", 0), time.time() - started, "; ".join(trace),
        )


_singleton = None


def get_venture_engine():
    global _singleton
    if _singleton is None:
        _singleton = VentureEngine()
    return _singleton


def get_venture_advice():
    """Gate input. Advisory mode (switch OFF) ALWAYS yields NEUTRAL / 1.0."""
    if not is_venture_enabled():
        return dict(DEFAULT_ADVICE)
    if _singleton is not None:
        return _singleton.current_advice()
    try:
        if os.path.exists(ADVICE_FILE):
            with open(ADVICE_FILE, "r", encoding="utf-8") as f:
                return {**DEFAULT_ADVICE, **json.load(f)}
    except Exception:
        pass
    return dict(DEFAULT_ADVICE)


def get_venture_read():
    """Latest council read regardless of the switch (for display)."""
    if _singleton is not None:
        return _singleton.current_advice()
    try:
        if os.path.exists(ADVICE_FILE):
            with open(ADVICE_FILE, "r", encoding="utf-8") as f:
                return {**DEFAULT_ADVICE, **json.load(f)}
    except Exception:
        pass
    return dict(DEFAULT_ADVICE)
