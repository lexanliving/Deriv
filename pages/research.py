"""pages/research.py — Offline research loop for MomentumMaster TF.

Read-only Streamlit page.
Places no trades.
Mutates no strategy.
Auto-appears in the sidebar via Streamlit multipage.
"""

import html
import inspect
import json
import os
import sys

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)

for _candidate in (_ROOT, _HERE):
    if os.path.isdir(os.path.join(_candidate, "src")) and _candidate not in sys.path:
        sys.path.insert(0, _candidate)
        break

from src.journal import get_journal
from src.persistence import (
    SNAPSHOT_FILE,
    SWEEP_THRESHOLDS,
    WEIGHT_VARIANTS,
    build_learning_bundle,
    compute_postmortem,
    export_archive_csv_bytes,
    export_merged_json_bytes,
    export_preset_text,
    import_journal,
    sweep_gates,
)

st.set_page_config(
    page_title="Research Loop",
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

.rl-topbar{
    display:flex;
    align-items:flex-end;
    justify-content:space-between;
    gap:18px;
    padding:6px 4px 16px 4px;
    position:relative;
    overflow:hidden;
}

.rl-topbar::after{
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

.rl-brand h1{
    font-family:'Space Grotesk',sans-serif;
    font-weight:700;
    font-size:1.18rem;
    letter-spacing:.14em;
    text-transform:uppercase;
    color:#eef3fb;
    margin:0;
    line-height:1;
}

.rl-brand .sub{
    font-family:'Space Grotesk',sans-serif;
    font-size:.56rem;
    font-weight:600;
    letter-spacing:.24em;
    text-transform:uppercase;
    color:#4f6080;
    margin-top:5px;
}

.rl-panel{
    position:relative;
    background:linear-gradient(160deg,#0c1322,#0a101d);
    border:1px solid #18233a;
    border-radius:15px;
    padding:16px 18px;
    overflow:hidden;
    box-shadow:inset 0 1px 0 rgba(255,255,255,.035);
    margin-bottom:14px;
}

.rl-panel::before{
    content:"";
    position:absolute;
    top:0;
    left:0;
    right:0;
    height:1px;
    background:linear-gradient(90deg,transparent,rgba(120,160,220,.25),transparent);
}

.rl-h{
    font-family:'Space Grotesk',sans-serif;
    font-size:.62rem;
    font-weight:700;
    letter-spacing:.18em;
    text-transform:uppercase;
    color:#8294b0;
    margin-bottom:10px;
}

.rl-kpis{
    display:grid;
    grid-template-columns:repeat(4,1fr);
    gap:13px;
    margin:4px 0 16px 0;
}

@media(max-width:1100px){
    .rl-kpis{
        grid-template-columns:repeat(2,1fr);
    }
}

.rl-kpi{
    position:relative;
    background:linear-gradient(160deg,#0c1322,#0a101d);
    border:1px solid #18233a;
    border-radius:14px;
    padding:14px 16px;
    overflow:hidden;
}

.rl-kpi::after{
    content:"";
    position:absolute;
    left:0;
    top:0;
    bottom:0;
    width:3px;
    background:var(--ac,#33507e);
}

.rl-kpi-l{
    font-family:'Space Grotesk',sans-serif;
    font-size:.58rem;
    font-weight:600;
    letter-spacing:.15em;
    text-transform:uppercase;
    color:#6b7c97;
}

.rl-kpi-v{
    font-family:'JetBrains Mono',monospace;
    font-weight:700;
    font-size:1.42rem;
    color:#eef3fb;
    margin-top:9px;
    letter-spacing:-.02em;
    font-variant-numeric:tabular-nums;
}

.rl-kpi-s{
    font-family:'JetBrains Mono',monospace;
    font-size:.66rem;
    color:#6b7c97;
    margin-top:4px;
}

.rl-list{
    margin:0;
    padding-left:18px;
    line-height:1.55;
    font-size:.82rem;
}

.rl-list li{
    margin:6px 0;
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

[data-testid="stDataFrame"]{
    border:0;
}

#MainMenu,footer{
    visibility:hidden;
}
</style>
""",
    unsafe_allow_html=True,
)


# ---------------------------------------------------------------------------
# UI helpers
# ---------------------------------------------------------------------------

def _tab_error(where: str, exc: Exception) -> None:
    st.markdown(
        f'<div class="rl-glitch"><div class="t">⚠ {html.escape(where)} hit a snag</div>'
        f'<div class="s">Your data is intact — this is a rendering edge case, not a trading fault. '
        f'The page stays usable. Details are in the expander.</div></div>',
        unsafe_allow_html=True,
    )
    with st.expander("Technical details"):
        st.exception(exc)


def _download_button(label: str, data: bytes, file_name: str, mime: str, key: str = None):
    """use_container_width -> width shim, guarded so older Streamlit cannot crash."""
    kwargs = {}
    if key:
        kwargs["key"] = key

    params = inspect.signature(st.download_button).parameters
    if "use_container_width" in params:
        kwargs["use_container_width"] = True
    elif "width" in params:
        kwargs["width"] = "stretch"

    try:
        return st.download_button(label, data=data, file_name=file_name, mime=mime, **kwargs)
    except TypeError:
        kwargs.pop("width", None)
        kwargs.pop("use_container_width", None)
        return st.download_button(label, data=data, file_name=file_name, mime=mime, **kwargs)


def _table(df: pd.DataFrame, height: int = None) -> None:
    if df is None or df.empty:
        st.info("No rows yet.")
        return

    kwargs = {"hide_index": True}
    if height:
        kwargs["height"] = height

    params = inspect.signature(st.dataframe).parameters
    if "use_container_width" in params:
        kwargs["use_container_width"] = True
    elif "width" in params:
        kwargs["width"] = "stretch"

    try:
        st.dataframe(df, **kwargs)
    except TypeError:
        kwargs.pop("width", None)
        kwargs.pop("use_container_width", None)
        st.dataframe(df, hide_index=True)


def _plotly_chart(fig: go.Figure) -> None:
    params = inspect.signature(st.plotly_chart).parameters
    kwargs = {"config": {"displayModeBar": False}}

    if "use_container_width" in params:
        kwargs["use_container_width"] = True
    elif "width" in params:
        kwargs["width"] = "stretch"

    try:
        st.plotly_chart(fig, **kwargs)
    except TypeError:
        kwargs.pop("width", None)
        kwargs.pop("use_container_width", None)
        st.plotly_chart(fig, config={"displayModeBar": False})


def _dark_fig(height: int = 300) -> dict:
    return dict(
        paper_bgcolor="#0a101d",
        plot_bgcolor="#0a101d",
        font=dict(color="#8294b0", family="JetBrains Mono, monospace", size=10),
        margin=dict(l=8, r=8, t=8, b=8),
        xaxis=dict(gridcolor="#13203a", zerolinecolor="#13203a", tickcolor="#13203a"),
        yaxis=dict(gridcolor="#13203a", zerolinecolor="#13203a", tickcolor="#13203a"),
        height=height,
        showlegend=False,
    )


def _kpi(label: str, value: str, sub: str, accent: str) -> str:
    return (
        f'<div class="rl-kpi" style="--ac:{html.escape(accent)}">'
        f'<div class="rl-kpi-l">{html.escape(label)}</div>'
        f'<div class="rl-kpi-v">{html.escape(value)}</div>'
        f'<div class="rl-kpi-s">{html.escape(sub)}</div>'
        f"</div>"
    )


def _edges_df(edge_dict: dict) -> pd.DataFrame:
    rows = []
    for key, values in (edge_dict or {}).items():
        rows.append(
            {
                "key": key,
                "trades": values.get("trades", 0),
                "wins": values.get("wins", 0),
                "win_rate": values.get("win_rate", 0.0),
                "pnl": values.get("pnl", 0.0),
            }
        )
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values("pnl", ascending=False)
    return df


def _select_columns(df: pd.DataFrame, columns: list) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    present = [c for c in columns if c in df.columns]
    if present:
        return df[present]
    return df


# ---------------------------------------------------------------------------
# Cached loaders
# ---------------------------------------------------------------------------

@st.cache_data(ttl=5, show_spinner=False)
def _journal_token():
    journal = get_journal()
    paths = [
        getattr(journal, "_live", ""),
        getattr(journal, "_archive", ""),
        SNAPSHOT_FILE,
    ]
    tokens = []
    for path in paths:
        try:
            if path:
                stat = os.stat(path)
                tokens.append((path, stat.st_mtime, stat.st_size))
            else:
                tokens.append((path, 0, 0))
        except OSError:
            tokens.append((path, 0, 0))
    return tuple(tokens)


@st.cache_data(ttl=20, show_spinner=False)
def _load_postmortem(token):
    return compute_postmortem(get_journal())


@st.cache_data(ttl=20, show_spinner=False)
def _load_sweep(token):
    return sweep_gates(get_journal())


@st.cache_data(ttl=20, show_spinner=False)
def _load_bundle(token):
    return build_learning_bundle(get_journal())


# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------

st.markdown(
    """
<div class="rl-topbar">
  <div class="rl-brand">
    <h1>Research Loop</h1>
    <div class="sub">Offline learning · Backup · Gate backtest · Post-mortem</div>
  </div>
</div>
""",
    unsafe_allow_html=True,
)

st.caption(
    "Read-only. This page places no trades and never changes the live strategy. "
    "The bot learns between sessions only through a human who reads this and forward-tests a proposal on demo."
)

tab_send, tab_backup, tab_gate, tab_missed = st.tabs(
    ["SEND TO Q", "BACKUP", "GATE BACKTEST", "MISSED & AVOIDABLE"]
)


# ---------------------------------------------------------------------------
# Tab 1 — SEND TO Q
# ---------------------------------------------------------------------------

with tab_send:
    try:
        token = _journal_token()
        postmortem = _load_postmortem(token)
        summary = postmortem.get("summary", {})

        net_pnl = float(summary.get("net_pnl", 0.0) or 0.0)
        win_rate = float(summary.get("win_rate", 0.0) or 0.0)
        reviews = int(summary.get("reviews", 0) or 0)
        taken = int(summary.get("taken", 0) or 0)
        closed = int(summary.get("closed", 0) or 0)
        snapshots = int(summary.get("snapshots_recorded", 0) or 0)

        net_str = f"{net_pnl:+,.2f}"
        wr_str = f"{win_rate:.1f}%"

        net_accent = "#22c55e" if net_pnl >= 0 else "#ef4444"
        wr_accent = "#22c55e" if win_rate >= 55 else ("#ef4444" if closed and win_rate < 45 else "#3884ff")

        kpis = (
            _kpi("Reviews", f"{reviews:,}", "trigger-candle reviews", "#3884ff")
            + _kpi("Taken", f"{taken:,}", "setups executed", "#a855f7")
            + _kpi("Closed P&L", net_str, f"{closed} closed trades", net_accent)
            + _kpi("Win rate", wr_str, f"{snapshots} snapshots recorded", wr_accent)
        )
        st.markdown(f'<div class="rl-kpis">{kpis}</div>', unsafe_allow_html=True)

        bundle_bytes = _load_bundle(token)
        postmortem_bytes = json.dumps(postmortem, indent=2, default=str).encode("utf-8")

        c1, c2 = st.columns(2)
        with c1:
            _download_button(
                "Download learning bundle (zip)",
                bundle_bytes,
                "momentummaster_learning_bundle.zip",
                "application/zip",
                key="dl_bundle",
            )
        with c2:
            _download_button(
                "Download postmortem.json",
                postmortem_bytes,
                "postmortem.json",
                "application/json",
                key="dl_postmortem",
            )

        gatekeepers = postmortem.get("gatekeeper_factors", []) or []
        top_gate = gatekeepers[0][0] if gatekeepers else "none yet"
        avoidable_count = len(postmortem.get("avoidable_losses", []) or [])
        fragile_count = len(postmortem.get("fragile_wins", []) or [])

        st.markdown(
            f"""
<div class="rl-panel">
  <div class="rl-h">What I look for</div>
  <ul class="rl-list">
    <li>
      <b>Avoidable losses: {avoidable_count}</b><br/>
      These lost after price was in favour.<br/>
      <span class="mut">Lever:</span> contract duration / exit choice.
    </li>
    <li>
      <b>Fragile wins: {fragile_count}</b><br/>
      These won but spent too much time against the entry.<br/>
      <span class="mut">Lever:</span> entry timing / trigger strictness.
    </li>
    <li>
      <b>Top gatekeeper factor: {html.escape(str(top_gate))}</b><br/>
      The soft factor most often weakest in near-miss trending stand-asides.<br/>
      <span class="mut">Lever:</span> re-test this one gate, not everything at once.
    </li>
    <li>
      <b>Edges by symbol / hour / regime</b><br/>
      Where the closed sample actually made money.<br/>
      <span class="mut">Lever:</span> selection. Do more of what works, less of what does not.
    </li>
  </ul>
</div>
""",
            unsafe_allow_html=True,
        )

        st.info(
            "Honest ceiling: nothing here auto-applies. The bundle is for offline review. "
            "Any change must be proposed, forward-tested on demo, then manually opted into."
        )

    except Exception as exc:
        _tab_error("Send to Q", exc)


# ---------------------------------------------------------------------------
# Tab 2 — BACKUP
# ---------------------------------------------------------------------------

with tab_backup:
    try:
        st.markdown(
            """
<div class="rl-panel">
  <div class="rl-h">Backup & restore</div>
  <div>
    Export the master archive or the merged JSON view.<br/>
    Import is idempotent: importing the same backup twice should add nothing the second time.
  </div>
</div>
""",
            unsafe_allow_html=True,
        )

        archive_bytes = export_archive_csv_bytes(get_journal())
        merged_bytes = export_merged_json_bytes(get_journal())

        c1, c2 = st.columns(2)
        with c1:
            _download_button(
                "Download master archive CSV",
                archive_bytes,
                "momentummaster_journal_archive.csv",
                "text/csv",
                key="dl_archive",
            )
        with c2:
            _download_button(
                "Download merged JSON",
                merged_bytes,
                "momentummaster_journal_merged.json",
                "application/json",
                key="dl_merged",
            )

        st.divider()

        uploaded = st.file_uploader(
            "Import backup",
            type=["csv", "json"],
            key="rl_import_file",
        )

        if uploaded is not None:
            if st.button("Import backup now", type="primary", use_container_width=True):
                result = import_journal(get_journal(), uploaded.read(), uploaded.name)
                st.session_state.rl_import_result = result
                st.cache_data.clear()
                st.rerun()

        if "rl_import_result" in st.session_state:
            result = st.session_state.pop("rl_import_result")
            st.success("Import complete — idempotent merge finished.")
            st.json(result)

    except Exception as exc:
        _tab_error("Backup", exc)


# ---------------------------------------------------------------------------
# Tab 3 — GATE BACKTEST
# ---------------------------------------------------------------------------

with tab_gate:
    try:
        token = _journal_token()
        sweep_rows = _load_sweep(token)
        sweep_df = pd.DataFrame(sweep_rows)

        st.markdown(
            """
<div class="rl-panel">
  <div class="rl-h">Offline gate backtest</div>
  <div>
    Non-destructive replay of recorded reviews.<br/>
    <b>AS-RECORDED</b> is ground truth. The other rows are proposals only.
  </div>
</div>
""",
            unsafe_allow_html=True,
        )

        _table(sweep_df, height=420)

        st.divider()

        c1, c2 = st.columns(2)
        with c1:
            variant_name = st.selectbox(
                "Weight variant",
                options=list(WEIGHT_VARIANTS.keys()),
                key="rl_variant",
            )
        with c2:
            threshold = st.selectbox(
                "Threshold",
                options=SWEEP_THRESHOLDS,
                key="rl_threshold",
            )

        preset_text = export_preset_text(variant_name, threshold)

        st.markdown('<div class="rl-h">Exportable preset proposal</div>', unsafe_allow_html=True)
        st.code(preset_text, language="text")

        _download_button(
            "Download preset proposal",
            preset_text.encode("utf-8"),
            f"preset_{variant_name}_{threshold}.txt",
            "text/plain",
            key="dl_preset",
        )

        st.warning(
            "This preset is proposal-only. It does not modify the bot. "
            "Forward-test on demo before manually opting in."
        )

    except Exception as exc:
        _tab_error("Gate backtest", exc)


# ---------------------------------------------------------------------------
# Tab 4 — MISSED & AVOIDABLE
# ---------------------------------------------------------------------------

with tab_missed:
    try:
        token = _journal_token()
        postmortem = _load_postmortem(token)

        avoidable = postmortem.get("avoidable_losses", []) or []
        fragile = postmortem.get("fragile_wins", []) or []
        gatekeepers = postmortem.get("gatekeeper_factors", []) or []
        edges = postmortem.get("edges", {}) or {}

        avoidable_df = _select_columns(
            pd.DataFrame(avoidable),
            [
                "timestamp_utc",
                "symbol",
                "direction",
                "duration_min",
                "score",
                "threshold",
                "pnl",
                "mae",
                "mfe",
                "mfe_minus_mae",
                "lens",
                "note",
            ],
        )

        fragile_df = _select_columns(
            pd.DataFrame(fragile),
            [
                "timestamp_utc",
                "symbol",
                "direction",
                "duration_min",
                "score",
                "threshold",
                "pnl",
                "mae",
                "mfe",
                "mfe_minus_mae",
                "lens",
                "note",
            ],
        )

        c1, c2 = st.columns(2)

        with c1:
            st.markdown('<div class="rl-h">Avoidable losses · duration / exit lens</div>', unsafe_allow_html=True)
            _table(avoidable_df, height=320)

        with c2:
            st.markdown('<div class="rl-h">Fragile wins · entry-timing lens</div>', unsafe_allow_html=True)
            _table(fragile_df, height=320)

        st.divider()

        st.markdown('<div class="rl-h">Gatekeeper factors · near-miss trending stand-asides</div>', unsafe_allow_html=True)

        if gatekeepers:
            gate_df = pd.DataFrame(gatekeepers, columns=["factor", "count"])

            fig = go.Figure(
                go.Bar(
                    x=gate_df["factor"],
                    y=gate_df["count"],
                    marker_color="#3884ff",
                    text=gate_df["count"],
                    textposition="outside",
                    hovertemplate="%{x}<br>count <b>%{y}</b><extra></extra>",
                )
            )
            fig.update_layout(**_dark_fig(300))
            _plotly_chart(fig)

            _table(gate_df, height=260)
        else:
            st.info("No gatekeeper pressure yet. Near-miss trending stand-asides will appear here.")

        st.divider()

        e1, e2, e3 = st.columns(3)

        with e1:
            st.markdown('<div class="rl-h">Edge by symbol</div>', unsafe_allow_html=True)
            _table(_edges_df(edges.get("by_symbol", {})), height=300)

        with e2:
            st.markdown('<div class="rl-h">Edge by hour UTC</div>', unsafe_allow_html=True)
            _table(_edges_df(edges.get("by_hour_utc", {})), height=300)

        with e3:
            st.markdown('<div class="rl-h">Edge by regime</div>', unsafe_allow_html=True)
            _table(_edges_df(edges.get("by_regime", {})), height=300)

    except Exception as exc:
        _tab_error("Missed & avoidable", exc)
