"""pages/research.py — Offline Research Lab (read-only analytics + backup + gate backtest)."""
import inspect
import json
import os
import sys

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

_here = os.path.dirname(os.path.abspath(__file__))
_root = os.path.dirname(_here)
for _c in (_here, _root):
    if os.path.isdir(os.path.join(_c, "src")) and _c not in sys.path:
        sys.path.insert(0, _c)
        break

from src.journal import get_journal
from src.persistence import (
    GATE_THRESHOLDS,
    WEIGHT_VARIANTS,
    build_learning_bundle,
    compute_postmortem,
    export_archive_csv_bytes,
    export_merged_json_bytes,
    export_preset_text,
    import_journal,
    snapshots_path,
    sweep_gates,
)

st.set_page_config(
    page_title="Research Lab",
    page_icon="🧪",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(
    """
<style>
@import url('https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;600;700&family=IBM+Plex+Sans:wght@400;500;600&family=JetBrains+Mono:wght@500;600;700&display=swap');

html,body,.stApp{
  background-color:#070b14;
  color:#c7d2e0;
  font-family:'IBM Plex Sans',sans-serif;
}

.stApp{
  background-image:
    radial-gradient(1000px 520px at 4% -10%, rgba(16,185,129,0.10), transparent 62%),
    radial-gradient(900px 480px at 102% 112%, rgba(56,132,255,0.10), transparent 60%),
    radial-gradient(rgba(120,150,190,0.045) 1px, transparent 1px);
  background-size:auto,auto,24px 24px;
  background-attachment:fixed;
}

[data-testid="stMainBlockContainer"]{
  max-width:1500px;
  padding-top:1.1rem;
}

[data-testid="stSidebar"]{
  background-color:#0a0f1c;
  border-right:1px solid #18233a;
}

.rl-header{
  display:flex;
  align-items:flex-end;
  justify-content:space-between;
  gap:18px;
  padding:6px 4px 16px 4px;
  position:relative;
  overflow:hidden;
}

.rl-header::after{
  content:"";
  position:absolute;
  left:4px;
  right:4px;
  bottom:0;
  height:2px;
  background:linear-gradient(90deg,#10b981,#3884ff 42%,transparent 94%);
  background-size:220% 100%;
  animation:rl-scan 7s linear infinite;
  border-radius:2px;
}

@keyframes rl-scan{
  0%{background-position:130% 0;}
  100%{background-position:-130% 0;}
}

.rl-title{
  font-family:'Space Grotesk',sans-serif;
  font-weight:700;
  font-size:1.18rem;
  letter-spacing:.14em;
  text-transform:uppercase;
  color:#eef3fb;
  margin:0;
  line-height:1;
}

.rl-sub{
  font-family:'Space Grotesk',sans-serif;
  font-size:.56rem;
  font-weight:600;
  letter-spacing:.24em;
  text-transform:uppercase;
  color:#4f6080;
  margin-top:5px;
}

.rl-panel{
  background:linear-gradient(160deg,#0c1322,#0a101d);
  border:1px solid #18233a;
  border-radius:15px;
  padding:16px 18px;
  margin:12px 0;
  box-shadow:inset 0 1px 0 rgba(255,255,255,.035);
}

.rl-panel h3{
  font-family:'Space Grotesk',sans-serif;
  font-size:.68rem;
  font-weight:700;
  letter-spacing:.18em;
  text-transform:uppercase;
  color:#8294b0;
  margin:0 0 10px 0;
}

.rl-note{
  font-size:.82rem;
  line-height:1.55;
  color:#bcd2f5;
}

.rl-note ul{
  margin:8px 0 0 0;
  padding-left:18px;
}

.rl-note li{
  margin:5px 0;
}

.rl-glitch{
  margin:14px 0;
  padding:14px 16px;
  border-radius:13px;
  background:rgba(244,63,94,.07);
  border:1px solid rgba(244,63,94,.28);
}

.rl-glitch .t{
  font-family:'Space Grotesk',sans-serif;
  font-weight:600;
  color:#fb7185;
  font-size:.84rem;
  letter-spacing:.04em;
}

.rl-glitch .s{
  font-family:'JetBrains Mono',monospace;
  font-size:.7rem;
  color:#9fb0c9;
  margin-top:6px;
  line-height:1.5;
}

.pos{color:#4ade80;}
.neg{color:#fb7185;}
.mut{color:#6b7c97;}

[data-testid="stDataFrame"]{border:0;}
#MainMenu,footer{visibility:hidden;}
</style>
""",
    unsafe_allow_html=True,
)


def _tab_error(where: str, exc: Exception) -> None:
    st.markdown(
        f'<div class="rl-glitch"><div class="t">⚠ {where} hit a snag</div>'
        f'<div class="s">Your data is intact — this is a rendering edge case, not a trading fault. '
        f'The page controls stay live. Details are in the expander.</div></div>',
        unsafe_allow_html=True,
    )
    with st.expander("Technical details"):
        st.exception(exc)


def _plotly_kwargs():
    try:
        sig = inspect.signature(st.plotly_chart)
        if "use_container_width" in sig.parameters:
            return {"use_container_width": True}
        if "width" in sig.parameters:
            return {"width": 1000}
    except Exception:
        pass
    return {}


def _dataframe_kwargs(height: int = 320):
    kwargs = {}
    try:
        sig = inspect.signature(st.dataframe)
        if "use_container_width" in sig.parameters:
            kwargs["use_container_width"] = True
        if "height" in sig.parameters:
            kwargs["height"] = height
        if "hide_index" in sig.parameters:
            kwargs["hide_index"] = True
    except Exception:
        pass
    return kwargs


def _button_kwargs():
    try:
        sig = inspect.signature(st.download_button)
        if "use_container_width" in sig.parameters:
            return {"use_container_width": True}
    except Exception:
        pass
    return {}


def _file_token():
    journal = get_journal()
    toks = []

    for p in (getattr(journal, "_live", ""), getattr(journal, "_archive", ""), snapshots_path(journal)):
        try:
            if p and os.path.exists(p):
                s = os.stat(p)
                toks.append((p, s.st_mtime, s.st_size))
            else:
                toks.append((p, 0, 0))
        except Exception:
            toks.append((p, 0, 0))

    return tuple(toks)


@st.cache_data(ttl=8, show_spinner=False)
def _cached_postmortem(token):
    return compute_postmortem(get_journal())


@st.cache_data(ttl=8, show_spinner=False)
def _cached_sweep(token):
    return sweep_gates(get_journal())


@st.cache_data(ttl=8, show_spinner=False)
def _cached_bundle(token):
    return build_learning_bundle(get_journal())


def _edge_df(edge_dict):
    rows = []
    for k, v in (edge_dict or {}).items():
        rows.append(
            {
                "key": k,
                "trades": v.get("trades", 0),
                "wins": v.get("wins", 0),
                "win_rate": v.get("win_rate", 0.0),
                "pnl": v.get("pnl", 0.0),
            }
        )
    return pd.DataFrame(rows)


def _small_table(df: pd.DataFrame, height: int = 300):
    if df is None or df.empty:
        st.caption("No rows yet.")
        return
    st.dataframe(df, **_dataframe_kwargs(height))


st.markdown(
    """
<div class="rl-header">
  <div>
    <div class="rl-title">Research Lab</div>
    <div class="rl-sub">Offline learning loop · read-only · human-in-the-loop</div>
  </div>
</div>
""",
    unsafe_allow_html=True,
)

token = _file_token()

tab_send, tab_backup, tab_gates, tab_missed = st.tabs(
    ["SEND TO Q", "BACKUP", "GATE BACKTEST", "MISSED & AVOIDABLE"]
)

with tab_send:
    try:
        post = _cached_postmortem(token)
        s = post.get("summary", {})

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Reviews", s.get("reviews", 0))
        c2.metric("Closed", s.get("closed", 0))
        c3.metric("Win rate", f"{s.get('win_rate', 0.0):.1f}%")
        c4.metric("Net P&L", f"{s.get('net_pnl', 0.0):+.2f}")

        c5, c6, c7, c8 = st.columns(4)
        c5.metric("Wins", s.get("wins", 0))
        c6.metric("Losses", s.get("losses", 0))
        c7.metric("Taken", s.get("taken", 0))
        c8.metric("Snapshots", s.get("snapshots_recorded", 0))

        st.markdown(
            """
<div class="rl-panel">
  <h3>What I look for</h3>
  <div class="rl-note">
    <ul>
      <li><b>avoidable_losses</b> → price was in favour, then reversed before expiry. Lever: <b>duration / exit</b>.</li>
      <li><b>fragile_wins</b> → price nearly went against you and survived. Lever: <b>entry timing / noise filter</b>.</li>
      <li><b>gatekeeper_factors</b> → the one soft factor most often weakest on trending near-misses. Lever: <b>re-test that gate only</b>.</li>
      <li><b>edges</b> → where the closed-trade edge actually came from. Lever: <b>symbol / hour / regime selection</b>.</li>
    </ul>
  </div>
</div>
""",
            unsafe_allow_html=True,
        )

        bundle = _cached_bundle(token)
        post_json = json.dumps(post, indent=2, default=str).encode("utf-8")

        b1, b2 = st.columns(2)
        with b1:
            st.download_button(
                "Download learning bundle (zip)",
                data=bundle,
                file_name="momentummaster_learning_bundle.zip",
                mime="application/zip",
                **_button_kwargs(),
            )
        with b2:
            st.download_button(
                "Download postmortem.json",
                data=post_json,
                file_name="postmortem.json",
                mime="application/json",
                **_button_kwargs(),
            )

        st.caption(
            "Nothing here auto-changes the bot. This bundle is for offline review, then manual demo forward-testing."
        )

    except Exception as e:
        _tab_error("SEND TO Q", e)

with tab_backup:
    try:
        journal = get_journal()

        archive_csv = export_archive_csv_bytes(journal)
        merged_json = export_merged_json_bytes(journal)

        b1, b2 = st.columns(2)
        with b1:
            st.download_button(
                "Export master archive CSV",
                data=archive_csv,
                file_name="journal_archive.csv",
                mime="text/csv",
                **_button_kwargs(),
            )
        with b2:
            st.download_button(
                "Export merged JSON",
                data=merged_json,
                file_name="journal_merged.json",
                mime="application/json",
                **_button_kwargs(),
            )

        st.markdown(
            """
<div class="rl-panel">
  <h3>Import backup</h3>
  <div class="rl-note">
    Accepts the archive CSV or merged JSON. Import is idempotent by <b>signal_id</b>: importing the same file twice is a no-op the second time.
  </div>
</div>
""",
            unsafe_allow_html=True,
        )

        uploaded = st.file_uploader("Backup file", type=["csv", "json"])

        if uploaded is not None:
            if st.button("Import", type="primary"):
                try:
                    result = import_journal(journal, uploaded.getvalue(), uploaded.name)
                    st.cache_data.clear()
                    st.success("Import completed.")
                    st.json(result)
                except Exception as e:
                    _tab_error("Import", e)

        if "research_import_result" in st.session_state:
            st.json(st.session_state["research_import_result"])

    except Exception as e:
        _tab_error("BACKUP", e)

with tab_gates:
    try:
        sweep = _cached_sweep(token)
        df = pd.DataFrame(sweep)

        st.markdown(
            """
<div class="rl-panel">
  <h3>Gate backtest</h3>
  <div class="rl-note">
    The <b>AS-RECORDED</b> row is ground truth. Variant rows are offline what-if recomputations only.
    <b>added_unknown</b> is not claimed as profit — it needs a forward test.
  </div>
</div>
""",
            unsafe_allow_html=True,
        )

        _small_table(df, height=420)

        st.markdown("### Export preset")

        c1, c2 = st.columns(2)
        with c1:
            variant = st.selectbox("Weight variant", list(WEIGHT_VARIANTS.keys()), index=0)
        with c2:
            threshold = st.selectbox("Threshold", GATE_THRESHOLDS, index=2)

        row = next(
            (
                r
                for r in sweep
                if str(r.get("variant")) == str(variant) and int(r.get("threshold", -1)) == int(threshold)
            ),
            None,
        )

        preset_text = export_preset_text(variant, threshold, row)
        st.code(preset_text, language="text")

        st.download_button(
            "Download preset text",
            data=preset_text.encode("utf-8"),
            file_name=f"offline_preset_{variant}_{threshold}.txt",
            mime="text/plain",
            **_button_kwargs(),
        )

    except Exception as e:
        _tab_error("GATE BACKTEST", e)

with tab_missed:
    try:
        post = _cached_postmortem(token)

        avoidable = post.get("avoidable_losses", [])
        fragile = post.get("fragile_wins", [])

        c1, c2 = st.columns(2, gap="small")

        with c1:
            st.markdown('<div class="rl-panel"><h3>Avoidable losses</h3></div>', unsafe_allow_html=True)
            df_av = pd.DataFrame(avoidable)
            if not df_av.empty:
                cols = [
                    "timestamp_utc",
                    "symbol",
                    "direction",
                    "regime",
                    "duration_min",
                    "score",
                    "mae",
                    "mfe",
                    "pnl",
                ]
                cols = [c for c in cols if c in df_av.columns]
                _small_table(df_av[cols], height=320)
            else:
                st.caption("No avoidable losses recorded.")

        with c2:
            st.markdown('<div class="rl-panel"><h3>Fragile wins</h3></div>', unsafe_allow_html=True)
            df_fr = pd.DataFrame(fragile)
            if not df_fr.empty:
                cols = [
                    "timestamp_utc",
                    "symbol",
                    "direction",
                    "regime",
                    "duration_min",
                    "score",
                    "mae",
                    "mfe",
                    "pnl",
                ]
                cols = [c for c in cols if c in df_fr.columns]
                _small_table(df_fr[cols], height=320)
            else:
                st.caption("No fragile wins recorded.")

        st.markdown('<div class="rl-panel"><h3>Gatekeeper factors</h3></div>', unsafe_allow_html=True)

        gate = post.get("gatekeeper_factors", {})
        if gate:
            gdf = pd.DataFrame(list(gate.items()), columns=["factor", "count"]).sort_values(
                "count", ascending=False
            )

            fig = go.Figure(
                go.Bar(
                    x=gdf["factor"],
                    y=gdf["count"],
                    marker_color="#3884ff",
                    hovertemplate="%{x}<br>count <b>%{y}</b><extra></extra>",
                )
            )
            fig.update_layout(
                paper_bgcolor="#0a101d",
                plot_bgcolor="#0a101d",
                font=dict(color="#8294b0", family="JetBrains Mono", size=10),
                margin=dict(l=8, r=8, t=8, b=8),
                xaxis=dict(gridcolor="#13203a", zerolinecolor="#13203a"),
                yaxis=dict(gridcolor="#13203a", zerolinecolor="#13203a"),
                height=260,
                showlegend=False,
            )
            st.plotly_chart(fig, **_plotly_kwargs())
            _small_table(gdf, height=260)
        else:
            st.caption("No trending near-miss stand-asides recorded yet.")

        edges = post.get("edges", {})

        st.markdown('<div class="rl-panel"><h3>Edges</h3></div>', unsafe_allow_html=True)

        e1, e2, e3 = st.columns(3, gap="small")

        with e1:
            st.markdown("#### By symbol")
            _small_table(_edge_df(edges.get("by_symbol", {})), height=300)

        with e2:
            st.markdown("#### By hour (UTC)")
            _small_table(_edge_df(edges.get("by_hour", {})), height=300)

        with e3:
            st.markdown("#### By regime")
            _small_table(_edge_df(edges.get("by_regime", {})), height=300)

    except Exception as e:
        _tab_error("MISSED & AVOIDABLE", e)
