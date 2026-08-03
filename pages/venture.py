"""pages/venture.py — Venture Advisory: the AI panel's live verdict on the venture."""
import os
import sys

import streamlit as st

_here = os.path.dirname(os.path.abspath(__file__))
for _c in (_here, os.path.dirname(_here)):
    if os.path.isdir(os.path.join(_c, "src")) and _c not in sys.path:
        sys.path.insert(0, _c)
        break

from src.venture_engine import get_venture_advice

st.set_page_config(page_title="Venture Advisory", page_icon="🧭", layout="wide")

st.markdown(
    """
<style>
html,body,.stApp{background:#070b14;color:#c7d2e0;font-family:'IBM Plex Sans',sans-serif;}
.va-card{background:linear-gradient(160deg,#0c1322,#0a101d);border:1px solid #18233a;border-radius:15px;padding:18px 20px;margin:12px 0;}
.va-verdict{font-family:'Space Grotesk',sans-serif;font-weight:800;font-size:2rem;letter-spacing:.04em;}
.va-good{color:#4ade80;} .va-caution{color:#fbbf24;} .va-poor{color:#fb7185;} .va-neutral{color:#8294b0;}
.va-voice{background:#0b1220;border:1px solid #18233a;border-radius:12px;padding:12px 14px;margin:8px 0;font-size:.82rem;line-height:1.55;}
.va-voice .who{font-family:'Space Grotesk',sans-serif;font-size:.6rem;font-weight:700;letter-spacing:.14em;text-transform:uppercase;margin-bottom:5px;}
.va-bull .who{color:#4ade80;} .va-bear .who{color:#fb7185;} .va-risk .who{color:#fbbf24;}
</style>
""",
    unsafe_allow_html=True,
)

st.title("🧭 Venture Advisory")
st.caption("The AI panel re-scans our stored research every few minutes and advises whether it's a good venture and how much to risk. Read-only; the gate applies it automatically.")

advice = get_venture_advice()
verdict = str(advice.get("verdict", "NEUTRAL")).upper()
vcls = {"GOOD": "va-good", "CAUTION": "va-caution", "POOR": "va-poor"}.get(verdict, "va-neutral")
mult = float(advice.get("risk_multiplier", 1.0) or 1.0)

st.markdown(
    f'<div class="va-card">'
    f'<div class="va-verdict {vcls}">{verdict}</div>'
    f'<div style="margin-top:6px;font-size:.85rem;">Risk multiplier <b>{mult:.2f}</b> · '
    f'confidence {advice.get("confidence", 0)} · period {advice.get("period_days", 30)}d · '
    f'updated {advice.get("created_at") or "—"}</div>'
    f'<div style="margin-top:10px;font-size:.85rem;">{advice.get("reasoning", "")}</div>'
    f'</div>',
    unsafe_allow_html=True,
)

disc = advice.get("discussion", {}) or {}
if disc:
    c1, c2, c3 = st.columns(3)
    with c1:
        st.markdown(f'<div class="va-voice va-bull"><div class="who">Bull</div>{disc.get("bull", "")}</div>', unsafe_allow_html=True)
    with c2:
        st.markdown(f'<div class="va-voice va-bear"><div class="who">Bear</div>{disc.get("bear", "")}</div>', unsafe_allow_html=True)
    with c3:
        st.markdown(f'<div class="va-voice va-risk"><div class="who">Risk</div>{disc.get("risk", "")}</div>', unsafe_allow_html=True)

st.caption("Verdict POOR blocks new entries; the multiplier scales stake (never above your configured max). Offline or AI-down ⇒ neutral, multiplier 1.0 — the bot trades as normal.")
