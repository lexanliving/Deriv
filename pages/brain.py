"""pages/brain.py — the Trading Brain cockpit (deep analysis, rich replies).

Auto-added to the Streamlit sidebar by multipage discovery — NO edit to
dashboard.py or bubbles.py. Five tabs: Pulse (deep synthesis + evidence +
one-click lessons), Chat (focused, grounded, streaming), Memory (trainable
corpus), Library (free KB), Lab (honest gate-backtest + presets).

The brain is an ADVISOR: it never places a trade and never mutates the live
strategy. With NO provider configured the page still works fully offline
(Memory + Library search + numeric post-mortem + Lab + proposed lessons).
"""
import html
import json
import os
import re
import sys

import pandas as pd
import streamlit as st

_here = os.path.dirname(os.path.abspath(__file__))
for _c in (_here, os.path.dirname(_here)):
    if os.path.isdir(os.path.join(_c, "src")) and _c not in sys.path:
        sys.path.insert(0, _c)
        break


def _mm_patch_width():
    names = ("dataframe", "table", "plotly_chart", "line_chart", "bar_chart",
             "area_chart", "button", "download_button", "link_button", "page_link")
    for _name in names:
        _orig = getattr(st, _name, None)
        if _orig is None or getattr(_orig, "_mm_width_patched", False):
            continue
        try:
            _params = _inspect_sig(_orig).parameters
        except (TypeError, ValueError):
            continue
        if "width" not in _params:
            continue

        def _make(o):
            def _w(*a, **k):
                if "use_container_width" in k and "width" not in k:
                    ucw = k.pop("use_container_width")
                    k2 = dict(k, width=("stretch" if ucw else "content"))
                    try:
                        return o(*a, **k2)
                    except TypeError:
                        return o(*a, use_container_width=ucw, **k)
                return o(*a, **k)
            _w._mm_width_patched = True
            _w.__name__ = getattr(o, "__name__", "wrapped")
            return _w
        try:
            setattr(st, _name, _make(_orig))
        except Exception:
            pass


def _inspect_sig(o):
    import inspect
    return inspect.signature(o)


_mm_patch_width()

import src.brain_llm as LLM  # noqa: E402
import src.brain_kb as KB  # noqa: E402
from src.journal import get_journal  # noqa: E402

st.set_page_config(page_title="Trading Brain", page_icon="🧠", layout="wide", initial_sidebar_state="expanded")

st.markdown("""<style>
@import url('https://fonts.googleapis.com/css2?family=Sora:wght@500;600;700;800&family=IBM+Plex+Sans:wght@400;500;600&family=JetBrains+Mono:wght@500;600;700&display=swap');
html,body,.stApp{background:#05080f;color:#c7d2e0;font-family:'IBM Plex Sans',sans-serif;}
.stApp{background-image:radial-gradient(1100px 560px at 4% -12%, rgba(45,212,191,0.12), transparent 60%),radial-gradient(980px 520px at 100% 114%, rgba(251,191,36,0.07), transparent 60%),radial-gradient(820px 460px at 52% 120%, rgba(56,132,255,0.07), transparent 62%),linear-gradient(rgba(120,150,190,0.045) 1px, transparent 1px),linear-gradient(90deg, rgba(120,150,190,0.045) 1px, transparent 1px);background-size:auto,auto,auto,26px 26px,26px 26px;background-attachment:fixed;animation:tb-drift 46s ease-in-out infinite alternate;}
@keyframes tb-drift{0%{background-position:0 0,0 0,0 0,0 0,0 0;}100%{background-position:30px -24px,-26px 22px,20px -16px,0 0,0 0;}}
[data-testid="stMainBlockContainer"]{max-width:1500px;padding-top:1.2rem;}
[data-testid="stSidebar"]{background:#0a0f1c;border-right:1px solid #1b2740;}
#MainMenu,footer{visibility:hidden;}
.tb-head{position:relative;display:flex;align-items:flex-end;justify-content:space-between;padding:6px 2px 18px 2px;overflow:hidden;}
.tb-head::after{content:"";position:absolute;left:2px;right:2px;bottom:0;height:2px;border-radius:2px;background:linear-gradient(90deg,#2dd4bf,#3884ff 46%,#fbbf24 80%,transparent 97%);background-size:240% 100%;animation:tb-scan 7s linear infinite;}
@keyframes tb-scan{0%{background-position:120% 0;}100%{background-position:-120% 0;}}
.tb-logo{font-family:'Sora',sans-serif;font-weight:800;font-size:1.62rem;letter-spacing:.13em;text-transform:uppercase;color:#eef3fb;line-height:1;}
.tb-logo .syn{color:#2dd4bf;}
.tb-eyebrow{font-family:'Sora',sans-serif;font-size:.58rem;font-weight:600;letter-spacing:.24em;text-transform:uppercase;color:#4f6080;margin-top:7px;}
.tb-badge{font-family:'JetBrains Mono',monospace;font-size:.66rem;text-align:right;}
.tb-pill{display:inline-flex;align-items:center;gap:7px;padding:4px 11px;border-radius:20px;font-weight:600;}
.tb-pill.on{background:rgba(45,212,191,.12);border:1px solid rgba(45,212,191,.34);color:#5eead4;}
.tb-pill.off{background:rgba(91,107,133,.1);border:1px solid rgba(91,107,133,.26);color:#8294b0;}
.tb-dot{width:8px;height:8px;border-radius:50%;background:#2dd4bf;animation:tb-pulse 1.8s ease-in-out infinite;}
.tb-dot.off{background:#5b6b85;animation:none;}
@keyframes tb-pulse{0%,100%{box-shadow:0 0 0 0 rgba(45,212,191,.5);}50%{box-shadow:0 0 0 6px rgba(45,212,191,0);}}
.tb-chain{display:flex;align-items:stretch;gap:0;margin:14px 0 4px 0;flex-wrap:wrap;}
.tb-nd{position:relative;flex:1 1 0;min-width:150px;background:linear-gradient(160deg,#0c1426,#0e1830);border:1px solid #1d2c49;border-radius:12px;padding:12px 14px;transition:transform .16s ease,border-color .16s ease,box-shadow .16s ease;}
.tb-nd:hover{transform:translateY(-3px);border-color:#33507e;box-shadow:0 10px 24px rgba(0,0,0,.4);}
.tb-nd.on{border-color:rgba(45,212,191,.55);box-shadow:0 0 0 1px rgba(45,212,191,.25),0 10px 26px rgba(45,212,191,.10);}
.tb-nd.dim{opacity:.62;} .tb-nd.off{opacity:.4;filter:grayscale(.5);}
.tb-nd .row1{display:flex;align-items:center;gap:8px;}
.tb-nd .nm{font-family:'Sora',sans-serif;font-weight:700;font-size:.92rem;letter-spacing:.03em;color:#eef3fb;}
.tb-nd .led{width:9px;height:9px;border-radius:50%;background:#33415c;flex:0 0 auto;}
.tb-nd.on .led{background:#2dd4bf;animation:tb-pulse 1.8s ease-in-out infinite;}
.tb-nd.dim .led{background:#3884ff;}
.tb-nd .mod{font-family:'JetBrains Mono',monospace;font-size:.6rem;color:#6b7c97;margin-top:7px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
.tb-nd .st{font-family:'JetBrains Mono',monospace;font-size:.56rem;letter-spacing:.08em;text-transform:uppercase;margin-top:5px;}
.tb-nd.on .st{color:#5eead4;} .tb-nd.dim .st{color:#7fb0ff;} .tb-nd.off .st{color:#56657f;}
.tb-conn{flex:0 0 26px;display:flex;align-items:center;justify-content:center;}
.tb-conn .line{width:100%;height:2px;background:#1d2c49;position:relative;overflow:hidden;border-radius:2px;}
.tb-conn .flow{position:absolute;top:0;left:-40%;width:40%;height:100%;background:linear-gradient(90deg,transparent,#2dd4bf,transparent);animation:tb-flow 2.4s linear infinite;}
@keyframes tb-flow{0%{left:-40%;}100%{left:100%;}}
@media(max-width:760px){.tb-conn{display:none;}.tb-nd{flex:1 1 100%;}}
.tb-kpis{display:grid;grid-template-columns:repeat(5,1fr);gap:13px;margin:16px 0 18px 0;}
@media(max-width:1100px){.tb-kpis{grid-template-columns:repeat(2,1fr);}}
.tb-kpi{position:relative;background:linear-gradient(150deg,#0c1426,#0e1830);border:1px solid #1d2c49;border-radius:12px;padding:15px 17px;overflow:hidden;transition:transform .16s ease,border-color .16s ease,box-shadow .16s ease;animation:tb-rise .55s cubic-bezier(.2,.7,.2,1) both;}
.tb-kpi:hover{transform:translateY(-3px);border-color:#2c466e;box-shadow:0 12px 28px rgba(0,0,0,.42);}
.tb-kpi::before{content:"";position:absolute;top:0;left:0;right:0;height:3px;background:var(--ac,#33507e);}
.tb-kpi .l{font-family:'Sora',sans-serif;font-size:.58rem;font-weight:600;letter-spacing:.15em;text-transform:uppercase;color:#6b7c97;}
.tb-kpi .v{font-family:'JetBrains Mono',monospace;font-weight:700;font-size:1.5rem;margin-top:8px;color:var(--vc,#eef3fb);font-variant-numeric:tabular-nums;}
.tb-kpi .s{font-family:'JetBrains Mono',monospace;font-size:.64rem;color:#6b7c97;margin-top:6px;}
@keyframes tb-rise{from{opacity:0;transform:translateY(12px);}to{opacity:1;transform:none;}}
.tb-panel{position:relative;background:linear-gradient(160deg,#0c1426,#0b1222);border:1px solid #1d2c49;border-radius:14px;padding:16px 18px;margin:12px 0;overflow:hidden;transition:border-color .18s ease,box-shadow .18s ease;}
.tb-panel::before{content:"";position:absolute;top:0;left:0;right:0;height:1px;background:linear-gradient(90deg,transparent,rgba(120,160,220,.22),transparent);}
.tb-panel:hover{border-color:#2c466e;box-shadow:0 12px 30px rgba(0,0,0,.34);}
.tb-h{font-family:'Sora',sans-serif;font-size:.64rem;font-weight:700;letter-spacing:.16em;text-transform:uppercase;color:#8294b0;margin-bottom:11px;display:flex;align-items:center;gap:10px;}
.tb-h .tag{margin-left:auto;font-family:'JetBrains Mono',monospace;font-size:.58rem;font-weight:600;letter-spacing:.08em;color:#5eead4;background:rgba(45,212,191,.1);border:1px solid rgba(45,212,191,.3);padding:2px 9px;border-radius:20px;}
.tb-bubble{background:#0e1830;border:1px solid #233452;border-radius:13px;padding:14px 16px;margin:11px 0;font-size:.88rem;line-height:1.64;}
.tb-bubble.user{background:rgba(56,132,255,.08);border-color:rgba(56,132,255,.3);}
.tb-bubble .who{font-family:'Sora',sans-serif;font-size:.58rem;font-weight:700;letter-spacing:.14em;text-transform:uppercase;color:#6b7c97;margin-bottom:8px;}
.tb-body{color:#dbe6f5;}
.tb-body strong{color:#eef3fb;}
.tb-body code{background:#0a1120;border:1px solid #233452;border-radius:4px;padding:0 4px;font-family:'JetBrains Mono',monospace;font-size:.82em;color:#7fd4ff;}
.tb-body ul{margin:6px 0;padding-left:18px;} .tb-body li{margin:3px 0;}
.tb-h2{font-family:'Sora',sans-serif;font-weight:700;font-size:1.02rem;letter-spacing:.02em;color:#5eead4;margin:16px 0 6px 0;padding-bottom:4px;border-bottom:1px solid #1d2c49;}
.tb-h3{font-family:'Sora',sans-serif;font-weight:600;font-size:.9rem;letter-spacing:.02em;color:#7fd4ff;margin:12px 0 5px 0;}
.tb-typing{font-family:'JetBrains Mono',monospace;font-size:.82rem;white-space:pre-wrap;color:#dbe6f5;}
.tb-caret{display:inline-block;color:#2dd4bf;animation:tb-blink 1s steps(1) infinite;}
@keyframes tb-blink{50%{opacity:0;}}
.tb-prop{background:linear-gradient(160deg,#102018,#0c1426);border:1px solid rgba(45,212,191,.4);border-radius:12px;padding:13px 15px;margin:12px 0;}
.tb-prop-h{font-family:'Sora',sans-serif;font-size:.6rem;font-weight:700;letter-spacing:.16em;text-transform:uppercase;color:#5eead4;margin-bottom:8px;}
.tb-prop pre{margin:0;background:#0a1120;border:1px solid #233452;border-radius:8px;padding:10px 12px;font-family:'JetBrains Mono',monospace;font-size:.72rem;color:#9fe9d6;white-space:pre-wrap;overflow:auto;}
.tb-lesson{background:#0e1830;border:1px solid #233452;border-left:3px solid #2dd4bf;border-radius:10px;padding:10px 12px;margin:8px 0;font-size:.82rem;line-height:1.5;transition:transform .14s ease,border-color .14s ease;}
.tb-lesson:hover{transform:translateX(3px);border-color:#33507e;}
.tb-lesson.brain{border-left-color:#fbbf24;}
.tb-lesson .meta{font-family:'JetBrains Mono',monospace;font-size:.62rem;color:#6b7c97;margin-bottom:4px;}
.tb-tag{display:inline-block;font-family:'JetBrains Mono',monospace;font-size:.58rem;font-weight:700;padding:1px 7px;border-radius:6px;background:rgba(45,212,191,.12);color:#5eead4;border:1px solid rgba(45,212,191,.3);margin-right:5px;}
.tb-trace{font-family:'JetBrains Mono',monospace;font-size:.66rem;color:#9fb0c9;margin-top:10px;line-height:1.7;}
.tb-trace .ok{color:#4ade80;} .tb-trace .no{color:#fb7185;}
.tb-warn{background:rgba(251,191,36,.08);border:1px solid rgba(251,191,36,.3);border-radius:11px;padding:12px 14px;font-size:.8rem;color:#fde68a;margin:10px 0;line-height:1.55;}
.tb-note{font-size:.8rem;line-height:1.6;color:#9fb0c9;}
.pos{color:#4ade80;} .neg{color:#fb7185;} .mut{color:#6b7c97;}
[data-testid="stDataFrame"]{border:0;}
[data-testid="stButton"] button{border-radius:9px;font-family:'Sora',sans-serif;font-weight:600;letter-spacing:.04em;transition:filter .15s ease,transform .12s ease;}
[data-testid="stButton"] button:hover{filter:brightness(1.14);}
[data-testid="stButton"] button:active{transform:scale(.985);}
</style>""", unsafe_allow_html=True)


def _esc(t) -> str:
    return html.escape("" if t is None else str(t))


def _md(text: str) -> str:
    esc = _esc(text)
    esc = re.sub(r"`([^`]+)`", r"<code>\1</code>", esc)
    esc = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", esc)
    out, in_list = [], False
    for line in esc.split("\n"):
        s = line.strip()
        mh = re.match(r"^(#{1,3})\s+(.*)$", s)
        if mh:
            if in_list:
                out.append("</ul>")
                in_list = False
            lvl = len(mh.group(1))
            cls = "tb-h2" if lvl <= 2 else "tb-h3"
            out.append('<div class="' + cls + '">' + mh.group(2) + "</div>")
            continue
        ml = re.match(r"^[-*]\s+(.*)$", s)
        if ml:
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append("<li>" + ml.group(1) + "</li>")
            continue
        if in_list:
            out.append("</ul>")
            in_list = False
        out.append((line + "<br>") if s else "<br>")
    if in_list:
        out.append("</ul>")
    return "".join(out)


def _prop_card(raw_json: str) -> str:
    try:
        obj = json.loads(raw_json)
        w = obj.get("weights", {})
        nz = ", ".join(f"{k}×{v}" for k, v in w.items() if str(v) not in ("1", "1.0")) or "all ×1"
        thr = obj.get("threshold", "?")
        rat = _esc(obj.get("rationale", ""))
        return ('<div class="tb-prop"><div class="tb-prop-h"> Preset proposal (loaded into the Lab tab)</div>'
                '<pre>threshold = ' + _esc(thr) + '\nweights  = ' + _esc(nz) +
                ('</pre><div class="tb-note">' + rat + '</div>' if rat else '</pre>') + '</div>')
    except Exception:
        return '<div class="tb-prop"><pre>' + _esc(raw_json) + "</pre></div>"


def render_assistant(text: str) -> str:
    placeholders: List[str] = []

    def _repl(m):
        placeholders.append(m.group(1))
        return "@@P" + str(len(placeholders) - 1) + "@@"

    cleaned = re.sub(r"```json\s*(\{.*?\})\s*```", _repl, text or "", flags=re.DOTALL)
    out = _md(cleaned)
    for i, p in enumerate(placeholders):
        out = out.replace("@@P" + str(i) + "@@", _prop_card(p))
    return out


def _kpi(label, value, sub, ac, vc, delay=0):
    return ('<div class="tb-kpi" style="--ac:' + _esc(ac) + ';--vc:' + _esc(vc) + ';animation-delay:' + str(delay) + 'ms;">'
            '<div class="l">' + _esc(label) + '</div><div class="v">' + _esc(value) + '</div>'
            '<div class="s">' + _esc(sub) + '</div></div>')


def _chain_html(nodes):
    parts = []
    for i, n in enumerate(nodes):
        if n["active"]:
            cls, stt = "tb-nd on", "active"
        elif n["configured"]:
            cls, stt = "tb-nd dim", "standby"
        else:
            cls, stt = "tb-nd off", "locked"
        parts.append('<div class="' + cls + '"><div class="row1"><span class="led"></span>'
                     '<span class="nm">' + _esc(n["label"]) + '</span></div>'
                     '<div class="mod">' + _esc(n["model"]) + '</div>'
                     '<div class="st">' + stt + '</div></div>')
        if i < len(nodes) - 1:
            parts.append('<div class="tb-conn"><span class="line"><span class="flow"></span></span></div>')
    return '<div class="tb-chain">' + "".join(parts) + '</div>'


def _trace_html(trace):
    if not trace:
        return ""
    bits = []
    for t in trace:
        cls = "ok" if t.get("status") == "ok" else "no"
        bits.append('<span class="' + cls + '">' + _esc(t.get("provider")) + ": " + _esc(t.get("status")) + "</span>"
                    + (('<span class="mut"> · ' + _esc(t.get("detail")) + "</span>") if t.get("detail") else ""))
    return '<div class="tb-trace">chain: ' + " &nbsp;→&nbsp; ".join(bits) + '</div>'


def _arrow_safe(rows):
    if not rows:
        return rows
    cols = list(rows[0].keys())
    out = [dict(r) for r in rows]
    for c in cols:
        vals = [r.get(c) for r in out]
        if any(isinstance(v, (dict, list)) for v in vals):
            for r in out:
                v = r.get(c)
                r[c] = json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else ("" if v is None else str(v))
            continue
        fams = set()
        for v in vals:
            if v is None:
                continue
            if isinstance(v, bool):
                fams.add("bool")
            elif isinstance(v, (int, float)):
                fams.add("num")
            elif isinstance(v, str):
                fams.add("str")
            else:
                fams.add("other")
        if len(fams) > 1:
            for r in out:
                v = r.get(c)
                r[c] = "" if v is None else str(v)
    return out


def _glitch(where, exc):
    st.markdown('<div class="tb-warn">⚠ ' + _esc(where) + ' hit a snag — your data is intact; this is a render edge case.</div>', unsafe_allow_html=True)
    with st.expander("Technical details"):
        st.exception(exc)


# ---- load journal + post-mortem (always; no network) ----------------------- #
try:
    rows = get_journal().read_archive_merged()
except Exception as _e:
    rows = []
    _glitch("journal read", _e)
try:
    pm = KB.compute_postmortem(rows)
except Exception as _e:
    pm = KB.compute_postmortem([])
    _glitch("post-mortem", _e)

nodes = LLM.provider_nodes()
nodes_configured = any(n["configured"] for n in nodes)
active = LLM.active_provider_id()
s = pm

for _k, _d in (("br_chat", []), ("br_proposal", None)):
    if _k not in st.session_state:
        st.session_state[_k] = _d

# ---- header ---------------------------------------------------------------- #
if nodes_configured:
    badge = '<span class="tb-pill on"><span class="tb-dot"></span>chain live · ' + _esc((active or "").upper()) + ' leads</span>'
else:
    badge = '<span class="tb-pill off"><span class="tb-dot off"></span>offline · local-only</span>'
st.markdown('<div class="tb-head"><div><div class="tb-logo">Trading<span class="syn">·</span>Brain</div>'
            '<div class="tb-eyebrow">Deep analyst over your live journal — proposes, never auto-applies</div></div>'
            '<div class="tb-badge">' + badge + '</div></div>', unsafe_allow_html=True)

st.markdown(_chain_html(nodes), unsafe_allow_html=True)

# ---- KPI strip ------------------------------------------------------------- #
net = s["net_pnl"]
net_ac = "#10b981" if net > 0 else "#f43f5e" if net < 0 else "#33507e"
net_vc = "#34d399" if net > 0 else "#fb7185" if net < 0 else "#eef3fb"
wr_ac = "#10b981" if s["win_rate"] >= 55 else "#f43f5e" if s["n_closed"] and s["win_rate"] < 45 else "#3884ff"
sep = s["score_separates"]
sep_txt = "yes" if sep is True else ("no ⚠" if sep is False else "n/a")
sep_ac = "#10b981" if sep is True else "#f43f5e" if sep is False else "#33507e"
st.markdown('<div class="tb-kpis">'
            + _kpi("Reviews / taken", str(s["n_reviews"]) + " / " + str(s["n_taken"]), str(s["n_closed"]) + " closed", "#2dd4bf", "#eef3fb", 0)
            + _kpi("Net P&L", ("+" if net > 0 else "") + f"{net:.2f}", "PF " + str(s["profit_factor"]) + " · exp " + f'{s["expectancy"]:+.2f}', net_ac, net_vc, 60)
            + _kpi("Win rate", f'{s["win_rate"]:.1f}%', f'W{s["wins"]} · L{s["losses"]}', wr_ac, "#eef3fb", 120)
            + _kpi("Score separates", sep_txt, f'gap {s["score_gap"]}', sep_ac, "#eef3fb", 180)
            + _kpi("Avoidable / fragile", f'{s["avoidable_frac"]:.0f}% / {s["fragile_frac"]:.0f}%', "of losses / wins", "#fbbf24", "#fde68a", 240)
            + '</div>', unsafe_allow_html=True)

tab_p, tab_c, tab_m, tab_l, tab_b = st.tabs(["Pulse", "Chat", "Memory", "Library", "Lab"])

# ============================ PULSE ========================================= #
with tab_p:
    st.markdown('<div class="tb-panel"><div class="tb-h">Daily synthesis <span class="tag">deep read</span></div>'
                '<div class="tb-note">The brain reads the full post-mortem (overview, factor-edge table, hard-gate funnel, '
                'excursion, regime/duration/symbol/hour/weekday/martingale slices) plus your memory, and returns a '
                'sectioned report: headline, biggest leak, what works, a testable recalibration hypothesis, session '
                'edges, excursion read, two save-worthy observations, and at most one back-testable proposal. Every '
                'number is grounded — verify it in the Evidence expander.</div>', unsafe_allow_html=True)
    if st.button("Write today’s synthesis", type="primary", disabled=(not nodes_configured or s["n_reviews"] == 0)):
        with st.spinner("reading every review across the chain…"):
            try:
                synth = LLM.chat_with_chain(KB.build_messages(KB.SYNTHESIS_PROMPT, pm, rows, include_recent=True), max_tokens=1500)
                trace = LLM.chain_trace()
            except LLM.BrainLLMError as exc:
                synth = None
                trace = LLM.chain_trace()
                st.error("Synthesis failed: " + str(exc))
        if synth:
            prop = KB.find_proposal(synth)
            if prop:
                st.session_state.br_proposal = prop
            st.markdown('<div class="tb-bubble"><div class="who">brain · synthesis</div><div class="tb-body">' + render_assistant(synth) + '</div></div>', unsafe_allow_html=True)
            st.markdown(_trace_html(trace), unsafe_allow_html=True)
            if prop:
                st.success("A preset proposal was captured — open the Lab tab to back-test it before opting in.")
            with st.expander("🔎 Evidence the brain was given (verify, don’t trust blindly)"):
                st.text(KB.grounding_digest(pm))
    st.markdown('</div>', unsafe_allow_html=True)

    # deterministic, number-cited lesson proposals with one-click save
    proposed = KB.propose_lessons(pm)
    if proposed:
        st.markdown('<div class="tb-panel"><div class="tb-h">Suggested lessons from the numbers <span class="tag">one-click save</span></div>'
                    '<div class="tb-note">These are derived deterministically from your stats (not the LLM), each citing '
                    'its number. Save the ones you agree with — they become part of every future answer.</div>', unsafe_allow_html=True)
        for i, les in enumerate(proposed):
            tag_html = "".join('<span class="tb-tag">' + _esc(t) + '</span>' for t in les.get("tags", []))
            st.markdown('<div class="tb-lesson"><div class="meta">auto-proposed ' + tag_html + '</div>' + _esc(les["text"]) + '</div>', unsafe_allow_html=True)
            if st.button("Save as lesson", key="save_prop_" + str(i) + "_" + KB._slug(les["text"])):
                try:
                    KB.add_lesson(les["text"], les.get("tags", []), source="auto-proposed", confirmed=True)
                    st.success("Saved to memory.")
                    st.rerun()
                except Exception as exc:
                    st.error("Could not save: " + str(exc))
        st.markdown('</div>', unsafe_allow_html=True)

    st.markdown('<div class="tb-panel"><div class="tb-h">Provider chain</div>', unsafe_allow_html=True)
    for n in nodes:
        with st.expander(_esc(n["label"]) + " — " + ("configured" if n["configured"] else "not configured")):
            st.markdown('<div class="tb-note"><strong>Model:</strong> <code>' + _esc(n["model"]) +
                        '</code><br><strong>Note:</strong> ' + _esc(n["free_note"]) +
                        '<br><a href="' + _esc(n["signup"]) + '" target="_blank">Get a key →</a></div>', unsafe_allow_html=True)
    if st.button("Test the whole chain", disabled=(not nodes_configured)):
        with st.spinner("probing each configured provider…"):
            report = LLM.test_chain()
        for r in report:
            (st.success if r["ok"] else st.error)(("✓ " if r["ok"] else "✕ ") + r["msg"])
    st.markdown('</div>', unsafe_allow_html=True)

# ============================ CHAT ========================================== #
with tab_c:
    st.markdown('<div class="tb-panel"><div class="tb-h">Grounded chat <span class="tag">focused · streams</span></div>'
                '<div class="tb-note">Ask about a symbol (gold, V75…), a duration (2m), a time (14:00), or a factor '
                '(trigger, structure…) and the brain pulls that exact slice into its context, so the reply is specific '
                'to your question — not generic. Try: “why am I losing on gold?” or “is 2m worth keeping?”</div></div>', unsafe_allow_html=True)
    for m in st.session_state.br_chat:
        cls = "tb-bubble user" if m["role"] == "user" else "tb-bubble"
        who = "you" if m["role"] == "user" else "brain"
        body = _esc(m["content"]) if m["role"] == "user" else render_assistant(m["content"])
        st.markdown('<div class="' + cls + '"><div class="who">' + who + '</div><div class="tb-body">' + body + '</div></div>', unsafe_allow_html=True)

    if not nodes_configured:
        st.markdown('<div class="tb-warn">Chat needs at least one provider key (see the chain on Pulse). Everything else on this page works without one.</div>', unsafe_allow_html=True)
    else:
        with st.form("br_chat_form", clear_on_submit=True):
            q = st.text_area("Ask the brain", placeholder="e.g. is 2m worth keeping? / why do I lose on gold 14:00-15:00? / is my trigger factor doing any work?", height=92)
            sent = st.form_submit_button("Send", use_container_width=True)
        if sent and q.strip():
            q = q.strip()
            st.session_state.br_chat.append({"role": "user", "content": q})
            st.markdown('<div class="tb-bubble user"><div class="who">you</div><div class="tb-body">' + _esc(q) + '</div></div>', unsafe_allow_html=True)
            focus = KB.focus_slice(pm, rows, q)
            ph = st.empty()
            ph.markdown('<div class="tb-bubble"><div class="who">brain</div><div class="tb-typing">thinking<span class="tb-caret">▍</span></div></div>', unsafe_allow_html=True)
            messages = KB.build_messages(q, pm, rows, include_recent=True, focus=focus)
            buf, used = "", False
            try:
                for delta in LLM.stream_chat(messages):
                    buf += delta
                    used = True
                    ph.markdown('<div class="tb-bubble"><div class="who">brain</div><div class="tb-typing">' + render_assistant(buf) + '<span class="tb-caret">▍</span></div></div>', unsafe_allow_html=True)
            except LLM.BrainLLMError:
                if not used:
                    try:
                        buf = LLM.chat_with_chain(messages)
                    except LLM.BrainLLMError as exc:
                        buf = "[brain error] " + str(exc)
            except Exception:
                if not used:
                    buf = "[stream interrupted]"
            final = buf or "[no response]"
            st.session_state.br_chat.append({"role": "assistant", "content": final})
            prop = KB.find_proposal(final)
            if prop:
                st.session_state.br_proposal = prop
            ph.markdown('<div class="tb-bubble"><div class="who">brain</div><div class="tb-body">' + render_assistant(final) + '</div></div>', unsafe_allow_html=True)
            if focus:
                with st.expander("🎯 Slice the brain focused on"):
                    st.text(focus)
            if prop:
                st.success("A preset proposal was captured — open the Lab tab to back-test it.")
            st.rerun()
        with st.expander("Clear conversation"):
            if st.button("Clear"):
                st.session_state.br_chat = []
                st.rerun()

# ============================ MEMORY ======================================== #
with tab_m:
    st.markdown('<div class="tb-panel"><div class="tb-h">Train the memory <span class="tag">compounds</span></div>'
                '<div class="tb-note">Save what you notice; tag with a symbol or regime (e.g. <code>XAUUSD</code>, '
                '<code>SHORT</code>) to sharpen retrieval. Each lesson is pulled into future answers — the safe form of '
                '“training”: the corpus grows, the live strategy never moves from a chat.</div></div>', unsafe_allow_html=True)
    with st.form("br_lesson_form", clear_on_submit=True):
        txt = st.text_area("Observation", placeholder="XAUUSD chops hard 14:00–15:00 UTC on low volume — my losses there were duration, not entry.", height=92)
        tags = st.text_input("Tags (comma separated)", placeholder="XAUUSD, SHORT, duration")
        saved = st.form_submit_button("Save lesson", use_container_width=True)
    if saved:
        try:
            KB.add_lesson(txt, [t.strip() for t in (tags or "").split(",") if t.strip()], source="user", confirmed=True)
            st.success("Saved — it will shape the next brain answer.")
            st.rerun()
        except Exception as exc:
            st.error("Could not save: " + str(exc))

    lc1, lc2, lc3 = st.columns(3)
    with lc1:
        st.download_button("Export lessons (.jsonl)", KB.lessons_bytes(), file_name="brain_lessons.jsonl", mime="application/jsonl", use_container_width=True)
    with lc2:
        st.download_button("Export whole corpus (.md)", KB.kb_markdown_bytes(), file_name="brain_corpus_kb.md", mime="text/markdown", use_container_width=True)
    with lc3:
        up = st.file_uploader("Import lessons (.jsonl)", type=["jsonl", "json", "txt"], label_visibility="collapsed")
        if up is not None:
            stats = KB.import_lessons(up.read())
            st.success('Imported ' + str(stats["added"]) + ' · skipped ' + str(stats["skipped"]) + ' · errors ' + str(stats["errors"]) + '.')
            st.rerun()

    lessons = KB.load_lessons()
    if lessons:
        st.markdown('<div class="tb-h" style="margin-top:14px;">Stored (most recent first)</div>', unsafe_allow_html=True)
        for l in reversed(lessons):
            tag_html = "".join('<span class="tb-tag">' + _esc(t) + '</span>' for t in l.get("tags", []))
            cls = "tb-lesson brain" if l.get("source") in ("brain", "auto-proposed") else "tb-lesson"
            st.markdown('<div class="' + cls + '"><div class="meta">' + _esc(l.get("ts_utc", "")) + ' · ' + _esc(l.get("source", "user")) +
                        ' · ' + ("confirmed" if l.get("confirmed", True) else "draft") + ' ' + tag_html + '</div>' + _esc(l.get("text", "")) + '</div>', unsafe_allow_html=True)
    else:
        st.caption("No lessons yet. Add your first observation above.")

# ============================ LIBRARY ======================================= #
with tab_l:
    st.markdown('<div class="tb-panel"><div class="tb-h">Knowledge library <span class="tag">free KB</span></div>'
                '<div class="tb-note">Your own retrieval corpus — paste playbooks, notes, or a broker memo. It is '
                'chunked and searched alongside your lessons. Stored locally (survives on a VPS; export/import on Cloud).</div></div>', unsafe_allow_html=True)
    with st.form("br_doc_form", clear_on_submit=True):
        title = st.text_input("Title", placeholder="My XAUUSD session playbook")
        body = st.text_area("Document text", height=140)
        added = st.form_submit_button("Add to library", use_container_width=True)
    if added and body.strip():
        n = KB.add_document(body, title or "untitled")
        st.success("Indexed " + str(n) + " chunk(s).")
        st.rerun()

    sc1, sc2 = st.columns([3, 1])
    with sc1:
        sq = st.text_input("Search the corpus", placeholder="e.g. london open fakeout")
    with sc2:
        st.write("")
        st.write("")
    if sq.strip():
        _, hits = KB.retrieve(sq, k_lessons=0, k_docs=8)
        if hits:
            st.markdown('<div class="tb-h" style="margin-top:10px;">Top chunks</div>', unsafe_allow_html=True)
            for d in hits:
                st.markdown('<div class="tb-lesson"><div class="meta">' + _esc(d.get("title", "")) + ' · ' + _esc(d.get("id", "")) + '</div>' + _esc(d.get("text", "")) + '</div>', unsafe_allow_html=True)
        else:
            st.caption("No matching chunks.")

    d1, d2, d3 = st.columns(3)
    with d1:
        st.download_button("Export library (.jsonl)", KB.docs_bytes(), file_name="brain_docs.jsonl", mime="application/jsonl", use_container_width=True)
    with d2:
        st.download_button("Export corpus as KB (.md)", KB.kb_markdown_bytes(), file_name="brain_corpus_kb.md", mime="text/markdown", use_container_width=True)
    with d3:
        up = st.file_uploader("Import docs (.md/.txt/.jsonl)", type=["md", "txt", "jsonl", "json"], label_visibility="collapsed")
        if up is not None:
            n = KB.import_kb(up.read(), up.name)
            st.success("Indexed " + str(n) + " chunk(s) from " + _esc(up.name) + ".")
            st.rerun()

    docs = KB.list_documents()
    if docs:
        st.markdown('<div class="tb-h" style="margin-top:14px;">Documents</div>', unsafe_allow_html=True)
        st.dataframe(pd.DataFrame(_arrow_safe(docs)), use_container_width=True, hide_index=True)
    else:
        st.caption("Library empty. Add your first document above.")

# ============================ LAB =========================================== #
with tab_b:
    st.markdown('<div class="tb-panel"><div class="tb-h">Gate backtest <span class="tag">validate first</span></div>'
                '<div class="tb-note">Replays your recorded reviews through re-weighted factors + a threshold. Weights '
                'are MULTIPLIERS on the stored sub-scores (1 = unchanged). <strong>kept_pnl</strong> is real (trades that '
                'still fire). <strong>dropped_pnl</strong> is real too — the P&amp;L this variant would skip: negative = '
                'avoids losers (good); positive = cuts winners (bad). <strong>added_unknown</strong> are soft '
                'score-rejections it would newly fire (hard-gate fails are never counted, honestly). Forward-test any '
                'winner on demo before opting in.</div></div>', unsafe_allow_html=True)

    preset_opts = list(KB.PRESETS.keys())
    if st.session_state.br_proposal:
        preset_opts = ["🧠 brain proposal"] + preset_opts
    pick = st.selectbox("Preset", preset_opts, key="bt_preset")
    if pick == "🧠 brain proposal":
        prop = st.session_state.br_proposal or {}
        weights, threshold = prop.get("weights", dict(KB.DEFAULT_WEIGHTS)), int(prop.get("threshold", 20))
        st.info('Brain proposal “' + str(prop.get("name", "")) + '” — ' + str(prop.get("rationale", "")))
    else:
        weights = KB.PRESETS[pick]
        threshold = st.select_slider("Threshold", options=KB.THRESHOLD_OPTIONS, value=20, key="bt_thr")

    if not rows:
        st.info("No journal rows yet — nothing to replay. Run the terminal and let reviews accumulate.")
    else:
        base = KB.baseline(rows)
        res = KB.backtest(rows, weights, threshold)
        st.dataframe(pd.DataFrame(_arrow_safe([base, res])), use_container_width=True, hide_index=True)
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Kept win rate", f'{res["kept_win_rate"]:.1f}%', f'{base["kept_win_rate"]:.1f}% as-built')
        m2.metric("Dropped P&L", f'{res["dropped_pnl"]:+.2f}', str(res["dropped_losses_avoided"]) + " losers avoided")
        m3.metric("Wins lost", str(res["dropped_wins_lost"]), "trades it would cut")
        m4.metric("Newly fired", str(res["added_unknown"]), "unknown — forward-test")
        if res["dropped_pnl"] < 0 and res["dropped_wins_lost"] <= max(1, res["dropped_losses_avoided"] // 2):
            verdict = "Promising: it skips more losing money than winning. Forward-test on demo, then opt in."
        elif res["dropped_pnl"] > 0:
            verdict = "Caution: this variant would have skipped net winners. Probably not an improvement."
        else:
            verdict = "Marginal: little real money moved. Collect more data before deciding."
        st.markdown('<div class="tb-note"><strong>Read:</strong> ' + _esc(verdict) + '</div>', unsafe_allow_html=True)
        ptxt = KB.preset_text(pick if pick != "🧠 brain proposal" else (st.session_state.br_proposal or {}).get("name", "brain-proposal"),
                              weights, threshold, "" if pick != "🧠 brain proposal" else (st.session_state.br_proposal or {}).get("rationale", ""))
        st.download_button("Download preset (.txt)", ptxt.encode("utf-8"),
                           file_name=("strategy_preset_" + pick.replace(" ", "_") + ".txt"), mime="text/plain", use_container_width=True)
        st.caption("Opt in: add the downloaded preset to config.STRATEGY_SENSITIVITY_PRESETS (or map it onto a preset name), then select it in the terminal sidebar. The bot never applies this on its own.")

st.markdown('<div class="tb-note" style="margin-top:18px;text-align:center;">Observe → ask the brain → brain proposes → back-test → opt in. The live strategy is untouched by everything on this page.</div>', unsafe_allow_html=True)
try:
    st.page_link("dashboard.py", label="← Back to Terminal", use_container_width=True)
except Exception:
    pass
