"""
Performance Scope — a read-only trading-journal cockpit built entirely from the
decision journal (logs/journal_archive.csv, falling back to the live log).

Four views, all data pulled from the CSV, every row treated as gold:
  Overview  — KPI strip, equity curve, monthly bars, calendar heatmap, month
              detail, asset donut, recent trades, win/loss streaks.
  Calendar  — month navigator + heatmap; pick a day to see that day's reviews.
  Trades    — every trade as a card with WHY it was placed (confluence
              breakdown + entry readings) and WHY it won/lost (MAE / MFE).
  Analytics — the weekly tuning loop: rejection funnel, component edge,
              score-bucket win rate, best/worst hours & weekdays, and per-week
              report cards with a plain-English "look at X" note.

Filterable by market and period. A user-set starting balance makes the equity
curve honest. Nothing here writes anything or touches the engine.
"""
import calendar as cal_mod
import math
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

_here = os.path.dirname(os.path.abspath(__file__))
for _c in (_here, os.path.dirname(_here)):
    if os.path.isdir(os.path.join(_c, "src")) and _c not in sys.path:
        sys.path.insert(0, _c)
        break

from src.journal import get_journal

st.set_page_config(page_title="Performance Scope", page_icon="◎", layout="wide", initial_sidebar_state="expanded")

# ---------------------------------------------------------------------------
# Theme — dark trading cockpit. Display + body + mono pairing, ambient depth,
# living panels, hover lift. No centered hero, no equal-card trio, no glass.
# ---------------------------------------------------------------------------
st.markdown(
    """
<style>
@import url('https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;600;700&family=IBM+Plex+Sans:wght@400;500;600&family=JetBrains+Mono:wght@500;600;700&display=swap');
html,body,.stApp{background-color:#070b14;color:#c7d2e0;font-family:'IBM Plex Sans',-apple-system,BlinkMacSystemFont,sans-serif;}
.stApp{
  background-image:
    radial-gradient(1000px 520px at 4% -10%, rgba(16,185,129,0.10), transparent 62%),
    radial-gradient(900px 480px at 102% 112%, rgba(56,132,255,0.10), transparent 60%),
    radial-gradient(rgba(120,150,190,0.045) 1px, transparent 1px);
  background-size:auto,auto,24px 24px;background-attachment:fixed;}
[data-testid="stMainBlockContainer"]{max-width:1500px;padding-top:1.1rem;}
[data-testid="stSidebar"]{background-color:#0a0f1c;border-right:1px solid #18233a;}
[data-testid="stSidebarNav"]{padding-top:6px;}
[data-testid="stSidebarNav"] li button,[data-testid="stSidebarNav"] a{font-family:'Space Grotesk',sans-serif;letter-spacing:.04em;}

.ps-topbar{display:flex;align-items:center;justify-content:space-between;gap:18px;
  padding:6px 4px 16px 4px;position:relative;}
.ps-topbar::after{content:"";position:absolute;left:4px;right:4px;bottom:0;height:2px;
  background:linear-gradient(90deg,#10b981,#3884ff 42%,transparent 94%);background-size:220% 100%;
  animation:ps-scan 7s linear infinite;border-radius:2px;}
@keyframes ps-scan{0%{background-position:130% 0;}100%{background-position:-130% 0;}}
.ps-brand{display:flex;align-items:center;gap:13px;}
.ps-mark{width:42px;height:42px;border-radius:12px;display:grid;place-items:center;
  background:linear-gradient(150deg,#0e2036,#0b1626);border:1px solid #1d3354;
  box-shadow:inset 0 1px 0 rgba(255,255,255,.05),0 6px 18px rgba(0,0,0,.4);}
.ps-mark svg{width:22px;height:22px;}
.ps-brand h1{font-family:'Space Grotesk',sans-serif;font-weight:700;font-size:1.18rem;
  letter-spacing:.14em;text-transform:uppercase;color:#eef3fb;margin:0;line-height:1;}
.ps-brand .sub{font-family:'Space Grotesk',sans-serif;font-size:.56rem;font-weight:600;
  letter-spacing:.24em;text-transform:uppercase;color:#4f6080;margin-top:5px;}
.ps-live{display:inline-flex;align-items:center;gap:7px;margin-top:7px;
  font-family:'JetBrains Mono',monospace;font-size:.68rem;color:#8294b0;}
.ps-live .dot{width:7px;height:7px;border-radius:50%;background:#10b981;
  animation:ps-pulse 2.2s ease-in-out infinite;}
@keyframes ps-pulse{0%,100%{box-shadow:0 0 3px rgba(16,185,129,.5);}50%{box-shadow:0 0 12px rgba(16,185,129,.95);}}
.ps-controls{display:flex;align-items:flex-end;gap:10px;}
.ps-controls>div{min-width:150px;}

.ps-panel{position:relative;background:linear-gradient(160deg,#0c1322,#0a101d);
  border:1px solid #18233a;border-radius:15px;padding:16px 18px;overflow:hidden;
  box-shadow:inset 0 1px 0 rgba(255,255,255,.035);
  animation:ps-rise .5s cubic-bezier(.2,.7,.2,1) both;}
.ps-panel::before{content:"";position:absolute;top:0;left:0;right:0;height:1px;
  background:linear-gradient(90deg,transparent,rgba(120,160,220,.25),transparent);}
@keyframes ps-rise{from{opacity:0;transform:translateY(10px);}to{opacity:1;transform:none;}}
.ps-panel-h{display:flex;align-items:center;justify-content:space-between;
  font-family:'Space Grotesk',sans-serif;font-size:.62rem;font-weight:600;
  letter-spacing:.18em;text-transform:uppercase;color:#6b7c97;margin-bottom:12px;}
.ps-panel-h .big{font-family:'JetBrains Mono',monospace;font-size:1.5rem;font-weight:700;
  color:#eef3fb;letter-spacing:-.02em;text-transform:none;}
.ps-panel-h .delta{font-family:'JetBrains Mono',monospace;font-size:.78rem;font-weight:600;}

.ps-kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:13px;margin:4px 0 16px 0;}
@media(max-width:1100px){.ps-kpis{grid-template-columns:repeat(2,1fr);}}
.ps-kpi{position:relative;background:linear-gradient(160deg,#0c1322,#0a101d);border:1px solid #18233a;
  border-radius:14px;padding:14px 16px;overflow:hidden;transition:transform .16s,border-color .16s,box-shadow .16s;
  animation:ps-rise .5s cubic-bezier(.2,.7,.2,1) both;}
.ps-kpi:hover{transform:translateY(-3px);border-color:#2c4straggle;border-color:#2c466e;box-shadow:0 12px 28px rgba(0,0,0,.42);}
.ps-kpi::after{content:"";position:absolute;left:0;top:0;bottom:0;width:3px;background:var(--ac,#33507e);}
.ps-kpi .row{display:flex;align-items:flex-start;justify-content:space-between;}
.ps-kpi .l{font-family:'Space Grotesk',sans-serif;font-size:.58rem;font-weight:600;
  letter-spacing:.15em;text-transform:uppercase;color:#6b7c97;}
.ps-kpi .ico{width:30px;height:30px;border-radius:9px;display:grid;place-items:center;
  background:var(--ib,rgba(51,80,126,.18));color:var(--ac,#7fa6e0);font-size:.95rem;}
.ps-kpi .v{font-family:'JetBrains Mono',monospace;font-weight:700;font-size:1.42rem;
  color:var(--vc,#eef3fb);margin-top:9px;letter-spacing:-.02em;font-variant-numeric:tabular-nums;}
.ps-kpi .s{font-family:'JetBrains Mono',monospace;font-size:.66rem;color:#6b7c97;margin-top:4px;}

.ps-grid2{display:grid;grid-template-columns:1.45fr 1fr;gap:14px;}
.ps-grid3{display:grid;grid-template-columns:1.25fr 1fr 1fr;gap:14px;}
@media(max-width:1000px){.ps-grid2,.ps-grid3{grid-template-columns:1fr;}}

/* calendar heatmap */
.cal{display:grid;grid-template-columns:repeat(7,1fr);gap:6px;}
.cal .dow{font-family:'Space Grotesk',sans-serif;font-size:.55rem;font-weight:600;
  letter-spacing:.1em;text-transform:uppercase;color:#56657f;text-align:center;padding-bottom:2px;}
.cal .cell{position:relative;border-radius:9px;min-height:54px;padding:6px 7px;
  border:1px solid #16203400;background:#0b1220;display:flex;flex-direction:column;
  justify-content:space-between;transition:transform .12s,box-shadow .12s;}
.cal .cell.act{cursor:default;}
.cal .cell.sel{outline:2px solid #3884ff;outline-offset:-1px;box-shadow:0 0 0 3px rgba(56,132,255,.18);}
.cal .cell.empty{background:transparent;border-color:transparent;}
.cal .cell .d{font-family:'JetBrains Mono',monospace;font-size:.62rem;color:#9fb0c9;}
.cal .cell .v{font-family:'JetBrains Mono',monospace;font-size:.72rem;font-weight:700;}
.cal .cell .ar{position:absolute;top:6px;right:7px;font-size:.6rem;}

.ps-legend{display:flex;flex-direction:column;gap:9px;}
.ps-legend .li{display:flex;align-items:center;gap:9px;font-family:'JetBrains Mono',monospace;font-size:.74rem;}
.ps-legend .li .dot{width:9px;height:9px;border-radius:3px;flex:0 0 auto;}
.ps-legend .li .nm{color:#c7d2e0;flex:1;}
.ps-legend .li .vl{color:#eef3fb;font-weight:700;}
.ps-legend .li .pc{color:#6b7c97;width:46px;text-align:right;}

.chip{display:inline-block;font-family:'JetBrains Mono',monospace;font-size:.62rem;font-weight:700;
  padding:2px 8px;border-radius:6px;letter-spacing:.03em;}
.chip.win{background:rgba(34,197,94,.14);color:#4ade80;border:1px solid rgba(34,197,94,.3);}
.chip.loss{background:rgba(239,68,68,.14);color:#fb7185;border:1px solid rgba(239,68,68,.3);}
.chip.buy{background:rgba(56,132,255,.14);color:#7fb0ff;border:1px solid rgba(56,132,255,.3);}
.chip.sell{background:rgba(168,85,247,.14);color:#c8a4ff;border:1px solid rgba(168,85,247,.3);}
.chip.flat{background:rgba(120,140,170,.12);color:#9fb0c9;border:1px solid rgba(120,140,170,.25);}

.trade{background:linear-gradient(160deg,#0c1322,#0a101d);border:1px solid #18233a;border-radius:13px;
  padding:13px 15px;margin-bottom:11px;transition:border-color .15s,transform .15s;}
.trade:hover{border-color:#2c466e;transform:translateX(2px);}
.trade .h{display:flex;align-items:center;gap:9px;flex-wrap:wrap;font-family:'JetBrains Mono',monospace;font-size:.8rem;}
.trade .h .t{color:#8294b0;}
.trade .h .pnl{margin-left:auto;font-weight:700;font-size:.95rem;}
.trade .meta{display:flex;gap:14px;flex-wrap:wrap;margin-top:8px;font-family:'JetBrains Mono',monospace;
  font-size:.68rem;color:#6b7c97;}
.trade .meta b{color:#c7d2e0;font-weight:600;}

.bar-row{display:flex;align-items:center;gap:9px;margin:5px 0;font-family:'JetBrains Mono',monospace;font-size:.72rem;}
.bar-row .nm{width:78px;color:#9fb0c9;text-transform:capitalize;}
.bar-row .track{flex:1;height:8px;border-radius:5px;background:#101a2c;overflow:hidden;position:relative;}
.bar-row .fill{height:100%;border-radius:5px;}
.bar-row .vv{width:34px;text-align:right;color:#eef3fb;font-weight:700;}

.edge-tbl{width:100%;border-collapse:collapse;font-family:'JetBrains Mono',monospace;font-size:.72rem;}
.edge-tbl th{text-align:left;color:#6b7c97;font-weight:600;font-size:.58rem;letter-spacing:.1em;
  text-transform:uppercase;padding:6px 8px;border-bottom:1px solid #18233a;}
.edge-tbl td{padding:7px 8px;border-bottom:1px solid #111a2b;color:#c7d2e0;}
.edge-tbl td.num{text-align:right;font-weight:700;}
.edge-pos{color:#4ade80;} .edge-neg{color:#fb7185;} .edge-flat{color:#6b7c97;}

.streaks{display:grid;grid-template-columns:1fr 1fr;gap:12px;}
.streak{background:#0b1220;border:1px solid #18233a;border-radius:12px;padding:13px 15px;text-align:center;}
.streak .n{font-family:'JetBrains Mono',monospace;font-size:1.9rem;font-weight:700;line-height:1;}
.streak .lab{font-family:'Space Grotesk',sans-serif;font-size:.56rem;font-weight:600;letter-spacing:.14em;
  text-transform:uppercase;color:#6b7c97;margin-top:7px;}
.streak .sub{font-family:'JetBrains Mono',monospace;font-size:.62rem;color:#56657f;margin-top:4px;}

.week{background:linear-gradient(160deg,#0c1322,#0a101d);border:1px solid #18233a;border-radius:13px;
  padding:14px 16px;margin-bottom:12px;}
.week .wh{display:flex;align-items:center;gap:12px;flex-wrap:wrap;}
.week .wk{font-family:'Space Grotesk',sans-serif;font-weight:700;color:#eef3fb;font-size:.95rem;}
.week .wm{display:flex;gap:16px;flex-wrap:wrap;font-family:'JetBrains Mono',monospace;font-size:.72rem;color:#9fb0c9;}
.week .wm b{color:#eef3fb;}
.week .hint{margin-top:11px;padding:10px 12px;border-radius:10px;background:rgba(56,132,255,.07);
  border:1px solid rgba(56,132,255,.22);font-size:.78rem;color:#bcd2f5;line-height:1.5;}
.week .hint .tag{font-family:'Space Grotesk',sans-serif;font-size:.55rem;font-weight:700;letter-spacing:.16em;
  text-transform:uppercase;color:#7fb0ff;display:block;margin-bottom:5px;}

.ps-empty{padding:60px 20px;text-align:center;color:#6b7c97;}
.ps-empty .e{font-family:'Space Grotesk',sans-serif;font-size:1.1rem;color:#9fb0c9;font-weight:600;}
.ps-empty .s{font-size:.82rem;margin-top:8px;line-height:1.6;}
.pos{color:#4ade80;} .neg{color:#fb7185;} .mut{color:#6b7c97;}
[data-testid="stDataFrame"]{border:0;}
#MainMenu,footer{visibility:hidden;}
</style>
""",
    unsafe_allow_html=True,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
COMP = [("trend", 5), ("trigger", 3), ("momentum", 3), ("volatility", 2),
        ("alignment", 1), ("adx", 3), ("macd", 2), ("rsi_zone", 2),
        ("pattern", 2), ("structure", 2)]
COMP_NAMES = [c[0] for c in COMP]
COMP_MAX = {c[0]: c[1] for c in COMP}
PALETTE = ["#3884ff", "#f59e0b", "#22c55e", "#a855f7", "#06b6d4", "#ef4444", "#84cc16", "#ec4899"]
PERIODS = ["All time", "This month", "Last 30 days", "Last 90 days", "This year"]


def _g(d, k):
    v = d.get(k)
    return "" if v is None else str(v).strip()


def _f(d, k):
    v = d.get(k)
    if v is None or str(v).strip() == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _i(d, k):
    v = _f(d, k)
    return int(v) if v is not None else 0


def _b(d, k):
    return _g(d, k).upper() == "TRUE"


# ---------------------------------------------------------------------------
# Cached parse (the only expensive step); everything else is light aggregation.
# ---------------------------------------------------------------------------
def _file_token():
    j = get_journal()
    toks = []
    for p in (j._live, j._archive):
        try:
            s = os.stat(p)
            toks.append((p, s.st_mtime, s.st_size))
        except OSError:
            toks.append((p, 0, 0))
    return tuple(toks)


@st.cache_data(ttl=8, show_spinner=False)
def _load_rows(token):
    rows = get_journal().read_archive_merged()
    if not rows:
        rows = get_journal().read_rows()
    out = []
    for r in rows:
        ts = pd.to_datetime(_g(r, "timestamp_utc"), utc=True, errors="coerce")
        out.append({
            "ts": ts,
            "sym": _g(r, "symbol") or "—",
            "dir": _g(r, "direction"),
            "trend": _g(r, "trend"),
            "taken": _b(r, "taken"),
            "score": _i(r, "score"),
            "thr": _i(r, "threshold"),
            "reason": _g(r, "rejection_reason"),
            "note": _g(r, "note"),
            "outcome": _g(r, "outcome"),
            "pnl": _f(r, "pnl") or 0.0,
            "stake": _f(r, "stake") or 0.0,
            "step": _i(r, "martingale_step"),
            "cid": _g(r, "contract_id"),
            "mode": _g(r, "execution_mode"),
            "regime": _g(r, "regime"),
            "dur": _g(r, "duration_min"),
            "mae": _f(r, "mae"),
            "mfe": _f(r, "mfe"),
            "sid": _g(r, "signal_id"),
            "comps": {n: _f(r, "s_" + n) for n in COMP_NAMES},
            "entry": {"adx": _f(r, "entry_adx"), "rsi": _f(r, "entry_rsi"),
                      "macd": _f(r, "entry_macd_hist"), "atr": _f(r, "atr"),
                      "close": _f(r, "close")},
        })
    return out


def _valid(t):
    return t["ts"] is not None and not pd.isna(t["ts"])


def _period_mask(t, period, now):
    if period == "All time":
        return True
    if not _valid(t):
        return False
    ts = t["ts"]
    if period == "This month":
        return ts.year == now.year and ts.month == now.month
    if period == "This year":
        return ts.year == now.year
    if period == "Last 30 days":
        return ts >= now - pd.Timedelta(days=30)
    if period == "Last 90 days":
        return ts >= now - pd.Timedelta(days=90)
    return True


# ---------------------------------------------------------------------------
# Formatting + plotly base
# ---------------------------------------------------------------------------
def money(x, sign=True):
    if x is None:
        return "—"
    s = f"{x:+,.2f}" if sign else f"{x:,.2f}"
    return s


def pct(x):
    return "—" if x is None else f"{x:.1f}%"


def PLOT(height=None):
    d = dict(paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
             font=dict(color="#8294b0", family="JetBrains Mono", size=10),
             margin=dict(l=8, r=8, t=8, b=8),
             xaxis=dict(gridcolor="#13203a", zerolinecolor="#13203a", tickcolor="#13203a"),
             yaxis=dict(gridcolor="#13203a", zerolinecolor="#13203a", tickcolor="#13203a"))
    if height:
        d["height"] = height
    return d


def kpi(label, value, sub, icon, ac, vc, ib, delay):
    return (f'<div class="ps-kpi" style="--ac:{ac};--vc:{vc};--ib:{ib};animation-delay:{delay}ms">'
            f'<div class="row"><div class="l">{label}</div><div class="ico">{icon}</div></div>'
            f'<div class="v">{value}</div><div class="s">{sub}</div></div>')


# ---------------------------------------------------------------------------
# Calendar heatmap HTML
# ---------------------------------------------------------------------------
def _cal_html(year, month, day_map, selected, scale):
    scale = max(scale, 1e-9)
    first_wd, ndays = cal_mod.monthrange(year, month)
    cells = ['<div class="dow">Mon</div><div class="dow">Tue</div><div class="dow">Wed</div>'
             '<div class="dow">Thu</div><div class="dow">Fri</div><div class="dow">Sat</div><div class="dow">Sun</div>']
    for _ in range(first_wd):
        cells.append('<div class="cell empty"></div>')
    for day in range(1, ndays + 1):
        key = f"{year:04d}-{month:02d}-{day:02d}"
        rec = day_map.get(key)
        sel = " sel" if key == selected else ""
        if rec is None:
            cells.append(f'<div class="cell{sel}"><div class="d">{day}</div><div class="v mut">·</div></div>')
        else:
            pnl, n = rec
            a = min(1.0, abs(pnl) / scale)
            alpha = 0.12 + a * 0.42
            col = (34, 197, 94) if pnl >= 0 else (239, 68, 68)
            bg = f"rgba({col[0]},{col[1]},{col[2]},{alpha:.2f})"
            bd = f"rgba({col[0]},{col[1]},{col[2]},0.5)"
            vc = "#bbf7d0" if pnl >= 0 else "#fecaca"
            ar = "▲" if pnl >= 0 else "▼"
            cells.append(
                f'<div class="cell act{sel}" style="background:{bg};border-color:{bd}">'
                f'<div class="ar" style="color:{vc}">{ar}</div>'
                f'<div class="d">{day}</div>'
                f'<div class="v" style="color:{vc}">{pnl:+,.0f}</div></div>')
    return '<div class="cal">' + "".join(cells) + "</div>"


# ---------------------------------------------------------------------------
# Sidebar (global filters live here so the top bar mirrors the reference)
# ---------------------------------------------------------------------------
rows = _load_rows(_file_token())
now = datetime.now(timezone.utc)
symbols = sorted({t["sym"] for t in rows if t["sym"] and t["sym"] != "—"})

with st.sidebar:
    st.markdown("<div style='font-family:Space Grotesk;font-size:.74rem;color:#6b7c97;font-weight:600;"
                "letter-spacing:.16em;text-transform:uppercase;margin-bottom:12px;'>Scope filters</div>",
                unsafe_allow_html=True)
    start_bal = st.number_input("Starting balance (for equity curve)", 0.0, 10000000.0, 0.0, 100.0, format="%.2f",
                                help="Your number, not a guess — equity = this + cumulative realised P&L.")
    show_attempted = st.toggle("Include non-executed in Trades tab", value=False)
    st.caption("Read-only. Nothing here changes the bot.")

# ---------------------------------------------------------------------------
# Top bar
# ---------------------------------------------------------------------------
n_reviews = len(rows)
n_exec = sum(1 for t in rows if t["outcome"] in ("WON", "LOST"))
c1, c2, c3 = st.columns([3, 1.2, 1.2])
with c1:
    st.markdown(
        f"""<div class="ps-topbar">
          <div class="ps-brand">
            <div class="ps-mark"><svg viewBox="0 0 24 24" fill="none" stroke="#34d399" stroke-width="2"
              stroke-linecap="round" stroke-linejoin="round"><path d="M3 17l5-5 4 4 8-9"/><path d="M16 7h4v4"/></svg></div>
            <div><h1>Performance Scope</h1>
              <div class="sub">Trading Journal · MomentumMaster TF</div>
              <div class="ps-live"><span class="dot"></span>{n_reviews} reviews · {n_exec} closed trades on record</div>
            </div>
          </div>
          <div class="ps-controls"></div>
        </div>""", unsafe_allow_html=True)
with c2:
    sym_filter = st.selectbox("Market", ["All markets"] + symbols, key="scope_sym")
with c3:
    period = st.selectbox("Period", PERIODS, key="scope_period")

try:
    st.page_link("dashboard.py", label="←  Back to Terminal", use_container_width=True)
except Exception:
    st.caption("← Open the Terminal from the sidebar.")

# ---------------------------------------------------------------------------
# Empty state
# ---------------------------------------------------------------------------
if not rows:
    st.markdown('<div class="ps-panel"><div class="ps-empty"><div class="e">No journal data yet</div>'
                '<div class="s">The Scope reads your decision journal. Run the Terminal and let a few 15‑minute '
                'reviews accumulate — every taken setup and every stand‑aside becomes a row here, and the '
                'cockpit fills in automatically.</div></div></div>', unsafe_allow_html=True)
    st.stop()

# ---------------------------------------------------------------------------
# Masked sets
# ---------------------------------------------------------------------------
def masked(period, sym):
    for t in rows:
        if sym != "All markets" and t["sym"] != sym:
            continue
        if not _period_mask(t, period, now):
            continue
        yield t

mrows = list(masked(period, sym_filter))
execd = [t for t in mrows if t["outcome"] in ("WON", "LOST")]
wins = [t for t in execd if t["outcome"] == "WON"]
losses = [t for t in execd if t["outcome"] == "LOST"]
net = sum(t["pnl"] for t in execd)
gw = sum(t["pnl"] for t in wins)
gl = abs(sum(t["pnl"] for t in losses))
wr = (len(wins) / len(execd) * 100) if execd else 0.0
pf = (gw / gl) if gl > 0 else (float("inf") if gw > 0 else 0.0)
avg_w = (gw / len(wins)) if wins else 0.0
avg_l = (gl / len(losses)) if losses else 0.0
rr = (avg_w / avg_l) if avg_l > 0 else 0.0
expect = (wr / 100 * avg_w) - ((1 - wr / 100) * avg_l)

# equity + drawdown (executed with valid ts, time-ordered)
eq_trades = sorted([t for t in execd if _valid(t)], key=lambda t: t["ts"])
cum = 0.0
peak = 0.0
max_dd = 0.0
eq_x, eq_y = [], []
if eq_trades:
    eq_x.append(eq_trades[0]["ts"]); eq_y.append(start_bal)
for t in eq_trades:
    cum += t["pnl"]
    eq = start_bal + cum
    peak = max(peak, eq)
    max_dd = min(max_dd, eq - peak)
    eq_x.append(t["ts"]); eq_y.append(eq)
equity_now = (start_bal + cum) if eq_trades else start_bal

# streaks (time-ordered executed)
cur_w = cur_l = long_w = long_l = 0
for t in eq_trades:
    if t["outcome"] == "WON":
        cur_w += 1; cur_l = 0; long_w = max(long_w, cur_w)
    else:
        cur_l += 1; cur_w = 0; long_l = max(long_l, cur_l)

# ---------------------------------------------------------------------------
# Tabs
# ---------------------------------------------------------------------------
tab_ov, tab_cal, tab_tr, tab_an = st.tabs(["Overview", "Calendar", "Trades", "Analytics"])

# ===== OVERVIEW ============================================================
with tab_ov:
    kpis = (
        kpi("Net P&L", money(net), f"equity {money(equity_now, False)}", "₿",
            "#22c55e" if net >= 0 else "#ef4444", "#4ade80" if net >= 0 else "#fb7185",
            "rgba(34,197,94,.16)" if net >= 0 else "rgba(239,68,68,.16)", 0)
        + kpi("Win rate", pct(wr), f"{len(wins)}W · {len(losses)}L", "◎",
              "#22c55e" if wr >= 55 else "#ef4444" if execd and wr < 45 else "#3884ff",
              "#eef3fb", "rgba(56,132,255,.16)", 40)
        + kpi("Profit factor", f"{pf:.2f}" if pf != float('inf') else "∞",
              f"edge {money(expect)} / trade", "⚡", "#a855f7", "#eef3fb", "rgba(168,85,247,.16)", 80)
        + kpi("Max drawdown", money(max_dd), f"risk/reward {rr:.2f}", "↯",
              "#ef4444", "#fb7185", "rgba(239,68,68,.16)", 120)
    )
    st.markdown(f'<div class="ps-kpis">{kpis}</div>', unsafe_allow_html=True)

    g1, g2 = st.columns([1.45, 1], gap="small")
    with g1:
        st.markdown('<div class="ps-panel"><div class="ps-panel-h"><span>Equity curve</span>'
                    f'<span><span class="big">{money(equity_now, False)}</span> '
                    f'<span class="delta {("pos" if net >= 0 else "neg")}">{money(net)}</span></span></div>'
                    '<div id="eq"></div></div>', unsafe_allow_html=True)
        if eq_trades:
            fig = go.Figure()
            fig.add_trace(go.Scatter(x=eq_x, y=eq_y, mode="lines", fill="tozeroline",
                                     fillcolor="rgba(56,132,255,0.12)",
                                     line=dict(color="#3884ff", width=2.2),
                                     hovertemplate="%{x|%b %d}<br>equity <b>%{y:,.2f}</b><extra></extra>"))
            fig.add_trace(go.Scatter(x=[eq_x[-1]], y=[eq_y[-1]], mode="markers",
                                     marker=dict(color="#10b981", size=8)))
            fig.update_layout(**PLOT(260), showlegend=False)
            st.plotly_chart(fig, width="stretch", config={"displayModeBar": False})
        else:
            st.info("No closed trades in this window yet.")
    with g2:
        st.markdown('<div class="ps-panel"><div class="ps-panel-h"><span>Monthly performance</span></div>'
                    '<div id="mo"></div></div>', unsafe_allow_html=True)
        # build month series across present range
        months_present = sorted({t["ts"].strftime("%Y-%m") for t in execd if _valid(t)})
        month_pnl = defaultdict(float)
        for t in execd:
            if _valid(t):
                month_pnl[t["ts"].strftime("%Y-%m")] += t["pnl"]
        if months_present:
            seq = pd.period_range(months_present[0], months_present[-1], freq="M")
            mx = [p.strftime("%b") for p in seq]
            my = [month_pnl.get(str(p), 0.0) for p in seq]
            fig = go.Figure(go.Bar(x=mx, y=my, marker_color=["#22c55e" if v >= 0 else "#ef4444" for v in my],
                                   hovertemplate="%{x}<br><b>%{y:+,.2f}</b><extra></extra>"))
            fig.add_hline(y=0, line=dict(color="#1d2c49", width=1))
            fig.update_layout(**PLOT(260), showlegend=False,
                              xaxis=dict(gridcolor="#13203a", tickcolor="#13203a", tickfont=dict(size=9)))
            st.plotly_chart(fig, width="stretch", config={"displayModeBar": False})
        else:
            st.info("No monthly data in this window.")

    # calendar + month detail + asset donut
    if "scope_month" not in st.session_state:
        st.session_state.scope_month = (months_present[-1] if months_present else now.strftime("%Y-%m"))
    if "scope_day" not in st.session_state:
        st.session_state.scope_day = None
    day_map_all = defaultdict(lambda: [0.0, 0])
    for t in execd:
        if _valid(t):
            d = t["ts"].strftime("%Y-%m-%d")
            day_map_all[d][0] += t["pnl"]
            day_map_all[d][1] += 1
    day_map_tuples = {k: (v[0], v[1]) for k, v in day_map_all.items()}

    c1, c2, c3 = st.columns([1.25, 1, 1], gap="small")
    with c1:
        y, m = map(int, st.session_state.scope_month.split("-"))
        colh = st.columns([1, 3, 1])
        with colh[0]:
            if st.button("‹", key="ov_prev"):
                ny, nm = (y - 1, 12) if m == 1 else (y, m - 1)
                st.session_state.scope_month = f"{ny:04d}-{nm:02d}"
                st.session_state.scope_day = None
                st.rerun()
        with colh[1]:
            st.markdown(f'<div class="ps-panel-h" style="justify-content:center;margin:0">'
                        f'<span style="color:#eef3fb;font-size:.9rem;letter-spacing:.05em">'
                        f'{cal_mod.month_name[m]} {y}</span></div>', unsafe_allow_html=True)
        with colh[2]:
            if st.button("›", key="ov_next"):
                ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
                st.session_state.scope_month = f"{ny:04d}-{nm:02d}"
                st.session_state.scope_day = None
                st.rerun()
        month_days = {k: v for k, v in day_map_tuples.items() if k.startswith(st.session_state.scope_month)}
        scale = max((abs(v[0]) for v in month_days.values()), default=1.0)
        st.markdown('<div class="ps-panel" style="margin-top:8px">' +
                    _cal_html(y, m, month_days, st.session_state.scope_day, scale) + '</div>',
                    unsafe_allow_html=True)
    with c2:
        mpnl = sum(v[0] for v in month_days.values())
        mtrades = sum(v[1] for v in month_days.values())
        mw = sum(1 for t in wins if _valid(t) and t["ts"].strftime("%Y-%m") == st.session_state.scope_month)
        ml = sum(1 for t in losses if _valid(t) and t["ts"].strftime("%Y-%m") == st.session_state.scope_month)
        mret = (mpnl / start_bal * 100) if start_bal > 0 else 0.0
        mwin_vals = [t["pnl"] for t in wins if _valid(t) and t["ts"].strftime("%Y-%m") == st.session_state.scope_month]
        mloss_vals = [t["pnl"] for t in losses if _valid(t) and t["ts"].strftime("%Y-%m") == st.session_state.scope_month]
        mavgw = (sum(mwin_vals) / len(mwin_vals)) if mwin_vals else 0.0
        mavgl = (sum(abs(x) for x in mloss_vals) / len(mloss_vals)) if mloss_vals else 0.0
        st.markdown(
            f'<div class="ps-panel"><div class="ps-panel-h"><span>{cal_mod.month_name[m]} {y}</span>'
            f'<span class="chip win">closed</span></div>'
            f'<div class="ps-panel-h" style="margin:0 0 12px 0"><span class="big {("pos" if mpnl >= 0 else "neg")}">{money(mpnl)}</span>'
            f'<span class="delta {("pos" if mret >= 0 else "neg")}">{mret:+.2f}%</span></div>'
            f'<div class="meta" style="display:flex;flex-direction:column;gap:7px;font-family:JetBrains Mono,monospace;font-size:.74rem">'
            f'<div style="display:flex;justify-content:space-between"><span class="mut">Total trades</span><b>{mtrades}</b></div>'
            f'<div style="display:flex;justify-content:space-between"><span class="mut">Winning</span><b class="pos">{mw} ({(mw/mtrades*100 if mtrades else 0):.0f}%)</b></div>'
            f'<div style="display:flex;justify-content:space-between"><span class="mut">Losing</span><b class="neg">{ml} ({(ml/mtrades*100 if mtrades else 0):.0f}%)</b></div>'
            f'<div style="display:flex;justify-content:space-between"><span class="mut">Avg win</span><b class="pos">{money(mavgw)}</b></div>'
            f'<div style="display:flex;justify-content:space-between"><span class="mut">Avg loss</span><b class="neg">{money(-mavgl)}</b></div>'
            f'</div></div>', unsafe_allow_html=True)
    with c3:
        sym_pnl = defaultdict(float)
        for t in execd:
            sym_pnl[t["sym"]] += t["pnl"]
        sym_items = sorted(sym_pnl.items(), key=lambda kv: abs(kv[1]), reverse=True)
        total_abs = sum(abs(v) for _, v in sym_items) or 1.0
        st.markdown('<div class="ps-panel"><div class="ps-panel-h"><span>Asset performance</span></div>',
                    unsafe_allow_html=True)
        if sym_items:
            fig = go.Figure(go.Pie(values=[abs(v) for _, v in sym_items],
                                   labels=[s for s, _ in sym_items], hole=0.7,
                                   marker=dict(colors=PALETTE, line=dict(color="#0a101d", width=2)),
                                   textinfo="none", hovertemplate="%{label}<br><b>%{value:+,.2f}</b><extra></extra>"))
            fig.update_layout(**PLOT(150), showlegend=False,
                              annotations=[dict(text=f"<b>{money(net)}</b>", x=0.5, y=0.5,
                                                font=dict(size=13, color="#eef3fb", family="JetBrains Mono"),
                                                showarrow=False)])
            st.plotly_chart(fig, width="stretch", config={"displayModeBar": False})
            leg = '<div class="ps-legend">'
            for (s, v), col in zip(sym_items, PALETTE):
                leg += (f'<div class="li"><span class="dot" style="background:{col}"></span>'
                        f'<span class="nm">{s}</span><span class="vl">{money(v)}</span>'
                        f'<span class="pc">{abs(v)/total_abs*100:.0f}%</span></div>')
            st.markdown(leg + '</div></div>', unsafe_allow_html=True)
        else:
            st.markdown('<div class="mut" style="padding:20px 0;text-align:center">No trades yet.</div></div>',
                        unsafe_allow_html=True)

    # recent trades + streaks
    r1, r2 = st.columns([2.2, 1], gap="small")
    with r1:
        st.markdown('<div class="ps-panel"><div class="ps-panel-h"><span>Recent trades</span></div>',
                    unsafe_allow_html=True)
        recent = list(reversed(eq_trades))[:8]
        if recent:
            recs = []
            for t in recent:
                recs.append({"Time": t["ts"].strftime("%m-%d %H:%M"), "Asset": t["sym"],
                             "Side": t["dir"], "Score": t["score"], "Result": t["outcome"],
                             "P&L": t["pnl"]})
            df = pd.DataFrame(recs)
            st.dataframe(df.style.map(lambda v: "color:#4ade80;font-weight:700" if v == "WON" else ("color:#fb7185;font-weight:700" if v == "LOST" else ""), subset=["Result"])
                         .map(lambda v: "color:#4ade80;font-weight:700" if isinstance(v, float) and v > 0 else ("color:#fb7185;font-weight:700" if isinstance(v, float) and v < 0 else ""), subset=["P&L"])
                         .format({"P&L": "{:+,.2f}"}),
                         width="stretch", height=300, hide_index=True)
        else:
            st.markdown('<div class="mut" style="padding:20px 0;text-align:center">No closed trades in this window.</div>',
                        unsafe_allow_html=True)
        st.markdown('</div>', unsafe_allow_html=True)
    with r2:
        st.markdown('<div class="ps-panel"><div class="ps-panel-h"><span>Streaks</span></div>'
                    '<div class="streaks">'
                    f'<div class="streak"><div class="n pos">{cur_w}</div><div class="lab">Winning</div><div class="sub">best {long_w}</div></div>'
                    f'<div class="streak"><div class="n neg">{cur_l}</div><div class="lab">Losing</div><div class="sub">worst {long_l}</div></div>'
                    '</div></div>', unsafe_allow_html=True)

# ===== CALENDAR ============================================================
with tab_cal:
    if "cal_month" not in st.session_state:
        st.session_state.cal_month = st.session_state.get("scope_month", now.strftime("%Y-%m"))
    y, m = map(int, st.session_state.cal_month.split("-"))
    h1, h2, h3, h4 = st.columns([1, 1, 3, 1])
    with h1:
        if st.button("‹ month", key="cal_prev"):
            ny, nm = (y - 1, 12) if m == 1 else (y, m - 1)
            st.session_state.cal_month = f"{ny:04d}-{nm:02d}"; st.rerun()
    with h2:
        if st.button("month ›", key="cal_next"):
            ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
            st.session_state.cal_month = f"{ny:04d}-{nm:02d}"; st.rerun()
    with h3:
        st.markdown(f'<div style="text-align:center;font-family:Space Grotesk,sans-serif;font-weight:700;'
                    f'color:#eef3fb;font-size:1.05rem;letter-spacing:.05em;padding-top:6px">'
                    f'{cal_mod.month_name[m]} {y}</div>', unsafe_allow_html=True)
    month_days = {k: v for k, v in day_map_tuples.items() if k.startswith(st.session_state.cal_month)}
    scale = max((abs(v[0]) for v in month_days.values()), default=1.0)
    active_days = sorted(month_days.keys())
    with h4:
        day_opts = ["— none —"] + [f"{d}  ({month_days[d][0]:+,.2f} · {month_days[d][1]} trade{'s' if month_days[d][1] != 1 else ''})" for d in active_days]
        cur_label = next((o for o in day_opts if o.startswith((st.session_state.scope_day or "######"))), "— none —")
        pick = st.selectbox("Jump to day", day_opts, index=day_opts.index(cur_label) if cur_label in day_opts else 0, key="cal_daypick")
        st.session_state.scope_day = None if pick == "— none —" else pick.split(" ")[0]

    st.markdown('<div class="ps-panel" style="margin-top:10px">' +
                _cal_html(y, m, month_days, st.session_state.scope_day, scale) + '</div>',
                unsafe_allow_html=True)

    if st.session_state.scope_day:
        d = st.session_state.scope_day
        day_trades = [t for t in mrows if _valid(t) and t["ts"].strftime("%Y-%m-%d") == d]
        dpnl = sum(t["pnl"] for t in day_trades if t["outcome"] in ("WON", "LOST"))
        st.markdown(f'<div class="ps-panel-h" style="margin:18px 0 4px 0"><span>{d} · {len(day_trades)} reviews</span>'
                    f'<span class="delta {("pos" if dpnl >= 0 else "neg")}">{money(dpnl)}</span></div>',
                    unsafe_allow_html=True)
        if day_trades:
            for t in sorted(day_trades, key=lambda x: x["ts"]):
                _render_trade_card(t)  # defined below via exec guard
        else:
            st.caption("No activity recorded on this day within the current filters.")

# ===== TRADES ==============================================================
with tab_tr:
    pool = mrows if show_attempted else execd
    pool = sorted(pool, key=lambda t: (t["ts"] if _valid(t) else pd.Timestamp.min.tz_localize("UTC")), reverse=True)
    f1, f2, f3 = st.columns(3)
    with f1:
        dsel = st.selectbox("Direction", ["All", "BUY", "SELL"], key="tr_dir")
    with f2:
        rsel = st.selectbox("Result", ["All", "WON", "LOST", "CANCELLED", "SKIPPED"], key="tr_res")
    with f3:
        nshow = st.slider("Show last N", 5, 200, 40, 5, key="tr_n")
    shown = []
    for t in pool:
        if dsel != "All" and t["dir"] != dsel:
            continue
        if rsel != "All" and t["outcome"] != rsel:
            continue
        shown.append(t)
        if len(shown) >= nshow:
            break
    if not shown:
        st.info("No trades match these filters in the selected window.")
    for t in shown:
        _render_trade_card(t)

# ===== ANALYTICS ===========================================================
with tab_an:
    st.caption("Everything below is computed from your journal — the tuning loop. Read it weekly, change one thing on demo, then forward-test.")
    a1, a2 = st.columns(2, gap="small")
    with a1:
        st.markdown('<div class="ps-panel"><div class="ps-panel-h"><span>Rejection funnel — where setups die</span></div>',
                    unsafe_allow_html=True)
        rej = Counter(t["reason"] for t in mrows if not t["taken"] and t["reason"])
        if rej:
            top = rej.most_common(8)
            mx = top[0][1]
            bars = ""
            for reason, c in top:
                w = c / mx * 100
                bars += (f'<div class="bar-row"><div class="nm" style="width:150px;font-size:.66rem">{reason[:34]}</div>'
                         f'<div class="track"><div class="fill" style="width:{w:.0f}%;background:linear-gradient(90deg,#a855f7,#3884ff)"></div></div>'
                         f'<div class="vv">{c}</div></div>')
            st.markdown(bars + '</div>', unsafe_allow_html=True)
        else:
            st.markdown('<div class="mut" style="padding:18px 0;text-align:center">No rejections in this window.</div></div>',
                        unsafe_allow_html=True)
    with a2:
        st.markdown('<div class="ps-panel"><div class="ps-panel-h"><span>Win rate by score bucket</span></div>',
                    unsafe_allow_html=True)
        buckets = [("13–15", 13, 16), ("16–18", 16, 19), ("19–21", 19, 22), ("22–25", 22, 99)]
        rows_b = []
        for label, lo, hi in buckets:
            sel = [t for t in execd if lo <= t["score"] < hi]
            w = sum(1 for t in sel if t["outcome"] == "WON")
            wrb = (w / len(sel) * 100) if sel else None
            rows_b.append((label, len(sel), wrb))
        if any(n for _, n, _ in rows_b):
            fig = go.Figure(go.Bar(x=[l for l, _, _ in rows_b],
                                   y=[(w if w is not None else 0) for _, _, w in rows_b],
                                   marker_color=["#22c55e" if (w or 0) >= 50 else "#ef4444" for _, _, w in rows_b],
                                   text=[f"{n}" for _, n, _ in rows_b], textposition="outside",
                                   hovertemplate="score %{x}<br>win rate <b>%{y:.0f}%</b><extra></extra>"))
            fig.add_hline(y=50, line=dict(color="#33507e", dash="dot", width=1))
            fig.update_layout(**PLOT(210), showlegend=False, yaxis_range=[0, 100])
            st.plotly_chart(fig, width="stretch", config={"displayModeBar": False})
            st.markdown('</div>', unsafe_allow_html=True)
        else:
            st.markdown('<div class="mut" style="padding:18px 0;text-align:center">No trades to bucket yet.</div></div>',
                        unsafe_allow_html=True)

    st.markdown('<div class="ps-panel" style="margin-top:14px"><div class="ps-panel-h">'
                '<span>Component edge — which factor separates winners from losers</span></div>',
                unsafe_allow_html=True)
    edge_rows = []
    for name in COMP_NAMES:
        wv = [t["comps"][name] for t in wins if t["comps"][name] is not None]
        lv = [t["comps"][name] for t in losses if t["comps"][name] is not None]
        wa = (sum(wv) / len(wv)) if wv else None
        la = (sum(lv) / len(lv)) if lv else None
        edge = (wa - la) if (wa is not None and la is not None) else None
        edge_rows.append((name, wa, la, edge))
    if wins or losses:
        html_t = ('<table class="edge-tbl"><tr><th>factor</th><th>max</th><th>avg in wins</th>'
                  '<th>avg in losses</th><th class="num">edge</th></tr>')
        for name, wa, la, edge in edge_rows:
            ec = "edge-pos" if (edge or 0) > 0.15 else ("edge-neg" if (edge or 0) < -0.15 else "edge-flat")
            es = "—" if edge is None else f"{edge:+.2f}"
            html_t += (f'<tr><td>{name}</td><td class="num mut">{COMP_MAX[name]}</td>'
                       f'<td class="num">{wa:.2f if wa is not None else "—"}</td>'
                       f'<td class="num">{la:.2f if la is not None else "—"}</td>'
                       f'<td class="num {ec}">{es}</td></tr>')
        html_t += '</table>'
        st.markdown(html_t, unsafe_allow_html=True)
        st.caption("Positive edge = winners score higher on that factor (it's doing its job). "
                   "Near-zero or negative edge = the factor isn't filtering — a candidate to re-weight or drop.")
    else:
        st.markdown('<div class="mut" style="padding:14px 0;text-align:center">Need wins and losses to compute edge.</div>',
                    unsafe_allow_html=True)
    st.markdown('</div>', unsafe_allow_html=True)

    g3a, g3b = st.columns(2, gap="small")
    with g3a:
        st.markdown('<div class="ps-panel"><div class="ps-panel-h"><span>Win rate by hour of day (UTC)</span></div>',
                    unsafe_allow_html=True)
        hr = defaultdict(lambda: [0, 0])
        for t in execd:
            if _valid(t):
                h = t["ts"].hour
                hr[h][0] += 1
                if t["outcome"] == "WON":
                    hr[h][1] += 1
        if hr:
            xs = sorted(hr)
            wrs = [(hr[h][1] / hr[h][0] * 100) if hr[h][0] else 0 for h in xs]
            fig = go.Figure(go.Bar(x=[f"{h:02d}" for h in xs], y=wrs,
                                   marker_color=["#22c55e" if w >= 50 else "#ef4444" for w in wrs],
                                   hovertemplate="%{x}:00<br>win rate <b>%{y:.0f}%</b><extra></extra>"))
            fig.add_hline(y=50, line=dict(color="#33507e", dash="dot", width=1))
            fig.update_layout(**PLOT(190), showlegend=False, yaxis_range=[0, 100])
            st.plotly_chart(fig, width="stretch", config={"displayModeBar": False})
        else:
            st.markdown('<div class="mut" style="padding:14px 0;text-align:center">No data.</div>', unsafe_allow_html=True)
        st.markdown('</div>', unsafe_allow_html=True)
    with g3b:
        st.markdown('<div class="ps-panel"><div class="ps-panel-h"><span>Win rate by weekday</span></div>',
                    unsafe_allow_html=True)
        wd = defaultdict(lambda: [0, 0])
        for t in execd:
            if _valid(t):
                d = t["ts"].weekday()
                wd[d][0] += 1
                if t["outcome"] == "WON":
                    wd[d][1] += 1
        if wd:
            names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
            xs = [i for i in range(7) if i in wd]
            wrs = [(wd[i][1] / wd[i][0] * 100) if wd[i][0] else 0 for i in xs]
            fig = go.Figure(go.Bar(x=[names[i] for i in xs], y=wrs,
                                   marker_color=["#22c55e" if w >= 50 else "#ef4444" for w in wrs],
                                   hovertemplate="%{x}<br>win rate <b>%{y:.0f}%</b><extra></extra>"))
            fig.add_hline(y=50, line=dict(color="#33507e", dash="dot", width=1))
            fig.update_layout(**PLOT(190), showlegend=False, yaxis_range=[0, 100])
            st.plotly_chart(fig, width="stretch", config={"displayModeBar": False})
        else:
            st.markdown('<div class="mut" style="padding:14px 0;text-align:center">No data.</div>', unsafe_allow_html=True)
        st.markdown('</div>', unsafe_allow_html=True)

    # weekly report cards with tuning hints
    st.markdown('<div class="ps-panel-h" style="margin:20px 0 6px 0"><span>Weekly reports — your edit-the-bot brief</span></div>',
                unsafe_allow_html=True)
    weeks = defaultdict(list)
    for t in mrows:
        if _valid(t):
            iso = t["ts"].isocalendar()
            weeks[(iso[0], iso[1])].append(t)
    if weeks:
        for (yr, wk) in sorted(weeks.keys(), reverse=True):
            wt = weeks[(yr, wk)]
            wex = [t for t in wt if t["outcome"] in ("WON", "LOST")]
            ww = [t for t in wex if t["outcome"] == "WON"]
            wpnl = sum(t["pnl"] for t in wex)
            wwr = (len(ww) / len(wex) * 100) if wex else 0.0
            wrej = Counter(t["reason"] for t in wt if not t["taken"] and t["reason"])
            top_rej = wrej.most_common(1)[0][0] if wrej else None
            # worst-edge component this week
            worst_name = worst_edge = None
            if ww and [t for t in wex if t["outcome"] == "LOST"]:
                wl = [t for t in wex if t["outcome"] == "LOST"]
                for name in COMP_NAMES:
                    wv = [t["comps"][name] for t in ww if t["comps"][name] is not None]
                    lv = [t["comps"][name] for t in wl if t["comps"][name] is not None]
                    if wv and lv:
                        e = sum(wv) / len(wv) - sum(lv) / len(lv)
                        if worst_edge is None or e < worst_edge:
                            worst_edge = e; worst_name = name
            hints = []
            if wwr < 50 and worst_name is not None and worst_edge < 0:
                hints.append(f"On losing setups this week the <b>{worst_name}</b> factor scored higher than on winners "
                             f"(edge {worst_edge:+.2f}) — it isn't filtering; consider raising its bar or trimming its weight.")
            if top_rej:
                hints.append(f"Most stand-asides came from <b>{top_rej}</b> — if that's 'no trigger' the market was ranging (normal); "
                             "if it's a score shortfall, the threshold may be a touch high for these conditions.")
            if wwr >= 55 and wex:
                hints.append("Setups that fired held a real edge this week — keep the gates as-is and trust the selectivity.")
            if not wex:
                hints.append("No trades fired — the gates did their job waiting; check the funnel above to see what they waited on.")
            d0 = min(t["ts"] for t in wt if _valid(t)).strftime("%b %d")
            d1 = max(t["ts"] for t in wt if _valid(t)).strftime("%b %d")
            st.markdown(
                f'<div class="week"><div class="wh"><span class="wk">Week {wk} · {yr}</span>'
                f'<span class="wm"><span>trades <b>{len(wex)}</b></span>'
                f'<span>win rate <b class="{("pos" if wwr >= 50 else "neg")}">{wwr:.0f}%</b></span>'
                f'<span>P&L <b class="{("pos" if wpnl >= 0 else "neg")}">{money(wpnl)}</b></span>'
                f'<span class="mut">{d0}–{d1}</span></span></div>'
                f'<div class="hint"><span class="tag">Tuning note</span>{" ".join(hints)}</div></div>',
                unsafe_allow_html=True)
    else:
        st.info("Not enough dated data for weekly reports yet.")


# ---------------------------------------------------------------------------
# Trade card renderer (used by Calendar + Trades tabs)
# ---------------------------------------------------------------------------
def _render_trade_card(t):
    oc = t["outcome"]
    oc_cls = "win" if oc == "WON" else ("loss" if oc == "LOST" else "flat")
    dir_cls = "buy" if t["dir"] == "BUY" else ("sell" if t["dir"] == "SELL" else "flat")
    ts = t["ts"].strftime("%Y-%m-%d %H:%M") if _valid(t) else "—"
    pnl_cls = "pos" if t["pnl"] > 0 else ("neg" if t["pnl"] < 0 else "mut")
    mae = t["mae"]; mfe = t["mfe"]
    meta = (f'<div class="meta"><span>score <b>{t["score"]}/{25}</b></span>'
            f'<span>stake <b>{t["stake"]:.2f}</b></span>'
            f'<span>step <b>{t["step"]}</b></span>'
            f'<span>regime <b>{t["regime"] or "—"}</b></span>'
            f'<span>dur <b>{t["dur"] or "—"}m</b></span>'
            f'<span>MAE <b>{mae if mae is not None else "—"}</b></span>'
            f'<span>MFE <b>{mfe if mfe is not None else "—"}</b></span></div>')
    st.markdown(
        f'<div class="trade"><div class="h"><span class="t">{ts}</span>'
        f'<span class="chip {dir_cls}">{t["dir"] or "—"}</span>'
        f'<span style="color:#c7d2e0;font-weight:600">{t["sym"]}</span>'
        f'<span class="chip {oc_cls}">{oc or "—"}</span>'
        f'<span class="pnl {pnl_cls}">{money(t["pnl"])}</span></div>{meta}</div>',
        unsafe_allow_html=True)
    # expanders with the WHY
    has_breakdown = any((t["comps"][n] or 0) > 0 for n in COMP_NAMES)
    with st.expander("Why this was placed · entry confluence"):
        if has_breakdown:
            for n in COMP_NAMES:
                v = t["comps"][n] or 0
                mx = COMP_MAX[n]
                w = (v / mx * 100) if mx else 0
                col = "#22c55e" if v >= mx * 0.66 else ("#f59e0b" if v > 0 else "#33415c")
                st.markdown(f'<div class="bar-row"><div class="nm">{n}</div>'
                            f'<div class="track"><div class="fill" style="width:{w:.0f}%;background:{col}"></div></div>'
                            f'<div class="vv">{int(v)}/{mx}</div></div>', unsafe_allow_html=True)
            e = t["entry"]
            st.markdown(f'<div class="meta" style="margin-top:10px">'
                        f'<span>entry ADX <b>{e["adx"] if e["adx"] is not None else "—"}</b></span>'
                        f'<span>RSI <b>{e["rsi"] if e["rsi"] is not None else "—"}</b></span>'
                        f'<span>MACD hist <b>{e["macd"] if e["macd"] is not None else "—"}</b></span>'
                        f'<span>ATR <b>{e["atr"] if e["atr"] is not None else "—"}</b></span>'
                        f'<span>close <b>{e["close"] if e["close"] is not None else "—"}</b></span>'
                        f'<span>trend <b>{t["trend"] or "—"}</b></span></div>', unsafe_allow_html=True)
        elif t["taken"]:
            st.caption("Taken on score, but no per-factor breakdown was stored for this row.")
        else:
            st.markdown(f'<div class="mut">Not taken — the gate that stopped it: '
                        f'<b style="color:#c7d2e0">{t["reason"] or "—"}</b></div>', unsafe_allow_html=True)
    if t["outcome"] in ("WON", "LOST"):
        with st.expander("Why it won / lost · excursion analysis"):
            if mae is not None and mfe is not None:
                if oc == "LOST" and mfe > 0:
                    line = (f"It was in profit (MFE {mfe:.5f}) but gave it back before expiry — the move reversed. "
                            "That points at <b>duration / exit</b>: a shorter contract, or a trend that exhausted, "
                            "would have banked it.")
                elif oc == "LOST":
                    line = (f"It moved against you from the start (MAE {mae:.5f}, MFE {mfe:.5f}) — entry timing or a "
                            "trend that was already exhausted. Look at whether the exhaustion / 5m-headwind gates caught it.")
                elif oc == "WON" and mae > 0:
                    line = (f"It held through a drawdown (MAE {mae:.5f}) and still won — conviction paid. MFE {mfe:.5f}.")
                else:
                    line = f"Clean run in your favour (MFE {mfe:.5f}, MAE {mae:.5f})."
                st.markdown(f'<div style="font-size:.82rem;line-height:1.6;color:#bcd2f5">{line}</div>',
                            unsafe_allow_html=True)
            else:
                st.caption("No excursion data for this row (recorded before MAE/MFE tracking, or no fill).")
            if t["note"]:
                st.markdown(f'<div class="mut" style="margin-top:8px">note: {t["note"]}</div>', unsafe_allow_html=True)
