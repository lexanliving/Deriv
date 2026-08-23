"""pages/research.py — Digit research loop for MomentumMaster Digit.

Read-only Streamlit page.
Places no trades.
Mutates no strategy.
Learns only from the journal and produces proposals that must be
forward-tested on demo before a human opts in.
"""
from __future__ import annotations

import html
import os
import sys

import pandas as pd
import streamlit as st

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from src.journal import get_journal
from src.persistence import (
    compute_condition_edges,
    compute_daily_progress,
    compute_digit_summary,
    compute_gatekeepers,
    compute_missed_avoidable,
    compute_review_conditions,
    export_archive_csv_bytes,
    export_merged_json_bytes,
    export_preset_text,
    import_journal,
    normalize_digit_rows,
    recommend_digit_settings,
    sweep_digit_gates,
)

st.set_page_config(
    page_title="Digit Research Loop",
    page_icon="🧪",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(
    """
    <style>
    html, body, .stApp { background: #060912; color: #c7d2e0; font-family: Inter, system-ui, sans-serif; }
    [data-testid="stSidebar"] { background: #0a0f1c; border-right: 1px solid #1b2740; }
    [data-testid="stMainBlockContainer"] { max-width: 1500px; padding-top: 1.1rem; }
    .rl-panel {
        background: linear-gradient(160deg, #0c1322, #0a101d);
        border: 1px solid #18233a; border-radius: 15px;
        padding: 16px 18px; margin-bottom: 14px;
    }
    .rl-h {
        font-size: .68rem; font-weight: 800; letter-spacing: .16em;
        text-transform: uppercase; color: #8294b0; margin-bottom: 10px;
    }
    .rl-kpis { display: grid; grid-template-columns: repeat(6, 1fr); gap: 12px; margin: 4px 0 16px 0; }
    @media (max-width: 1100px) { .rl-kpis { grid-template-columns: repeat(3, 1fr); } }
    .rl-kpi {
        background: linear-gradient(160deg, #0c1322, #0a101d);
        border: 1px solid #18233a; border-radius: 14px; padding: 12px 14px;
        border-left: 3px solid var(--ac, #33507e);
    }
    .rl-kpi-l { font-size: .58rem; font-weight: 700; letter-spacing: .14em; text-transform: uppercase; color: #6b7c97; }
    .rl-kpi-v { font-family: monospace; font-weight: 800; font-size: 1.3rem; color: #eef3fb; margin-top: 7px; }
    .rl-kpi-s { font-family: monospace; font-size: .64rem; color: #6b7c97; margin-top: 4px; }
    .pos { color: #4ade80; } .neg { color: #fb7185; } .mut { color: #6b7c97; }
    </style>
    """,
    unsafe_allow_html=True,
)


def _kpi(label: str, value: str, sub: str, accent: str) -> str:
    return (
        f'<div class="rl-kpi" style="--ac:{html.escape(accent)}">'
        f'<div class="rl-kpi-l">{html.escape(label)}</div>'
        f'<div class="rl-kpi-v">{html.escape(value)}</div>'
        f'<div class="rl-kpi-s">{html.escape(sub)}</div></div>'
    )


def _table(df: pd.DataFrame, height: int = 320) -> None:
    if df is None or df.empty:
        st.info("No rows yet.")
        return
    st.dataframe(df, use_container_width=True, height=height, hide_index=True)


@st.cache_data(ttl=10, show_spinner=False)
def _journal_token():
    journal = get_journal()
    tokens = []
    for path in (getattr(journal, "_live", ""), getattr(journal, "_archive", "")):
        try:
            stat = os.stat(path) if path and os.path.exists(path) else None
            tokens.append((path, stat.st_mtime if stat else 0, stat.st_size if stat else 0))
        except OSError:
            tokens.append((path, 0, 0))
    return tuple(tokens)


@st.cache_data(ttl=15, show_spinner=False)
def _load_rows(token):
    return normalize_digit_rows(get_journal())


st.markdown(
    """
    <div class="rl-panel">
        <div class="rl-h">Digit Research Loop · offline learning · backup · gate backtest · condition lab</div>
        <div class="mut">Read-only. This page places no trades and never changes the live strategy.
        It only studies what the journal recorded and proposes settings for demo forward-testing.</div>
    </div>
    """,
    unsafe_allow_html=True,
)

token = _journal_token()
rows = _load_rows(token)
summary = compute_digit_summary(rows)

net_accent = "#22c55e" if summary["net_pnl"] >= 0 else "#ef4444"
wr_accent = "#22c55e" if summary["win_rate"] >= 55 else ("#ef4444" if summary["closed"] and summary["win_rate"] < 45 else "#3884ff")

st.markdown(
    f'<div class="rl-kpis">'
    + _kpi("Reviews", f"{summary['reviews']:,}", "minute reviews", "#3884ff")
    + _kpi("Arms", f"{summary['arms']:,}", f"arm rate {summary['arm_rate']:.1f}%", "#a855f7")
    + _kpi("Trades", f"{summary['entries']:,}", f"{summary['closed']} closed", "#f59e0b")
    + _kpi("Win rate", f"{summary['win_rate']:.1f}%", f"{summary['wins']}W · {summary['losses']}L", wr_accent)
    + _kpi("Net P&L", f"{summary['net_pnl']:+,.2f}", "closed trades", net_accent)
    + _kpi("Unknown", f"{summary['unknown']}", f"{summary['cancelled']} cancelled", "#64748b")
    + "</div>",
    unsafe_allow_html=True,
)

tab_daily, lab, gate, missed, backup = st.tabs(
    ["DAILY PROGRESS", "CONDITION LAB", "GATE BACKTEST", "MISSED & AVOIDABLE", "BACKUP"]
)

# ---------------------------------------------------------------------------
with tab_daily:
    st.markdown('<div class="rl-h">Daily progress · recorded from the journal</div>', unsafe_allow_html=True)

    daily = compute_daily_progress(rows)
    if not daily:
        st.info("No journal rows yet. Run the bot and let the journal collect reviews and trades.")
    else:
        df = pd.DataFrame(daily)
        _table(df, height=360)

        st.markdown('<div class="rl-h">Cumulative closed P&L by day</div>', unsafe_allow_html=True)
        st.line_chart(df.set_index("date")[["cum_pnl"]])

        st.markdown('<div class="rl-h">Daily win rate (%)</div>', unsafe_allow_html=True)
        st.bar_chart(df.set_index("date")[["win_rate"]])

# ---------------------------------------------------------------------------
with lab:
    st.markdown(
        '<div class="rl-h">Condition lab · where does 7–9 show up, and where do trades actually win?</div>',
        unsafe_allow_html=True,
    )

    if not rows:
        st.info("No journal rows yet.")
    else:
        conditions = compute_review_conditions(rows)
        edges = compute_condition_edges(rows)

        c1, c2 = st.columns(2)
        with c1:
            st.markdown('<div class="rl-h">7–9 environment by market (reviews)</div>', unsafe_allow_html=True)
            _table(pd.DataFrame(conditions.get("by_market", [])), height=300)

            st.markdown('<div class="rl-h">7–9 environment by hour UTC (reviews)</div>', unsafe_allow_html=True)
            _table(pd.DataFrame(conditions.get("by_hour", [])), height=300)
        with c2:
            st.markdown('<div class="rl-h">Trade edge by market (closed trades)</div>', unsafe_allow_html=True)
            _table(pd.DataFrame(edges.get("by_symbol", [])), height=300)

            st.markdown('<div class="rl-h">Trade edge by hour UTC (closed trades)</div>', unsafe_allow_html=True)
            _table(pd.DataFrame(edges.get("by_hour", [])), height=300)

        c3, c4 = st.columns(2)
        with c3:
            st.markdown('<div class="rl-h">Edge by lower-confirmation N</div>', unsafe_allow_html=True)
            _table(pd.DataFrame(edges.get("by_lower_n", [])), height=260)
        with c4:
            st.markdown('<div class="rl-h">Edge by threshold</div>', unsafe_allow_html=True)
            _table(pd.DataFrame(edges.get("by_threshold", [])), height=260)

        st.caption(
            "How to read this: the left tables show where the 7–9 concentration actually appears in reviews; "
            "the right tables show where closed trades made money. A condition that shows high 7–9 but negative P&L "
            "is a warning, not an invitation. Only forward-test overlaps on demo."
        )

# ---------------------------------------------------------------------------
with gate:
    st.markdown(
        '<div class="rl-h">Gate backtest · non-destructive replay of recorded reviews</div>',
        unsafe_allow_html=True,
    )

    if not rows:
        st.info("No journal rows yet.")
    else:
        sweep = sweep_digit_gates(rows)
        st.caption("AS-RECORDED is ground truth. Other rows are proposals that replay the recorded window statistics only.")
        _table(pd.DataFrame(sweep), height=360)

        rec = recommend_digit_settings(rows)

        st.markdown('<div class="rl-h">Best-setting proposal (observation only)</div>', unsafe_allow_html=True)

        pieces = []
        if rec.get("threshold"):
            pieces.append(f"threshold {rec['threshold']['setting']} (win rate {rec['threshold']['win_rate_pct']}% over {rec['threshold']['trades']} trades)")
        if rec.get("lower_n"):
            pieces.append(f"lower N={rec['lower_n']['lower_N']} (win rate {rec['lower_n']['win_rate_pct']}% over {rec['lower_n']['trades']} trades)")
        if rec.get("symbol"):
            pieces.append(f"market {rec['symbol']['market']} (P&L {rec['symbol']['pnl']:+.2f} over {rec['symbol']['trades']} trades)")
        if rec.get("hour"):
            pieces.append(f"hour {rec['hour']['hour_utc']}:00 UTC (win rate {rec['hour']['win_rate_pct']}% over {rec['hour']['trades']} trades)")

        if pieces:
            st.success(" · ".join(pieces))
        else:
            st.info(
                f"Not enough closed trades yet (need at least {rec['min_trades']} per setting). "
                "Keep collecting journal data before trusting any proposal."
            )

        if rec.get("env_symbol") or rec.get("env_hour"):
            env = []
            if rec.get("env_symbol"):
                env.append(f"{rec['env_symbol']['market']} shows avg medium 7–9 {rec['env_symbol']['avg_medium_7_9_pct']}% over {rec['env_symbol']['reviews']} reviews")
            if rec.get("env_hour"):
                env.append(f"{rec['env_hour']['hour_utc']}:00 UTC shows avg medium 7–9 {rec['env_hour']['avg_medium_7_9_pct']}% over {rec['env_hour']['reviews']} reviews")
            st.caption("Where 7–9 shows up most: " + " · ".join(env))

        preset = export_preset_text(rec)
        st.code(preset, language="text")
        st.download_button(
            "Download preset proposal",
            preset.encode("utf-8"),
            "digit_preset_proposal.txt",
            "text/plain",
            use_container_width=True,
        )
        st.warning(
            "This preset is proposal-only. It does not modify the bot. Forward-test on demo before manually opting in."
        )

# ---------------------------------------------------------------------------
with missed:
    st.markdown('<div class="rl-h">Missed & avoidable · what blocked us, what hurt us</div>', unsafe_allow_html=True)

    if not rows:
        st.info("No journal rows yet.")
    else:
        gatekeepers = compute_gatekeepers(rows)
        ma = compute_missed_avoidable(rows)

        st.markdown('<div class="rl-h">Top gatekeeper factors (what most often blocks arming)</div>', unsafe_allow_html=True)
        if gatekeepers:
            gdf = pd.DataFrame(gatekeepers)
            st.bar_chart(gdf.set_index("factor"))
            _table(gdf, height=240)
        else:
            st.info("No stand-aside rejections recorded yet.")

        c1, c2 = st.columns(2)
        with c1:
            st.markdown('<div class="rl-h">Avoidable losses · lens: threshold / lower N</div>', unsafe_allow_html=True)
            _table(pd.DataFrame(ma.get("avoidable_losses", [])), height=300)
        with c2:
            st.markdown('<div class="rl-h">Fragile wins · won on weak condition</div>', unsafe_allow_html=True)
            _table(pd.DataFrame(ma.get("fragile_wins", [])), height=300)

        st.markdown('<div class="rl-h">Missed arms · strong fast/medium blocked by slow support only</div>', unsafe_allow_html=True)
        _table(pd.DataFrame(ma.get("missed_arms", [])), height=300)

# ---------------------------------------------------------------------------
with backup:
    st.markdown('<div class="rl-h">Backup & restore · idempotent</div>', unsafe_allow_html=True)

    journal = get_journal()

    c1, c2 = st.columns(2)
    with c1:
        st.download_button(
            "Download master archive CSV",
            export_archive_csv_bytes(journal),
            "momentummaster_digit_archive.csv",
            "text/csv",
            use_container_width=True,
        )
    with c2:
        st.download_button(
            "Download merged JSON",
            export_merged_json_bytes(journal),
            "momentummaster_digit_merged.json",
            "application/json",
            use_container_width=True,
        )

    uploaded = st.file_uploader("Import backup", type=["csv", "json"], key="rl_import_file")
    if uploaded is not None and st.button("Import backup now", type="primary", use_container_width=True):
        result = import_journal(journal, uploaded.read(), uploaded.name)
        st.session_state.rl_import_result = result
        st.cache_data.clear()
        st.rerun()

    if "rl_import_result" in st.session_state:
        result = st.session_state.pop("rl_import_result")
        st.success("Import complete — idempotent merge finished.")
        st.json(result)
