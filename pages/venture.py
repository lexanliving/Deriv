"""pages/venture.py — Venture council status + on/off (per-trade council)."""
import os, sys
import streamlit as st
_here = os.path.dirname(os.path.abspath(__file__))
for _c in (_here, os.path.dirname(_here)):
    if os.path.isdir(os.path.join(_c, "src")) and _c not in sys.path:
        sys.path.insert(0, _c); break
from src.venture_engine import set_venture_enabled, is_venture_enabled
from src.journal import get_journal
st.set_page_config(page_title="Venture Council", page_icon="🧭", layout="wide")
st.title("🧭 Venture Council")
st.caption("The council reviews each trade entry in real time and approves or declines it. It uses only live market conditions — never past trades.")
on = st.toggle("Venture control (approve/decline entries)", value=is_venture_enabled(), key="venture_on")
set_venture_enabled(bool(on))
if on:
    st.success("Council ON — entries are reviewed and can be declined.")
else:
    st.warning("Council OFF — entries proceed without council review.")
st.subheader("Recent council decisions")
try:
    rows = get_journal().read_rows()
    dec = [r for r in rows if "venture council" in (r.get("note") or "").lower()]
    if dec:
        import pandas as pd
        df = pd.DataFrame(dec).tail(15).iloc[::-1]
        cols = [c for c in ["timestamp_utc", "symbol", "direction", "outcome", "note"] if c in df.columns]
        st.dataframe(df[cols], use_container_width=True, hide_index=True)
    else:
        st.caption("No council decisions yet. Decisions appear when a signal is reviewed.")
except Exception:
    st.caption("Could not load decisions.")
