"""src/ai_engine.py — generates the per-trade research record.

Uses the ALREADY-CONFIGURED AI failover chain (src/brain_llm.py). The AI is asked
to compare the new trade with prior research/knowledge BEFORE concluding, and to
answer in strict JSON. A deterministic fallback guarantees a record even if every
provider is down (model = 'deterministic-fallback').
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple

from src import analytics
from src.logger import get_logger

logger = get_logger("ai_engine")

try:
    from src import brain_llm as _llm
except Exception:  # brain not configured -> deterministic mode
    _llm = None

_SCHEMA = ("entry_analysis, exit_analysis, strategy_adherence, market_behaviour, "
           "confidence (0-100), strengths[], weaknesses[], mistakes[], pattern_detected, "
           "risk_observations[], suggested_improvements[], technical_explanation, ai_summary")


def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    text = re.sub(r"```(?:json)?", "", text or "")
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def _fallback_record(ctx: Dict[str, Any]) -> Dict[str, Any]:
    prof = ctx.get("factor_profile", {})
    strong, weak = analytics.strongest_weakest(prof)
    mae, mfe = ctx.get("mae"), ctx.get("mfe")
    outcome = ctx.get("outcome")
    exit_analysis = "No excursion data recorded."
    if mae is not None and mfe is not None:
        if outcome == "LOST" and mfe > 0 and mfe > mae * 0.5:
            exit_analysis = ("Price was in profit (MFE %.5f) then reversed before expiry — a "
                             "duration/exit issue, not an entry issue." % mfe)
        elif outcome == "WON" and mae > 0 and mae > mfe * 0.5:
            exit_analysis = ("Won after a full-risk scare (MAE %.5f) — survived on timing; review "
                             "entry timing / stop noise." % mae)
        elif outcome == "WON":
            exit_analysis = "Clean run; price moved favourably with negligible drawdown."
        else:
            exit_analysis = "Price moved against the position from the start."
    score = ctx.get("score")
    thr = ctx.get("threshold")
    adherence = ("All hard gates passed and score %s met threshold %s." % (score, thr)
                 if score is not None and thr is not None and score >= thr
                 else "Setup did not clear the live threshold; review which gate blocked.")
    return {
        "entry_analysis": "Strongest factors: %s. Weakest: %s." % (", ".join(strong) or "n/a", ", ".join(weak) or "n/a"),
        "exit_analysis": exit_analysis,
        "strategy_adherence": adherence,
        "market_behaviour": "Regime %s; MTF biases %s." % (ctx.get("regime", "?"), ctx.get("tf_biases", {})),
        "confidence": int(round((score / 25.0) * 100)) if score is not None else 0,
        "strengths": strong, "weaknesses": weak,
        "mistakes": ["duration too long for the move"] if (outcome == "LOST" and mfe and mae is not None and mfe > mae * 0.5) else [],
        "pattern_detected": ctx.get("regime"),
        "risk_observations": ["martingale step %s" % ctx.get("martingale_step")] if ctx.get("martingale_step") else [],
        "suggested_improvements": ["collect more samples before changing gates"] ,
        "technical_explanation": "Deterministic fallback record generated without an LLM.",
        "ai_summary": "Fallback analysis (AI unavailable). Entry/exit read from factor breakdown and MAE/MFE.",
    }


def generate_research(ctx: Dict[str, Any], prior_knowledge: List[Dict[str, Any]],
                      prior_research: List[Dict[str, Any]]) -> Tuple[Dict[str, Any], str]:
    prof = ctx.get("factor_profile", {})
    strong, weak = analytics.strongest_weakest(prof)
    prompt = (
        "You are a senior quantitative researcher for a Deriv binary-options trend bot. "
        "Compare the NEW trade below with the PRIOR KNOWLEDGE and PRIOR RESEARCH before concluding. "
        "Answer with ONLY a JSON object with keys: " + _SCHEMA + ".\n\n"
        f"NEW TRADE: symbol={ctx.get('symbol')} direction={ctx.get('direction')} outcome={ctx.get('outcome')} "
        f"pnl={ctx.get('pnl')} score={ctx.get('score')}/{ctx.get('threshold')} regime={ctx.get('regime')} "
        f"mtf={ctx.get('tf_biases')} mae={ctx.get('mae')} mfe={ctx.get('mfe')} "
        f"martingale_step={ctx.get('martingale_step')} factor_profile={prof} "
        f"strong={strong} weak={weak} rejection={ctx.get('rejection') or 'none'}\n\n"
        f"SESSION STATS: {ctx.get('stats')}\n"
        f"PRIOR KNOWLEDGE: {prior_knowledge}\n"
        f"PRIOR RESEARCH: {prior_research}\n"
    )
    if _llm is not None:
        try:
            text = _llm.chat_with_chain([{"role": "user", "content": prompt}], max_tokens=1200)
            obj = _extract_json(text)
            if obj:
                obj.setdefault("ai_summary", text[:400])
                return obj, "llm-chain"
        except Exception as exc:
            logger.warning("AI research generation failed; using fallback: %s", exc)
    return _fallback_record(ctx), "deterministic-fallback"
