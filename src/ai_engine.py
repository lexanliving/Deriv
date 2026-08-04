"""src/ai_engine.py — generates the per-trade research record (uses brain_llm chain)."""
from __future__ import annotations
import json, re
from typing import Any, Dict, Tuple
from src import analytics
from src.logger import get_logger
logger = get_logger("ai_engine")
try:
    from src import brain_llm as _llm
except Exception:
    _llm = None
_SCHEMA = ("entry_analysis, exit_analysis, strategy_adherence, market_behaviour, confidence (0-100), strengths[], weaknesses[], mistakes[], pattern_detected, risk_observations[], suggested_improvements[], technical_explanation, ai_summary")
def _extract_json(text):
    text = re.sub(r"```(?:json)?", "", text or "")
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0)); return obj if isinstance(obj, dict) else None
    except Exception:
        return None
def _fallback_record(ctx):
    prof = ctx.get("factor_profile", {}); strong, weak = analytics.strongest_weakest(prof)
    mae, mfe = ctx.get("mae"), ctx.get("mfe"); outcome = ctx.get("outcome")
    if mae is not None and mfe is not None:
        if outcome == "LOST" and mfe > 0 and mfe > mae * 0.5:
            exit_analysis = f"Price was in profit (MFE {mfe:.5f}) then reversed — a duration/exit issue."
        elif outcome == "WON" and mae > 0 and mae > mfe * 0.5:
            exit_analysis = f"Won after a full-risk scare (MAE {mae:.5f}) — review entry timing."
        elif outcome == "WON":
            exit_analysis = "Clean run; negligible drawdown."
        else:
            exit_analysis = "Price moved against the position from the start."
    else:
        exit_analysis = "No excursion data recorded."
    score, thr = ctx.get("score"), ctx.get("threshold")
    adherence = ("All hard gates passed and score %s met threshold %s." % (score, thr)) if (score is not None and thr is not None and score >= thr) else "Setup did not clear the live threshold."
    return {"entry_analysis": f"Strongest: {', '.join(strong) or 'n/a'}. Weakest: {', '.join(weak) or 'n/a'}.",
            "exit_analysis": exit_analysis, "strategy_adherence": adherence,
            "market_behaviour": f"Regime {ctx.get('regime', '?')}; MTF {ctx.get('tf_biases', {})}.",
            "confidence": int(round((score / 25.0) * 100)) if score is not None else 0,
            "strengths": strong, "weaknesses": weak,
            "mistakes": (["duration too long for the move"] if (outcome == "LOST" and mfe and mae is not None and mfe > mae * 0.5) else []),
            "pattern_detected": ctx.get("regime"),
            "risk_observations": ([f"martingale step {ctx.get('martingale_step')}"] if ctx.get("martingale_step") else []),
            "suggested_improvements": ["collect more samples before changing gates"],
            "technical_explanation": "Deterministic fallback record generated without an LLM.",
            "ai_summary": "Fallback analysis (AI unavailable)."}
def generate_research(ctx, prior_knowledge, prior_research) -> Tuple[Dict[str, Any], str]:
    prof = ctx.get("factor_profile", {}); strong, weak = analytics.strongest_weakest(prof)
    prompt = ("You are a senior quant researcher for a Deriv binary-options trend bot. Compare the NEW trade with PRIOR KNOWLEDGE and PRIOR RESEARCH before concluding. Answer ONLY JSON with keys: " + _SCHEMA + ".\n\n"
      f"NEW TRADE: symbol={ctx.get('symbol')} direction={ctx.get('direction')} outcome={ctx.get('outcome')} pnl={ctx.get('pnl')} score={ctx.get('score')}/{ctx.get('threshold')} regime={ctx.get('regime')} mtf={ctx.get('tf_biases')} mae={ctx.get('mae')} mfe={ctx.get('mfe')} martingale_step={ctx.get('martingale_step')} factor_profile={prof} strong={strong} weak={weak} rejection={ctx.get('rejection') or 'none'}\n"
      f"SESSION STATS: {ctx.get('stats')}\nPRIOR KNOWLEDGE: {prior_knowledge}\nPRIOR RESEARCH: {prior_research}\n")
    if _llm is not None:
        try:
            text = _llm.chat_with_chain([{"role": "user", "content": prompt}], max_tokens=1200)
            obj = _extract_json(text)
            if obj:
                obj.setdefault("ai_summary", text[:400]); return obj, "llm-chain"
        except Exception as exc:
            logger.warning("AI research failed; fallback: %s", exc)
    return _fallback_record(ctx), "deterministic-fallback"
