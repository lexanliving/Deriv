"""src/venture_engine.py — the Venture Council: signal-triggered entry review.

Powered by the SAME failover chain as the Trading Brain (src.brain_llm:
Groq -> OpenRouter -> Cerebras -> OpenAI). Whichever API keys are configured
become the council.

HOW IT WORKS (signal-synced, not timer-synced):

  * MomentumMaster TF evaluates trigger candles ON CANDLE CLOSE: the 5m candle
    for 1/2/5/15-minute contracts and the 15m candle for 30/60-minute
    contracts. The council lives on that same rhythm — its background market
    watch refreshes once per trigger candle, never on an arbitrary timer.

  * When the strategy fires a TRUE signal (every hard gate and the score
    threshold already passed), the engine hands that exact signal to the
    council for ONE fast review call: BULL argues the entry, BEAR argues
    against it, the CHAIR decides.

  * THE CHAIR ONLY ALLOWS OR REFUSES:
        PROCEED -> the trade executes EXACTLY as the bot planned it, at the
                   full stake from the user's configured stake plan
                   (starting stake + martingale). The council NEVER sizes,
                   shrinks, or scales the stake.
        SKIP    -> the setup is skipped and journaled with the council's reason

  * Judgement basis: CURRENT MARKET CONDITIONS ONLY (trend, multi-timeframe
    agreement, momentum, volatility regime, candle structure, price vs EMAs),
    the contract DURATION, and broad trading knowledge. The bot's own trade
    history is never provided and can never influence the verdict.

  * FAIL-OPEN discipline: no keys, a timeout, or an unreadable reply all mean
    PROCEED. The council can only skip a setup when it is sure; uncertainty or
    absence never blocks a technically qualified entry.

  * Dashboard switch (unchanged): ON = the council may allow/refuse entries;
    OFF = advisory only, never consulted at signal time.
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

_enabled = True  # dashboard toggle (ON = council may allow/refuse entries)
_trade_context = {"symbol": None, "duration_minutes": None, "sensitivity": None}


def set_venture_enabled(on: bool) -> None:
    global _enabled
    _enabled = bool(on)


def is_venture_enabled() -> bool:
    return _enabled


def bind_trade_setup(symbol=None, duration_minutes=None, sensitivity=None) -> None:
    """The trading engine calls this on start so the council knows the setup."""
    try:
        if symbol:
            _trade_context["symbol"] = str(symbol)
        if duration_minutes:
            _trade_context["duration_minutes"] = int(duration_minutes)
        if sensitivity:
            _trade_context["sensitivity"] = str(sensitivity)
    except Exception:
        pass


def _trigger_seconds() -> int:
    """Trigger-candle period for the configured duration (5m or 15m)."""
    try:
        from config import ENTRY_TIMEFRAME_BY_DURATION
        dur = _trade_context.get("duration_minutes")
        tf = ENTRY_TIMEFRAME_BY_DURATION.get(int(dur), "5m") if dur else "5m"
        return 900 if tf == "15m" else 300
    except Exception:
        return 300


def _duration_guidance(minutes):
    try:
        minutes = int(minutes)
    except (TypeError, ValueError):
        return ("Duration unknown: judge for a generic short binary-option trend entry. "
                "Uncertainty must resolve to PROCEED.")
    if minutes <= 2:
        return (f"Duration {minutes}m (scalp): needs clean immediate momentum and a decisive trigger; chop or "
                "dead volatility kills it fast, a healthy impulse carries 1-2 minutes easily. Only SKIP on clear, "
                "immediate hostility to the move.")
    if minutes <= 15:
        return (f"Duration {minutes}m (short): needs genuine trending 5m tape with follow-through. Mild pullbacks "
                "can recover inside the window; flat EMAs, opposite higher-timeframe bias, or volatility spikes "
                "are the real threats.")
    if minutes <= 30:
        return (f"Duration {minutes}m (medium): 30m and 1h agreement matters more than the last candle; normal "
                "noise is fine. The threats are a late-stage stretched trend or a regime flip mid-contract.")
    return (f"Duration {minutes}m (long): higher-timeframe structure dominates; short-term wiggles are irrelevant, "
            "exhaustion and divergence are not. Requires a sustained trend with intact structure.")


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
        logger.info("Venture council armed (signal-triggered reviews on the trigger-candle rhythm).")

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

    # ---- live market snapshot ------------------------------------------------
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
                if len(closes) >= 13 and closes[-13] > 0:
                    snap["momentum_1h_pct"] = round((closes[-1] - closes[-13]) / closes[-13] * 100.0, 4)
                if len(closes) >= 4 and closes[-4] > 0:
                    snap["momentum_15m_pct"] = round((closes[-1] - closes[-4]) / closes[-4] * 100.0, 4)
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

    # ---- background watch on the trigger rhythm (deterministic, no LLM) -------
    def _run(self):
        time.sleep(8)
        while not self._stop.is_set():
            try:
                self._market_watch()
            except Exception as exc:
                logger.error("Market watch failed: %s", exc)
            self._stop.wait(self._seconds_to_next_trigger())

    def _seconds_to_next_trigger(self) -> float:
        period = _trigger_seconds()
        now = time.time()
        next_boundary = (int(now // period) + 1) * period
        return max(5.0, (next_boundary + 35.0) - now)

    def _market_watch(self):
        snap = self._market_snapshot()
        watch = dict(DEFAULT_ADVICE)
        watch["verdict"] = "NEUTRAL"
        watch["risk_multiplier"] = 1.0
        watch["reasoning"] = ("Market watch on the trigger-candle rhythm. The council spends its judgement "
                              "on fired signals only — no timer verdicts between candles.")
        watch["discussion"] = {
            "trend": str(snap.get("trend_direction") or "-"),
            "mtf": str(snap.get("mtf_tf_biases") or {}),
            "vol_regime": str(snap.get("vol_regime") or "-"),
            "momentum_1h_pct": snap.get("momentum_1h_pct"),
            "structure_5m": str(snap.get("structure_5m") or "-"),
        }
        watch["created_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        with self._lock:
            self._advice = watch
        try:
            os.makedirs(os.path.dirname(ADVICE_FILE), exist_ok=True)
            with open(ADVICE_FILE, "w", encoding="utf-8") as f:
                json.dump(watch, f, ensure_ascii=False)
        except Exception:
            pass
        logger.info("Market watch refreshed (trigger rhythm %ds).", _trigger_seconds())

    # ---- THE SIGNAL REVIEW (the real gate: allow or refuse, never size) --------
    def review_signal(self, signal, entry_price=None):
        """One fast council review of a fired signal. FAIL-OPEN on any problem."""
        base_result = {
            "decision": "PROCEED",
            "risk_multiplier": 1.0,
            "confidence": 0,
            "reasoning": "",
            "discussion": {},
            "signal": signal,
            "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        }
        if not is_venture_enabled():
            base_result["reasoning"] = "Council control is OFF — advisory only."
            return base_result

        snap = self._market_snapshot()
        snap["signal_under_review"] = signal
        if entry_price is not None:
            snap["entry_price"] = entry_price
        data = json.dumps(snap, default=str)
        duration = _trade_context.get("duration_minutes")
        guidance = _duration_guidance(duration)
        dur_txt = f"{duration} minutes" if duration else "the configured duration"

        prompt = (
            "CONTEXT: You are the Venture Council for 'MomentumMaster TF', a Deriv binary-options "
            "trend-following bot. A contract is UP/DOWN (CALL/PUT): it wins if price is above/below the "
            "entry at expiry. There is NO stop-loss and NO early exit — entry quality and duration fit "
            "are everything.\n"
            f"A live {signal} signal has JUST FIRED on a closed trigger candle for a {dur_txt} contract. "
            "Every hard gate ALREADY PASSED: higher-timeframe trend agreement for this contract length, "
            "EMAs not flat, entry-timeframe ADX floor, volatility inside the allowed band, a real trigger "
            "break of the prior candle, close beyond the fast EMA, the express-aware exhaustion limit, no "
            "RSI/price divergence, intact entry-timeframe structure, and the regime gates. The confluence "
            "score also cleared its threshold. The technical system has fully qualified this entry.\n"
            "YOUR JOB: one fast confirmation — is this entry the right call RIGHT NOW, for this duration?\n"
            "YOU ONLY ALLOW OR REFUSE. You never size the trade: a PROCEED executes at the full stake from "
            "the bot's own stake plan, untouched. Your only outputs are PROCEED (allow) or SKIP (refuse).\n"
            "Judge ONLY the current market conditions below plus your broad trading knowledge. You receive "
            "NO trade history and must never reason from, or ask for, past results.\n"
            "DECISION RULES:\n"
            f"- PROCEED: allow the entry. This is the DEFAULT whenever you are not sure. Conditions support "
            f"this entry for its {dur_txt} life.\n"
            "- SKIP: refuse the entry. ONLY when you are sure the market will punish this entry within the "
            "contract duration — the tape has clearly gone flat/choppy since the trigger, a hard reversal is "
            "already printing, volatility is spiking against the direction, or the move is visibly exhausted "
            "into a wall. Uncertainty, ambiguity, or missing data must NEVER produce a SKIP.\n"
            f"DURATION FIT: {guidance}\n"
            f"MARKET SNAPSHOT (live): {data}\n"
            "Deliberate in order: the BULL case for the entry, the BEAR case against it, then the CHAIR's "
            "final decision.\n"
            "ANSWER ONLY JSON:\n"
            '{"decision":"PROCEED|SKIP","confidence":0,'
            '"bull":"max 60 words","bear":"max 60 words","chair":"one sentence",'
            '"reasoning":"2 sentences citing snapshot numbers"}'
        )

        try:
            from src import brain_llm
            text = brain_llm.chat_with_chain(
                [{"role": "user", "content": prompt}],
                temperature=0.1,
                max_tokens=560,
            )
        except Exception as exc:
            logger.warning("Council unavailable at signal; PROCEED by default: %s", exc)
            base_result["reasoning"] = "Council unavailable — the technical gates stand."
            self._publish(base_result)
            return base_result

        obj = _extract_json(text)
        if not obj:
            base_result["reasoning"] = "Council reply unreadable — the technical gates stand."
            self._publish(base_result)
            return base_result

        decision = str(obj.get("decision", "")).strip().upper()
        if decision not in ("PROCEED", "SKIP"):
            base_result["reasoning"] = "Council verdict unclear — the technical gates stand."
            self._publish(base_result)
            return base_result

        try:
            conf = max(0, min(100, int(float(obj.get("confidence", 0)))))
        except (TypeError, ValueError):
            conf = 0
        result = {
            "decision": decision,
            "risk_multiplier": 1.0 if decision == "PROCEED" else 0.0,
            "confidence": conf,
            "reasoning": str(obj.get("reasoning", "")).strip()[:300],
            "discussion": {
                "bull": str(obj.get("bull", ""))[:400],
                "bear": str(obj.get("bear", ""))[:400],
                "chair": str(obj.get("chair", ""))[:300],
            },
            "signal": signal,
            "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        }
        self._publish(result)
        logger.info(
            "Council reviewed %s: %s (conf %d) — %s",
            signal, decision, conf, result["reasoning"] or "no reasoning",
        )
        return result

    def _publish(self, result):
        """Persist the review locally + Supabase (advice-table shape)."""
        advice = dict(DEFAULT_ADVICE)
        advice["verdict"] = result.get("decision", "PROCEED")
        advice["risk_multiplier"] = result.get("risk_multiplier", 1.0)
        advice["discussion"] = result.get("discussion", {})
        advice["reasoning"] = result.get("reasoning", "")
        advice["confidence"] = result.get("confidence", 0)
        advice["period_days"] = 30
        advice["created_at"] = result.get("created_at", "")
        with self._lock:
            self._advice = advice
        try:
            os.makedirs(os.path.dirname(ADVICE_FILE), exist_ok=True)
            with open(ADVICE_FILE, "w", encoding="utf-8") as f:
                json.dump(advice, f, ensure_ascii=False)
        except Exception:
            pass
        try:
            sb = get_supabase()
            if sb.enabled:
                sb.upsert_venture_advice(advice)
        except Exception:
            pass


_singleton = None


def get_venture_engine():
    global _singleton
    if _singleton is None:
        _singleton = VentureEngine()
    return _singleton


def review_signal(signal, entry_price=None):
    """Module-level entry point used by the trading engine (thread-safe)."""
    try:
        return get_venture_engine().review_signal(signal, entry_price)
    except Exception as exc:
        logger.warning("Council review crashed open: %s", exc)
        return {
            "decision": "PROCEED",
            "risk_multiplier": 1.0,
            "confidence": 0,
            "reasoning": "Council error — the technical gates stand.",
            "discussion": {},
            "signal": signal,
            "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        }


def get_venture_advice():
    """Backward-compatible read of the latest council output (never blocks)."""
    if _singleton is not None:
        return _singleton.current_advice()
    try:
        if os.path.exists(ADVICE_FILE):
            with open(ADVICE_FILE, "r", encoding="utf-8") as f:
                return {**DEFAULT_ADVICE, **json.load(f)}
    except Exception:
        pass
    return dict(DEFAULT_ADVICE)
