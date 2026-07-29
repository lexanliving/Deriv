"""
bubbles.py — MomentumMaster TF · Performance Scope (READ-ONLY, crash-proof)
A crypto-bubble-map style view of your REAL recorded trades. It reads the
append-only journal archive (logs/journal_archive.csv), so a day's data never
disappears even if the live trade_journal.csv is cleared. No filters, no
toggles, no uploader — it shows the full history since the bot started.

SIZE = stake (volume) · COLOUR = net P&L (red -> green) · HOVER = full record.
No connection to the live engine; reads only via the journal module.
Run standalone:  streamlit run bubbles.py   (or drop into a pages/ folder)
"""
import math
import os
import random

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from src.journal import get_journal, COLUMNS

GREEN, RED, NEUTRAL = (52, 211, 153), (251, 113, 133), (58, 74, 102)
PER_TRADE_CAP = 90

st.set_page_config(page_title="Performance Scope", page_icon="◎", layout="wide", initial_sidebar_state="collapsed")

st.markdown(
    """
<style>
@import url('https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;600;700&family=IBM+Plex+Sans:wght@400;500;600&family=JetBrains+Mono:wght@500;600;700&display=swap');
html,body,.stApp{background-color:#060912;color:#c7d2e0;font-family:'IBM Plex Sans',sans-serif;}
.stApp{
  background-image:
    radial-gradient(900px 460px at 8% -10%, rgba(16,185,129,0.10), transparent 60%),
    radial-gradient(820px 440px at 100% 110%, rgba(56,132,255,0.09), transparent 60%),
    radial-gradient(rgba(120,150,190,0.05) 1px, transparent 1px);
  background-size:auto,auto,22px 22px;background-attachment:fixed;}
[data-testid="stMainBlockContainer"]{max-width:1480px;padding-top:1.2rem;}
[data-testid="stSidebar"]{display:none;}
.ps-head{display:flex;align-items:flex-end;justify-content:space-between;padding:4px 2px 14px 2px;position:relative;}
.ps-head::after{content:" ";position:absolute;left:0;right:0;bottom:0;height:2px;
  background:linear-gradient(90deg,#10b981,#3884ff 45%,transparent 92%);background-size:220% 100%;
  animation:ps-scan 6s linear infinite;border-radius:2px;}
@keyframes ps-scan{0%{background-position:120% 0;}100%{background-position:-120% 0;}}
.ps-logo{font-family:'Space Grotesk',sans-serif;font-weight:700;font-size:1.3rem;letter-spacing:.18em;color:#eef3fb;text-transform:uppercase;}
.ps-logo .d{color:#10b981;}
.ps-sub{font-family:'JetBrains Mono',monospace;font-size:.7rem;color:#6b7c97;letter-spacing:.04em;margin-top:3px;}
.ps-tag{font-family:'JetBrains Mono',monospace;font-size:.7rem;color:#6b7c97;text-align:right;}
.ps-tag b{color:#34d399;}
.ps-readonly{font-family:'JetBrains Mono',monospace;font-size:.66rem;color:#6b7c97;margin:2px 2px 0 2px;}
.ps-readonly .lock{color:#fbbf24;}
.ps-kpi{display:grid;grid-template-columns:1.7fr 1fr 1fr 1fr 1fr;gap:13px;margin:16px 0 6px 0;}
@media(max-width:1000px){.ps-kpi{grid-template-columns:1fr 1fr;}}
.ps-card{position:relative;background:linear-gradient(150deg,#0c1426,#0e1830);border:1px solid #1d2c49;
  border-radius:11px;padding:14px 16px;overflow:hidden;transition:transform .16s,border-color .16s,box-shadow .16s;}
.ps-card:hover{transform:translateY(-3px);border-color:#33507e;box-shadow:0 10px 26px rgba(0,0,0,.4);}
.ps-card::before{content:" ";position:absolute;top:0;left:0;right:0;height:3px;background:var(--a,#33507e);}
.ps-card.hero{padding-top:18px;padding-bottom:18px;}
.ps-card .l{font-family:'Space Grotesk',sans-serif;font-size:.6rem;font-weight:600;letter-spacing:.16em;text-transform:uppercase;color:#6b7c97;}
.ps-card .v{font-family:'JetBrains Mono',monospace;font-weight:700;margin-top:7px;color:var(--c,#eef3fb);font-variant-numeric:tabular-nums;}
.ps-card.hero .v{font-size:clamp(2rem,3.6vw,2.9rem);letter-spacing:-.03em;}
.ps-card:not(.hero) .v{font-size:clamp(1.3rem,2.1vw,1.7rem);}
.ps-card .s{font-family:'JetBrains Mono',monospace;font-size:.66rem;color:#6b7c97;margin-top:6px;}
.ps-pulse{display:inline-block;width:8px;height:8px;border-radius:50%;background:#10b981;margin-right:7px;
  animation:ps-p 2.2s ease-in-out infinite;vertical-align:middle;}
@keyframes ps-p{0%,100%{box-shadow:0 0 4px rgba(16,185,129,.5);}50%{box-shadow:0 0 14px rgba(16,185,129,.95);}}
.ps-scope{position:relative;margin:18px 0 6px 0;border:1px solid #1d2c49;border-radius:14px;
  background:radial-gradient(120% 120% at 50% 0%, #0b1426 0%, #070b16 70%);padding:10px;overflow-x:auto;overflow-y:hidden;}
.ps-scope::before{content:" ";position:absolute;inset:0;border-radius:14px;pointer-events:none;
  box-shadow:inset 0 0 60px rgba(56,132,255,.06);animation:ps-breathe 7s ease-in-out infinite;}
@keyframes ps-breathe{0%,100%{box-shadow:inset 0 0 50px rgba(56,132,255,.05);}50%{box-shadow:inset 0 0 80px rgba(16,185,129,.08);}}
.ps-corner{position:absolute;width:14px;height:14px;border-color:#33507e;opacity:.7;}
.ps-tl{top:8px;left:8px;border-top:2px solid;border-left:2px solid;}
.ps-tr{top:8px;right:8px;border-top:2px solid;border-right:2px solid;}
.ps-bl{bottom:8px;left:8px;border-bottom:2px solid;border-left:2px solid;}
.ps-br{bottom:8px;right:8px;border-bottom:2px solid;border-right:2px solid;}
.ps-scope-title{position:absolute;top:12px;left:20px;z-index:2;font-family:'Space Grotesk',sans-serif;
  font-size:.62rem;font-weight:600;letter-spacing:.18em;text-transform:uppercase;color:#6b7c97;}
.ps-legend{display:flex;align-items:center;gap:14px;flex-wrap:wrap;font-family:'JetBrains Mono',monospace;
  font-size:.68rem;color:#6b7c97;margin:4px 2px 0 2px;}
.ps-grad{height:9px;width:170px;border-radius:5px;background:linear-gradient(90deg,#fb7185,#3a4a66 50%,#34d399);}
.ps-h{font-family:'Space Grotesk',sans-serif;font-size:.7rem;font-weight:600;letter-spacing:.16em;
  text-transform:uppercase;color:#6b7c97;margin:24px 0 10px 0;padding-bottom:7px;border-bottom:1px solid #1b2740;}
.ps-empty{color:#6b7c97;font-size:.84rem;padding:26px 0;text-align:center;}
[data-testid="stDataFrame"]{border:0;}
#MainMenu,footer{visibility:hidden;}
</style>
""",
    unsafe_allow_html=True,
)

# ---------------------------------------------------------------------------
# Data — read the crash-proof archive (full history), merged decision+outcome
# ---------------------------------------------------------------------------
def _coerce(rows) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=COLUMNS)
    df = pd.DataFrame(rows)
    for c in ("pnl", "stake", "score", "martingale_step"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)
    df["ts"] = pd.to_datetime(df.get("timestamp_utc"), errors="coerce", utc=True) if "timestamp_utc" in df.columns else pd.NaT
    for c in ("outcome", "symbol", "direction", "taken", "executed", "note"):
        if c not in df.columns:
            df[c] = ""
    return df

df = _coerce(get_journal().read_archive_merged())

settled = df[df["outcome"].isin(["WON", "LOST"])].copy()
taken_n = int((df["taken"].astype(str) == "TRUE").sum()) if len(df) else 0
rejected_n = int((df["taken"].astype(str) == "FALSE").sum()) if len(df) else 0
executed_n = int(df["outcome"].isin(["WON", "LOST"]).sum()) if len(df) else 0
cancelled_n = int((df["outcome"].astype(str) == "CANCELLED").sum()) if len(df) else 0
skipped_n = int((df["outcome"].astype(str) == "SKIPPED").sum()) if len(df) else 0

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _blend(a, b, t):
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))

def _hex(rgb):
    return "#%02x%02x%02x" % rgb

def pnl_rgb(pnl, mag):
    t = max(-1.0, min(1.0, pnl / max(mag, 1e-9)))
    return _blend(NEUTRAL, GREEN, t) if t >= 0 else _blend(NEUTRAL, RED, -t)

def _luma(rgb):
    r, g, b = [v / 255.0 for v in rgb]
    return 0.2126 * r + 0.7152 * g + 0.0722 * b

def short_sym(s):
    s = str(s or "")
    if s.startswith("frx"):
        return s[3:]
    if s.startswith("1HZ"):
        return "V" + s[3:-1]
    return s

def pack(values, gap=5.0, seed=7):
    random.seed(seed)
    order = sorted(range(len(values)), key=lambda i: values[i], reverse=True)
    placed, pos = [], [None] * len(values)
    for idx in order:
        r = values[idx]
        if not placed:
            pos[idx] = (0.0, 0.0); placed.append((0.0, 0.0, r)); continue
        ang, rad, found = 0.0, 0.0, None
        for _ in range(5000):
            x, y = rad * math.cos(ang), rad * math.sin(ang)
            if all((x - px) ** 2 + (y - py) ** 2 >= (r + pr + gap) ** 2 - 1e-9 for px, py, pr in placed):
                found = (x, y); break
            ang += 0.45; rad += 0.55
        if found is None:
            found = (rad * math.cos(ang), rad * math.sin(ang))
        pos[idx] = found; placed.append((found[0], found[1], r))
    return pos

def _radii(values, r_max=72.0, r_min=13.0):
    raw = np.sqrt(np.maximum(np.asarray(values, float), 0.0))
    if raw.max() <= 0:
        raw = np.ones_like(raw)
    scaled = raw * (r_max / raw.max())
    return np.clip(scaled, r_min, r_max)

PLOTLY_LAYOUT = dict(
    paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
    margin=dict(l=0, r=0, t=0, b=0),
    xaxis=dict(visible=False, scaleanchor="y", scaleratio=1),
    yaxis=dict(visible=False), showlegend=False,
)

def render_scope(labels, sizes_val, pnl_arr, hover_rows, hover_tpl, title, r_max=72.0):
    if len(labels) == 0:
        return
    mag = float(np.max(np.abs(pnl_arr))) if len(pnl_arr) else 1.0
    fills = [pnl_rgb(p, mag) for p in pnl_arr]
    fill_hex = [_hex(c) for c in fills]
    text_col = ["#06101f" if _luma(c) > 0.62 else "#f4f8ff" for c in fills]
    radii = _radii(sizes_val, r_max=r_max)
    pos = pack(radii.tolist())
    xs = [p[0] for p in pos]; ys = [p[1] for p in pos]
    show_text = [labels[i] if radii[i] >= 17 else "" for i in range(len(labels))]
    pad = 18.0
    xmin, xmax = min(xs) - radii.max() - pad, max(xs) + radii.max() + pad
    ymin, ymax = min(ys) - radii.max() - pad, max(ys) + radii.max() + pad
    W, H = max(int(math.ceil(xmax - xmin)), 360), max(int(math.ceil(ymax - ymin)), 300)
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=xs, y=ys, mode="markers+text", text=show_text, customdata=hover_rows, hovertemplate=hover_tpl,
        marker=dict(size=radii * 2, color=fill_hex, line=dict(width=1.5, color="rgba(238,243,251,0.25)")),
        textfont=dict(size=11, color=text_col, family="JetBrains Mono"),
        hoverlabel=dict(bgcolor="#0c1426", bordercolor="#33507e",
                        font=dict(color="#eef3fb", family="JetBrains Mono", size=12)),
    ))
    fig.update_layout(**PLOTLY_LAYOUT, width=W, height=H, xaxis_range=[xmin, xmax], yaxis_range=[ymin, ymax])
    st.markdown(f'<div class="ps-scope"><span class="ps-scope-title">{title}</span>'
                '<span class="ps-corner ps-tl"></span><span class="ps-corner ps-tr"></span>'
                '<span class="ps-corner ps-bl"></span><span class="ps-corner ps-br"></span>', unsafe_allow_html=True)
    st.plotly_chart(fig, width="content", config={"displayModeBar": False})
    st.markdown('</div>', unsafe_allow_html=True)

# ---------------------------------------------------------------------------
# Header (static, read-only) — full execution funnel
# ---------------------------------------------------------------------------
st.markdown(
    f"""<div class="ps-head">
  <div><div class="ps-logo">Performance <span class="d">◎</span> Scope</div>
  <div class="ps-sub">REAL TRADE JOURNAL · FULL HISTORY · {len(settled)} SETTLED TRADES</div></div>
  <div class="ps-tag">taken <b>{taken_n}</b> · executed {executed_n} · cancelled {cancelled_n}<br>
  skipped {skipped_n} · strategy-rejected {rejected_n}</div>
</div>
<div class="ps-readonly"><span class="lock">🔒</span> READ-ONLY · reads the append-only archive · survives a cleared CSV · nothing here is editable</div>""",
    unsafe_allow_html=True,
)

st.markdown('<div class="ps-legend"><span>SIZE = stake (volume)</span>'
            '<span>COLOUR = net P&L</span>'
            '<span style="display:flex;align-items:center;gap:7px;">loss<span class="ps-grad"></span>profit</span>'
            '<span>hover any bubble for the full record</span></div>', unsafe_allow_html=True)

# ---------------------------------------------------------------------------
# Empty state
# ---------------------------------------------------------------------------
if len(settled) == 0:
    st.markdown('<div class="ps-empty">No settled trades recorded yet. The scope lights up the moment your first '
                'trade closes — until then the journal is recording every 15m decision in the dashboard.</div>',
                unsafe_allow_html=True)
    st.stop()

# ---------------------------------------------------------------------------
# 1) Per-trade bubbles (most recent settled)
# ---------------------------------------------------------------------------
pt = settled.sort_values("ts").tail(PER_TRADE_CAP).reset_index(drop=True)
pt_labels = [r.direction for _, r in pt.iterrows()]
pt_hover = [[r["ts"].strftime("%m-%d %H:%M") if pd.notna(r["ts"]) else "-",
             short_sym(r["symbol"]), r["direction"], int(r["score"]),
             f"{r['stake']:.2f}", f"{r['pnl']:+.2f}", int(r["martingale_step"])]
            for _, r in pt.iterrows()]
pt_tpl = ("⏵ %{customdata[0]}<br>%{customdata[1]} · %{customdata[2]}"
          "<br>score %{customdata[3]} · stake %{customdata[4]}"
          "<br>P&L <b>%{customdata[5]}</b> · step %{customdata[6]}<extra></extra>")
render_scope(pt_labels, pt["stake"].values, pt["pnl"].values, pt_hover, pt_tpl,
             f"PER-TRADE · last {len(pt)} settled · sized by volume")

# ---------------------------------------------------------------------------
# 2) Daily / Monthly / By-market bubbles (aggregated)
# ---------------------------------------------------------------------------
def _agg(key_series):
    g = settled.copy(); g["key"] = key_series
    a = g.groupby("key").agg(trades=("pnl", "size"),
                             wins=("outcome", lambda s: (s == "WON").sum()),
                             pnl=("pnl", "sum"), stake=("stake", "sum")).reset_index()
    a["wr"] = a["wins"] / a["trades"] * 100.0
    return a.sort_values("pnl", ascending=False).reset_index(drop=True)

def _agg_render(a, title):
    labels = [str(k) for k in a["key"]]
    hover = [[str(r["key"]), int(r["trades"]), int(r["wins"]), f"{r['wr']:.0f}",
              f"{r['stake']:.2f}", f"{r['pnl']:+.2f}"] for _, r in a.iterrows()]
    tpl = ("%{customdata[0]}<br>trades %{customdata[1]} · wins %{customdata[2]}"
           "<br>win rate %{customdata[3]}% · volume %{customdata[4]}"
           "<br>net P&L <b>%{customdata[5]}</b><extra></extra>")
    render_scope(labels, a["stake"].values, a["pnl"].values, hover, tpl, title)

_agg_render(_agg(settled["ts"].dt.strftime("%m-%d")), "DAILY · sized by volume")
_agg_render(_agg(settled["ts"].dt.strftime("%Y-%m")), "MONTHLY · sized by volume")
_agg_render(_agg(settled["symbol"].map(short_sym)), "BY MARKET · sized by volume")

# ---------------------------------------------------------------------------
# KPI baseline strip (full history)
# ---------------------------------------------------------------------------
n = len(settled); wins = int((settled["outcome"] == "WON").sum()); losses = n - wins
net = float(settled["pnl"].sum()); wr = wins / n * 100 if n else 0.0
gw = float(settled.loc[settled["outcome"] == "WON", "pnl"].sum())
gl = float(settled.loc[settled["outcome"] == "LOST", "pnl"].sum())
avg_w = gw / wins if wins else 0.0; avg_l = abs(gl) / losses if losses else 0.0
expect = (wr / 100 * avg_w) - ((1 - wr / 100) * avg_l)
pf = gw / abs(gl) if gl != 0 else (float("inf") if gw > 0 else 0.0)
daily = settled.copy(); daily["d"] = daily["ts"].dt.date
day_pnl = daily.groupby("d")["pnl"].sum()
best_d = day_pnl.idxmax() if len(day_pnl) else None
worst_d = day_pnl.idxmin() if len(day_pnl) else None

def _c(v, good=True):
    return "#34d399" if (v > 0) == good else "#fb7185" if (v < 0) == good else "#9fb0c9"

net_c = "#34d399" if net > 0 else "#fb7185" if net < 0 else "#eef3fb"
st.markdown(
    f"""<div class="ps-kpi">
 <div class="ps-card hero" style="--a:{net_c};--c:{net_c};"><div class="l"><span class="ps-pulse"></span>NET P&L</div>
   <div class="v">{net:+.2f}</div><div class="s">{n} settled · all-time</div></div>
 <div class="ps-card" style="--a:{_c(expect)};--c:{_c(expect)};"><div class="l">EXPECTANCY</div>
   <div class="v">{expect:+.2f}</div><div class="s">edge / trade</div></div>
 <div class="ps-card" style="--a:{'#34d399' if wr>=55 else '#fb7185' if wr<45 else '#3884ff'};"><div class="l">WIN RATE</div>
   <div class="v">{wr:.1f}%</div><div class="s">{wins}W / {losses}L</div></div>
 <div class="ps-card" style="--a:#3884ff;--c:#eef3fb;"><div class="l">PROFIT FACTOR</div>
   <div class="v">{pf:.2f}</div><div class="s">avgW {avg_w:.2f} · avgL {avg_l:.2f}</div></div>
 <div class="ps-card" style="--a:#a78bfa;--c:#eef3fb;"><div class="l">BEST / WORST DAY</div>
   <div class="v" style="font-size:1.1rem;">{('+' if day_pnl.get(best_d,0)>=0 else '')+f'{day_pnl.get(best_d,0):.2f}' if best_d else '-'}<br>
   <span style="color:#fb7185;">{f'{day_pnl.get(worst_d,0):.2f}' if worst_d else '-'}</span></div>
   <div class="s">{best_d} · {worst_d}</div></div>
</div>""",
    unsafe_allow_html=True,
)

# ---------------------------------------------------------------------------
# Equity baseline: daily curve + monthly bars
# ---------------------------------------------------------------------------
st.markdown('<div class="ps-h">Equity baseline</div>', unsafe_allow_html=True)
eq = settled.sort_values("ts").copy(); eq["cum"] = eq["pnl"].cumsum(); eq["d"] = eq["ts"].dt.date
eq_day = eq.groupby("d")["cum"].last().reset_index()
eq_mon = settled.copy(); eq_mon["m"] = eq_mon["ts"].dt.strftime("%Y-%m"); eq_mon = eq_mon.groupby("m")["pnl"].sum().reset_index()

f1 = go.Figure()
f1.add_trace(go.Scatter(x=eq_day["d"], y=eq_day["cum"], mode="lines", line=dict(color="#34d399", width=2),
             fill="tozeroline", fillcolor="rgba(16,185,129,0.10)",
             hovertemplate="%{x}<br>equity <b>%{y:+.2f}</b><extra></extra>"))
f1.add_hline(y=0, line=dict(color="#33507e", width=1, dash="dot"))
f1.update_layout(**{**PLOTLY_LAYOUT,
             "xaxis": dict(visible=True, gridcolor="#16223c", tickcolor="#16223c", tickfont=dict(color="#6b7c97", family="JetBrains Mono", size=9)),
             "yaxis": dict(visible=True, gridcolor="#16223c", tickcolor="#16223c", tickfont=dict(color="#6b7c97", family="JetBrains Mono", size=9)),
             "height": 250, "margin": dict(l=8, r=8, t=8, b=8)})

f2 = go.Figure()
cols = ["#34d399" if v >= 0 else "#fb7185" for v in eq_mon["pnl"]]
f2.add_trace(go.Bar(x=eq_mon["m"], y=eq_mon["pnl"], marker_color=cols,
             hovertemplate="%{x}<br><b>%{y:+.2f}</b><extra></extra>"))
f2.add_hline(y=0, line=dict(color="#33507e", width=1, dash="dot"))
f2.update_layout(**{**PLOTLY_LAYOUT,
             "xaxis": dict(visible=True, gridcolor="#16223c", tickcolor="#16223c", tickfont=dict(color="#6b7c97", family="JetBrains Mono", size=9)),
             "yaxis": dict(visible=True, gridcolor="#16223c", tickcolor="#16223c", tickfont=dict(color="#6b7c97", family="JetBrains Mono", size=9)),
             "height": 250, "margin": dict(l=8, r=8, t=8, b=8)})

c1, c2 = st.columns(2, gap="medium")
with c1:
    st.caption("Cumulative P&L by day")
    st.plotly_chart(f1, width="stretch", config={"displayModeBar": False})
with c2:
    st.caption("P&L by month")
    st.plotly_chart(f2, width="stretch", config={"displayModeBar": False})

# ---------------------------------------------------------------------------
# Rollup tables (display-only)
# ---------------------------------------------------------------------------
st.markdown('<div class="ps-h">Rollups</div>', unsafe_allow_html=True)
d_tbl = daily.groupby("d").agg(trades=("pnl", "size"), wins=("outcome", lambda s: (s == "WON").sum()),
                               pnl=("pnl", "sum"), stake=("stake", "sum")).reset_index().sort_values("d", ascending=False)
d_tbl["wr%"] = (d_tbl["wins"] / d_tbl["trades"] * 100).round(1)
m_tbl = eq_mon.rename(columns={"m": "month"}).merge(
    settled.copy().assign(m=lambda x: x["ts"].dt.strftime("%Y-%m")).groupby("m")
    .agg(trades=("pnl", "size"), wins=("outcome", lambda s: (s == "WON").sum())).reset_index(),
    on="m", how="left").sort_values("month", ascending=False)
m_tbl["wr%"] = (m_tbl["wins"] / m_tbl["trades"] * 100).round(1)

def _pnl_style(v):
    try:
        return "color:#34d399;font-weight:700;" if float(v) > 0 else "color:#fb7185;font-weight:700;" if float(v) < 0 else ""
    except Exception:
        return ""

t1, t2 = st.columns(2, gap="medium")
with t1:
    st.caption("Daily ledger")
    st.dataframe(d_tbl.rename(columns={"d": "day", "stake": "volume"})[["day", "trades", "wins", "wr%", "pnl", "volume"]]
                 .style.map(_pnl_style, subset=["pnl"]).format({"pnl": "{:+.2f}", "volume": "{:.2f}"}),
                 width="stretch", height=260, hide_index=True)
with t2:
    st.caption("Monthly ledger")
    st.dataframe(m_tbl[["month", "trades", "wins", "wr%", "pnl"]].reset_index(drop=True)
                 .style.map(_pnl_style, subset=["pnl"]).format({"pnl": "{:+.2f}"}),
                 width="stretch", height=260, hide_index=True)
