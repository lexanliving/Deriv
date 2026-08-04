"""pages/council.py — live proof the Council is connected and reviewing."""
import json, os, sys
import streamlit as st
_here = os.path.dirname(os.path.abspath(__file__))
for _c in (_here, os.path.dirname(_here)):
    if os.path.isdir(os.path.join(_c, "src")) and _c not in sys.path:
        sys.path.insert(0, _c); break
from src.venture_engine import is_venture_enabled
from src.council.council import CAL_FILE, get_calibration
from src.supabase_service import get_supabase
from src.research_engine import get_research_engine
st.set_page_config(page_title="Council", page_icon="🧠", layout="wide")
st.title("🧠 Council — live decision monitor")

def _read_cal(n=300):
    if not os.path.exists(CAL_FILE):
        return []
    try:
        with open(CAL_FILE, "r", encoding="utf-8") as f:
            return [json.loads(l) for l in f.readlines()[-n:] if l.strip()]
    except Exception:
        return []

entries = _read_cal()
cal = get_calibration()
sb = get_supabase()
re_started = bool(getattr(get_research_engine(), "_started", False))

c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Council", "ON" if is_venture_enabled() else "OFF")
c2.metric("Reviews", cal["reviews"])
c3.metric("Approval rate", f"{cal['approval_rate']:.0f}%")
c4.metric("Supabase", "connected" if sb.enabled else "off")
c5.metric("Research engine", "running" if re_started else "not started")

st.caption("If **Reviews** increases as signals fire, the Council is actively reviewing. "
           "If it stays 0 while trades happen, the Council isn't wired in.")

st.subheader("Recent decisions")
if entries:
    import pandas as pd
    rows = []
    for e in reversed(entries):
        rows.append({"time": e.get("ts", ""), "symbol": e.get("symbol", ""), "dir": e.get("direction", ""),
                     "outcome": e.get("outcome", ""), "conf": e.get("confidence", ""),
                     "think_ms": e.get("thinking_ms", ""), "reason": (e.get("reasoning") or "")[:90]})
    st.dataframe(pd.DataFrame(rows).head(15), use_container_width=True, hide_index=True)
else:
    st.info("No council reviews yet. The Council reviews each candidate trade the moment a signal fires — "
            "once your strategy produces a signal, decisions will appear here within seconds.")

st.subheader("Calibration (is it over-filtering?)")
a, b, c = st.columns(3)
a.json({"approved": cal["approved"], "caution": cal["caution"], "rejected": cal["rejected"],
        "low_confidence": cal["low_confidence"]})
b.json({"hard_rejects": cal["hard_rejects"] or "none"})
c.json({"factor_avg (low = over-penalising)": cal["factor_avg"] or "no data yet"})
st.caption("A factor with a persistently low `factor_avg` on approved trades is over-penalising; "
           "a hard rule with a high count is doing most of the rejecting.")
