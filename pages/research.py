"""pages/research.py — Digit Research Loop.

Read-only Streamlit research page.
Places no trades.
Mutates no strategy.
Uses only the existing digit journal.
"""
from __future__ import annotations

import calendar
import html
import os
import sys
from datetime import datetime, timezone

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
    compute_monthly_progress,
    compute_review_conditions,
    compute_yearly_progress,
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
    html, body, .stApp {
        background: #060912;
        color: #c7d2e0;
        font-family: Inter, system-ui, sans-serif;
    }

    [data-testid="stSidebar"] {
        background: #0a0f1c;
        border-right: 1px solid #1b2740;
    }

    [data-testid="stMainBlockContainer"] {
        max-width: 1500px;
        padding-top: 1.1rem;
    }

    .rl-panel {
        background: linear-gradient(160deg, #0c1322, #0a101d);
        border: 1px solid #18233a;
        border-radius: 15px;
        padding: 16px 18px;
        margin-bottom: 14px;
    }

    .rl-h {
        font-size: .68rem;
        font-weight: 800;
        letter-spacing: .16em;
        text-transform: uppercase;
        color: #8294b0;
        margin-bottom: 10px;
    }

    .rl-kpis {
        display: grid;
        grid-template-columns: repeat(6, 1fr);
        gap: 12px;
        margin: 4px 0 16px 0;
    }

    @media (max-width: 1100px) {
        .rl-kpis {
            grid-template-columns: repeat(3, 1fr);
        }
    }

    .rl-kpi {
        background: linear-gradient(160deg, #0c1322, #0a101d);
        border: 1px solid #18233a;
        border-radius: 14px;
        padding: 12px 14px;
        border-left: 3px solid var(--ac, #33507e);
    }

    .rl-kpi-l {
        font-size: .58rem;
        font-weight: 700;
        letter-spacing: .14em;
        text-transform: uppercase;
        color: #6b7c97;
    }

    .rl-kpi-v {
        font-family: monospace;
        font-weight: 800;
        font-size: 1.3rem;
        color: #eef3fb;
        margin-top: 7px;
    }

    .rl-kpi-s {
        font-family: monospace;
        font-size: .64rem;
        color: #6b7c97;
        margin-top: 4px;
    }

    .pos { color: #4ade80; }
    .neg { color: #fb7185; }
    .mut { color: #6b7c97; }

    .cal {
        width: 100%;
        border-collapse: collapse;
        font-family: monospace;
        table-layout: fixed;
    }

    .cal th {
        color: #6b7c97;
        font-size: .65rem;
        padding: 6px;
        text-transform: uppercase;
        border-bottom: 1px solid #18233a;
    }

    .cal td {
        border: 1px solid #18233a;
        height: 68px;
        vertical-align: top;
        padding: 6px;
        background: #0a101d;
    }

    .cal td.empty {
        background: transparent;
        border: none;
    }

    .cal .day {
        font-weight: 700;
        color: #8294b0;
    }

    .cal .cellpnl {
        font-size: .7rem;
        margin-top: 6px;
    }

    .pos-day {
        background: rgba(34, 197, 94, 0.12);
        border-left: 3px solid #22c55e;
    }

    .neg-day {
        background: rgba(239, 68, 68, 0.12);
        border-left: 3px solid #ef4444;
    }

    .rev-day {
        background: rgba(56, 132, 255, 0.08);
        border-left: 3px solid #3884ff;
    }

    .none {
        color: #42506b;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


def _kpi(label: str, value: str, sub: str, accent: str) -> str:
    return (
        f'<div class="rl-kpi" style="--ac:{html.escape(accent)}">'
        f'<div class="rl-kpi-l">{html.escape(label)}</div>'
        f'<div class="rl-kpi-v">{html.escape(value)}</div>'
        f'<div class="rl-kpi-s">{html.escape(sub)}</div>'
        f"</div>"
    )


def _table(df: pd.DataFrame, height: int = 320) -> None:
    if df is None or df.empty:
        st.info("No rows yet.")
        return
    st.dataframe(df, use_container_width=True, height=height, hide_index=True)


@st.cache_data(ttl=5, show_spinner=False)
def _journal_token():
    journal = get_journal()
    tokens = []

    for path in (getattr(journal, "_live", ""), getattr(journal, "_archive", "")):
        try:
            if path and os.path.exists(path):
                stat = os.stat(path)
                tokens.append((path, stat.st_mtime, stat.st_size))
            else:
                tokens.append((path, 0, 0))
        except OSError:
            tokens.append((path, 0, 0))

    return tuple(tokens)


@st.cache_data(ttl=15, show_spinner=False)
def _load_rows(token):
    return normalize_digit_rows(get_journal())


def _render_month_calendar(year: int, month: int, days: dict) -> str:
    cal = calendar.Calendar(firstweekday=0)
    parts = []

    parts.append('<table class="cal"><thead><tr>')
    for day_name in ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"):
        parts.append(f"<th>{day_name}</th>")
    parts.append("</tr></thead><tbody>")

    for week in cal.monthdayscalendar(year, month):
        parts.append("<tr>")

        for day in week:
            if day == 0:
                parts.append('<td class="empty"></td>')
                continue

            date_str = f"{year:04d}-{month:02d}-{day:02d}"
            d = days.get(date_str)

            if d and int(d.get("closed", 0)) > 0:
                pnl = float(d.get("pnl", 0.0))
                css = "pos-day" if pnl >= 0 else "neg-day"
                title = (
                    f"P&L {pnl:+.2f} | trades {d.get('closed', 0)} | "
                    f"win rate {d.get('win_rate', 0)}% | reviews {d.get('reviews', 0)}"
                )
                parts.append(
                    f'<td class="{css}" title="{html.escape(title, quote=True)}">'
                    f'<div class="day">{day}</div>'
                    f'<div class="cellpnl">{pnl:+.2f}</div>'
                    f"</td>"
                )
            elif d and int(d.get("reviews", 0)) > 0:
                title = f"reviews {d.get('reviews', 0)} | arms {d.get('arms', 0)}"
                parts.append(
                    f'<td class="rev-day" title="{html.escape(title, quote=True)}">'
                    f'<div class="day">{day}</div>'
                    f'<div class="cellpnl">reviews</div>'
                    f"</td>"
                )
            else:
                parts.append(
                    f'<td class="none"><div class="day">{day}</div></td>'
                )

        parts.append("</tr>")

    parts.append("</tbody></table>")
    return "".join(parts)


st.markdown(
    """
    <div class="rl-panel">
        <div class="rl-h">Digit Research Loop · offline journal analysis · calendar · condition lab · proposals</div>
        <div class="mut">
            Read-only. This page places no trades and never changes the live strategy.
            It studies the existing journal and produces proposals only.
        </div>
    </div>
    """,
    unsafe_allow_html=True,
)

token = _journal_token()
rows = _load_rows(token)
summary = compute_digit_summary(rows)

net_accent = "#22c55e" if summary["net_pnl"] >= 0 else "#ef4444"
wr_accent = (
    "#22c55e"
    if summary["win_rate"] >= 55
    else ("#ef4444" if summary["closed"] and summary["win_rate"] < 45 else "#3884ff")
)

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

tab_overview, tab_calendar, tab_lab, tab_backtest, tab_advisor, tab_backup = st.tabs(
    ["OVERVIEW", "CALENDAR", "CONDITION LAB", "BACKTEST", "ADVISOR", "BACKUP"]
)

# ---------------------------------------------------------------------------
with tab_overview:
    st.markdown('<div class="rl-h">Daily progress</div>', unsafe_allow_html=True)

    daily = compute_daily_progress(rows)
    if not daily:
        st.info("No journal rows yet. Run the bot and let the journal collect reviews and trades.")
    else:
        daily_df = pd.DataFrame(daily)
        _table(daily_df, height=380)

        st.markdown('<div class="rl-h">Cumulative closed P&L by day</div>', unsafe_allow_html=True)
        st.line_chart(daily_df.set_index("date")[["cum_pnl"]])

        st.markdown('<div class="rl-h">Daily win rate (%)</div>', unsafe_allow_html=True)
        st.bar_chart(daily_df.set_index("date")[["win_rate"]])

# ---------------------------------------------------------------------------
with tab_calendar:
    st.markdown('<div class="rl-h">Progress calendar</div>', unsafe_allow_html=True)

    daily = compute_daily_progress(rows)
    if not daily:
        st.info("No daily progress yet.")
    else:
        days = {d["date"]: d for d in daily}
        years = sorted({int(d["date"][:4]) for d in daily if d["date"]})

        if not years:
            st.info("No valid dates in the journal yet.")
        else:
            now = datetime.now(timezone.utc)

            year = st.selectbox(
                "Year",
                options=years,
                index=len(years) - 1,
                key="research_year",
            )

            months = sorted(
                {
                    int(d["date"][5:7])
                    for d in daily
                    if d["date"].startswith(f"{year:04d}")
                }
            ) or list(range(1, 13))

            default_month = now.month if year == now.year else 1
            month_index = months.index(default_month) if default_month in months else len(months) - 1

            month = st.selectbox(
                "Month",
                options=months,
                index=month_index,
                key="research_month",
            )

            st.markdown(_render_month_calendar(int(year), int(month), days), unsafe_allow_html=True)

            st.markdown('<div class="rl-h">Monthly progress</div>', unsafe_allow_html=True)
            monthly = compute_monthly_progress(rows)
            _table(pd.DataFrame(monthly), height=320)

            st.markdown('<div class="rl-h">Yearly progress</div>', unsafe_allow_html=True)
            yearly = compute_yearly_progress(rows)
            _table(pd.DataFrame(yearly), height=260)

# ---------------------------------------------------------------------------
with tab_lab:
    st.markdown(
        '<div class="rl-h">Condition lab · where 7–9 appears and where trades actually win</div>',
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
            "Left tables show where 7–9 concentration appears in reviews. "
            "Right tables show where closed trades actually made money. "
            "High 7–9 share alone is not enough; it must convert into profit."
        )

# ---------------------------------------------------------------------------
with tab_backtest:
    st.markdown(
        '<div class="rl-h">Offline gate replay · recorded evidence only</div>',
        unsafe_allow_html=True,
    )

    if not rows:
        st.info("No journal rows yet.")
    else:
        sweep = sweep_digit_gates(rows)
        sweep_df = pd.DataFrame(sweep)

        st.caption(
            "AS-RECORDED is ground truth. Other rows replay the recorded window statistics "
            "against proposed thresholds. This page does not invent trades that were not recorded."
        )

        _table(sweep_df, height=380)

        st.markdown('<div class="rl-h">Gatekeeper factors</div>', unsafe_allow_html=True)
        gatekeepers = compute_gatekeepers(rows)

        if gatekeepers:
            gate_df = pd.DataFrame(gatekeepers)
            st.bar_chart(gate_df.set_index("factor"))
            _table(gate_df, height=260)
        else:
            st.info("No stand-aside rejections recorded yet.")

        st.markdown('<div class="rl-h">Missed & avoidable</div>', unsafe_allow_html=True)
        ma = compute_missed_avoidable(rows)

        c1, c2 = st.columns(2)

        with c1:
            st.markdown('<div class="rl-h">Avoidable losses</div>', unsafe_allow_html=True)
            _table(pd.DataFrame(ma.get("avoidable_losses", [])), height=300)

        with c2:
            st.markdown('<div class="rl-h">Fragile wins</div>', unsafe_allow_html=True)
            _table(pd.DataFrame(ma.get("fragile_wins", [])), height=300)

        st.markdown('<div class="rl-h">Missed arms blocked by slow support only</div>', unsafe_allow_html=True)
        _table(pd.DataFrame(ma.get("missed_arms", [])), height=300)

# ---------------------------------------------------------------------------
with tab_advisor:
    st.markdown(
        '<div class="rl-h">Advisor · observation-based proposals only</div>',
        unsafe_allow_html=True,
    )

    if not rows:
        st.info("No journal rows yet.")
    else:
        rec = recommend_digit_settings(rows)

        pieces = []

        if rec.get("threshold"):
            pieces.append(
                f"threshold {rec['threshold']['setting']} "
                f"(win rate {rec['threshold']['win_rate_pct']}% over {rec['threshold']['trades']} trades)"
            )

        if rec.get("lower_n"):
            pieces.append(
                f"lower {rec['lower_n']['lower_N']} "
                f"(win rate {rec['lower_n']['win_rate_pct']}% over {rec['lower_n']['trades']} trades)"
            )

        if rec.get("symbol"):
            pieces.append(
                f"market {rec['symbol']['market']} "
                f"(P&L {rec['symbol']['pnl']:+,.2f} over {rec['symbol']['trades']} trades)"
            )

        if rec.get("hour"):
            pieces.append(
                f"hour {rec['hour']['hour_utc']}:00 UTC "
                f"(win rate {rec['hour']['win_rate_pct']}% over {rec['hour']['trades']} trades)"
            )

        if pieces:
            st.success(" · ".join(pieces))
        else:
            st.info(
                f"Not enough closed trades yet. Need at least {rec['min_trades']} trades per setting "
                "before the advisor will make a proposal."
            )

        env_symbol = rec.get("env_symbol")
        env_hour = rec.get("env_hour")

        if env_symbol or env_hour:
            env = []

            if env_symbol:
                env.append(
                    f"{env_symbol['market']} shows avg medium 7–9 {env_symbol['avg_medium_7_9_pct']}% "
                    f"over {env_symbol['reviews']} reviews"
                )

            if env_hour:
                env.append(
                    f"{env_hour['hour_utc']}:00 UTC shows avg medium 7–9 {env_hour['avg_medium_7_9_pct']}% "
                    f"over {env_hour['reviews']} reviews"
                )

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
            "This preset is proposal-only. It does not modify the bot. "
            "Forward-test on demo before manually opting in."
        )

# ---------------------------------------------------------------------------
with tab_backup:
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

    uploaded = st.file_uploader("Import backup", type=["csv", "json"], key="research_backup_upload")

    if uploaded is not None and st.button(
        "Restore backup",
        type="primary",
        use_container_width=True,
        key="research_restore",
    ):
        result = import_journal(journal, uploaded.read(), uploaded.name)
        st.success(f"Restore complete: {result}")
        st.cache_data.clear()
