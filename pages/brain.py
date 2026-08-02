"""pages/brain.py — the Trading Brain cockpit (free, grounded, alive).

Auto-added to the Streamlit sidebar by multipage discovery — NO edit to
dashboard.py or bubbles.py. Five tabs:

  Pulse   — live provider status, KPIs, one-click daily synthesis (brain diary)
  Chat    — streaming, grounded conversation (rulebook + post-mortem + memory)
  Memory  — your lessons + the brain's diary entries (the trainable corpus)
  Library — a free knowledge base: add / search / export / import documents
  Lab     — gate-backtest over recorded reviews + exportable presets

Nothing here places a trade or mutates the live strategy. If no LLM key is set,
the page still fully works offline (Memory + Library search + numeric
post-mortem + Lab); only the conversational/streaming layer needs a free key.
"""
import html
import inspect as _inspect
import os
import re
import sys
import time

import pandas as pd
import streamlit as st

_here = os.path.dirname(os.path.abspath(__file__))
for _c in (_here, os.path.dirname(_here)):
    if os.path.isdir(os.path.join(_c, "src")) and _c not in sys.path:
        sys.path.insert(0, _c)
        break


def _mm_patch_width():
    for _name in ("dataframe", "table", "plotly_chart", "line_chart", "bar_chart",
                  "area_chart", "button", "download_button", "link_button", "page_link"):
        _orig = getattr(st, _name, None)
        if _orig is None or getattr(_orig, "_mm_width_patched", False):
            continue
        try:
            _params = _inspect.signature(_orig).parameters
        except (TypeError, ValueError):
            continue
        if "width" not in _params:
            continue

        def _make(o):
            def _w(*a, **k):
                if "use_container_width" in k and "width" not in k:
                    k["width"] = "stretch" if k.pop("use_container_width") else "content"
                return o(*a, **k)
            _w._mm_width_patched = True
            _w.__name__ = getattr(o, "__name__", "wrapped")
            return _w
        setattr(st, _name, _make(_orig))


_mm_patch_width()

import src.brain_llm as LLM  # noqa: E402
import src.brain_kb as KB  # noqa: E402
from src.journal import get_journal  # noqa: E402

st.set_page_config(page_title="Trading Brain", page_icon="🧠", layout="wide", initial_sidebar_state="expanded")

st.markdown("""<style>
@import url('https://fonts.googleapis.com/css2?family=Sora:wght@500;600;700;800&family=IBM+Plex+Sans:wght@400;500;600&family=JetBrains+Mono:wght@500;600;700&display=swap');
html,body,.stApp{background:#060912;color:#c7d2e0;font-family:'IBM Plex Sans',sans-serif;}
.stApp{
  background-image:
    radial-gradient(1000px 520px at 5% -10%, rgba(45,212,191,0.11), transparent 60%),
    radial-gradient(900px 500px at 100% 112%, rgba(251,191,36,0.07), transparent 60%),
    radial-gradient(760px 420px at 50% 120%, rgba(56,132,255,0.06), transparent 60%),
    linear-gradient(rgba(120,150,190,0.045) 1px, transparent 1px),
    linear-gradient(90deg, rgba(120,150,190,0.045) 1px, transparent 1px);
  background-size:auto,auto,auto,24px 24px,24px 24px;
  background-attachment:fixed;
  animation:br-drift 44s ease-in-out infinite alternate;
}
@keyframes br-drift{0%{background-position:0 0,0 0,0 0,0 0,0 0;}100%{background-position:26px -22px,-24px 20px,18px -14px,0 0,0 0;}}
[data-testid="stMainBlockContainer"]{max-width:1500px;padding-top:1.2rem;}
[data-testid="stSidebar"]{background:#0a0f1c;border-right:1px solid #1b2740;}
#MainMenu,footer{visibility:hidden;}

.br-head{position:relative;display:flex;align-items:flex-end;justify-content:space-between;padding:6px 2px 16px 2px;overflow:hidden;}
.br-head::after{content:"";position:absolute;left:0;right:0;bottom:0;height:2px;border-radius:2px;background:linear-gradient(90deg,#2dd4bf,#3884ff 46%,#fbbf24 78%,transparent 96%);background-size:240% 100%;animation:br-scan 7s linear infinite;}
@keyframes br-scan{0%{background-position:120% 0;}100%{background-position:-120% 0;}}
.br-logo{font-family:'Sora',sans-serif;font-weight:800;font-size:1.5rem;letter-spacing:.14em;text-transform:uppercase;color:#eef3fb;}
.br-logo .syn{color:#2dd4bf;}
.br-eyebrow{font-family:'Sora',sans-serif;font-size:.58rem;font-weight:600;letter-spacing:.24em;text-transform:uppercase;color:#4f6080;margin-top:6px;}
.br-prov{font-family:'JetBrains Mono',monospace;font-size:.7rem;text-align:right;}
.br-badge{display:inline-flex;align-items:center;gap:7px;padding:4px 11px;border-radius:20px;font-weight:600;font-size:.66rem;letter-spacing:.04em;}
.br-badge.on{background:rgba(45,212,191,.12);border:1px solid rgba(45,212,191,.34);color:#5eead4;}
.br-badge.off{background:rgba(91,107,133,.1);border:1px solid rgba(91,107,133,.26);color:#8294b0;}
.br-pulse{width:8px;height:8px;border-radius:50%;background:#2dd4bf;animation:br-pulse 1.8s ease-in-out infinite;}
.br-pulse.off{background:#5b6b85;animation:none;}
@keyframes br-pulse{0%,100%{box-shadow:0 0 0 0 rgba(45,212,191,.5);}50%{box-shadow:0 0 0 6px rgba(45,212,191,0);}}

.br-kpis{display:grid;grid-template-columns:repeat(5,1fr);gap:13px;margin:8px 0 18px 0;}
@media(max-width:1100px){.br-kpis{grid-template-columns:repeat(2,1fr);}}
.br-kpi{position:relative;background:linear-gradient(150deg,#0c1426,#0e1830);border:1px solid #1d2c49;border-radius:12px;padding:15px 17px;overflow:hidden;transition:transform .16s ease,border-color .16s ease,box-shadow .16s ease;animation:br-rise .55s cubic-bezier(.2,.7,.2,1) both;}
.br-kpi:hover{transform:translateY(-3px);border-color:#2c466e;box-shadow:0 12px 28px rgba(0,0,0,.42);}
.br-kpi::before{content:"";position:absolute;top:0;left:0;right:0;height:3px;background:var(--ac,#33507e);}
.br-kpi .l{font-family:'Sora',sans-serif;font-size:.58rem;font-weight:600;letter-spacing:.15em;text-transform:uppercase;color:#6b7c97;}
.br-kpi .v{font-family:'JetBrains Mono',monospace;font-weight:700;font-size:1.5rem;margin-top:8px;color:var(--vc,#eef3fb);font-variant-numeric:tabular-nums;}
.br-kpi .s{font-family:'JetBrains Mono',monospace;font-size:.64rem;color:#6b7c97;margin-top:6px;}
@keyframes br-rise{from{opacity:0;transform:translateY(12px);}to{opacity:1;transform:none;}}

.br-panel{position:relative;background:linear-gradient(160deg,#0c1426,#0b1222);border:1px solid #1d2c49;border-radius:14px;padding:16px 18px;margin:12px 0;overflow:hidden;transition:border-color .18s ease,box-shadow .18s ease;}
.br-panel::before{content:"";position:absolute;top:0;left:0;right:0;height:1px;background:linear-gradient(90deg,transparent,rgba(120,160,220,.22),transparent);}
.br-panel:hover{border-color:#2c466e;box-shadow:0 12px 30px rgba(0,0,0,.34);}
.br-h{font-family:'Sora',sans-serif;font-size:.64rem;font-weight:700;letter-spacing:.16em;text-transform:uppercase;color:#8294b0;margin-bottom:11px;display:flex;align-items:center;gap:10px;}
.br-h .tag{margin-left:auto;font-family:'JetBrains Mono',monospace;font-size:.58rem;font-weight:600;letter-spacing:.08em;color:#5eead4;background:rgba(45,212,191,.1);border:1px solid rgba(45,212,191,.3);padding:2px 9px;border-radius:20px;}

.br-meter{height:8px;border-radius:5px;background:#16223c;overflow:hidden;margin:7px 0;}
.br-meter .f{height:100%;border-radius:5px;background:linear-gradient(90deg,#3884ff,#2dd4bf);transition:width .8s cubic-bezier(.2,.7,.2,1);}
.br-meter .f.hot{background:linear-gradient(90deg,#2dd4bf,#34d399);}

.br-bubble{background:#0e1830;border:1px solid #233452;border-radius:13px;padding:13px 15px;margin:11px 0;font-size:.88rem;line-height:1.62;}
.br-bubble.user{background:rgba(56,132,255,.08);border-color:rgba(56,132,255,.3);}
.br-bubble .who{font-family:'Sora',sans-serif;font-size:.58rem;font-weight:700;letter-spacing:.14em;text-transform:uppercase;color:#6b7c97;margin-bottom:7px;}
.br-typing{font-family:'JetBrains Mono',monospace;font-size:.82rem;white-space:pre-wrap;color:#dbe6f5;}
.br-caret{display:inline-block;color:#2dd4bf;animation:br-blink 1s steps(1) infinite;}
@keyframes br-blink{50%{opacity:0;}}
.br-body{color:#dbe6f5;}
.br-body strong{color:#eef3fb;}
.br-body code{background:#0a1120;border:1px solid #233452;border-radius:4px;padding:0 4px;font-family:'JetBrains Mono',monospace;font-size:.82em;color:#7fd4ff;}
.br-body ul{margin:6px 0;padding-left:18px;}
.br-body li{margin:3px 0;}

.br-lesson{background:#0e1830;border:1px solid #233452;border-left:3px solid #2dd4bf;border-radius:10px;padding:10px 12px;margin:8px 0;font-size:.82rem;line-height:1.5;transition:transform .14s ease,border-color .14s ease;}
.br-lesson:hover{transform:translateX(3px);border-color:#33507e;}
.br-lesson.brain{border-left-color:#fbbf24;}
.br-lesson .meta{font-family:'JetBrains Mono',monospace;font-size:.62rem;color:#6b7c97;margin-bottom:4px;}
.br-tag{display:inline-block;font-family:'JetBrains Mono',monospace;font-size:.58rem;font-weight:700;padding:1px 7px;border-radius:6px;background:rgba(45,212,191,.12);color:#5eead4;border:1px solid rgba(45,212,191,.3);margin-right:5px;}

.br-warn{background:rgba(251,191,36,.08);border:1px solid rgba(251,191,36,.3);border-radius:11px;padding:12px 14px;font-size:.8rem;color:#fde68a;margin:10px 0;line-height:1.55;}
.br-note{font-size:.8rem;line-height:1.6;color:#9fb0c9;}
.br-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px;}
@media(max-width:820px){.br-grid{grid-template-columns:1fr;}}
.pos{color:#4ade80;} .neg{color:#fb7185;} .mut{color:#6b7c97;}
[data-testid="stDataFrame"]{border:0;}
[data-testid="stButton"] button{border-radius:9px;font-family:'Sora',sans-serif;font-weight:600;letter-spacing:.04em;transition:filter .15s ease,transform .12s ease;}
[data-testid="stButton"] button:hover{filter:brightness(1.14);}
[data-testid="stButton"] button:active{transform:scale(.985);}
</style>""", unsafe_allow_html=True)


def _esc(t: Any) -> str:
    return html.escape("" if t is None else str(t))


def _fmt(text: str) -> str:
    """Escape first (safe), then apply a tiny markdown-ish transform producing
    only our own tags, so streaming output is both styled and injection-safe."""
    esc = _esc(text)
    esc = re.sub(r"`([^`]+)`", r"<code>\1</code>", esc)
    esc = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", esc)
    out, in_list = [], False
    for line in esc.split("\n"):
        s = line.strip()
        m = re.match(r"^[-*]\s+(.*)$", s)
        if m:
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{m.group(1)}</li>")
        else:
            if in_list:
                out.append("</ul>")
                in_list = False
            out.append(line + "<br>" if s else "<br>")
    if in_list:
        out.append("</ul>")
    return "".join(out)


def _kpi(label, value, sub, ac, vc, delay=0):
    return (f'<div class="br-kpi" style="--ac:{ac};--vc:{vc};animation-delay:{delay}ms;">'
            f'<div class="l">{_esc(label)}</div><div class="v">{_esc(value)}</div>'
            f'<div class="s">{_esc(sub)}</div></div>')


def _glitch(where, exc):
    st.markdown(f'<div class="br-warn">⚠ {_esc(where)} hit a snag — your data is intact; this is a render edge case.</div>', unsafe_allow_html=True)
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
    pm = {"summary": {"reviews": 0, "taken": 0, "closed": 0, "wins": 0, "losses": 0,
                      "win_rate": 0.0, "net_pnl": 0.0, "profit_factor": 0.0,
                      "expectancy": 0.0, "lessons": 0, "doc_chunks": 0},
          "avoidable_losses": [], "fragile_wins": [], "gatekeeper_factors": [],
          "by_symbol": {}, "by_hour": {}, "by_regime": {}, "by_duration": {}}
    _glitch("post-mortem", _e)

prov = LLM.detect_provider()
s = pm["summary"]

for k in ("br_chat", "br_proposal"):
    if k not in st.session_state:
        st.session_state[k] = [] if k == "br_chat" else None

# ---- header ---------------------------------------------------------------- #
if prov:
    badge = f'<span class="br-badge on"><span class="br-pulse"></span>{_esc(LLM.PROVIDER_INFO[prov]["label"])} live</span>'
else:
    badge = '<span class="br-badge off"><span class="br-pulse off"></span>offline · local-only</span>'
st.markdown(
    f'<div class="br-head"><div><div class="br-logo">Trading<span class="syn">·</span>Brain</div>'
    f'<div class="br-eyebrow">Free grounded analyst over your live journal — proposes, never auto-applies</div></div>'
    f'<div class="br-prov">{badge}</div></div>', unsafe_allow_html=True)

# ---- KPI strip ------------------------------------------------------------- #
net = s["net_pnl"]
net_ac = "#10b981" if net > 0 else "#f43f5e" if net < 0 else "#33507e"
net_vc = "#34d399" if net > 0 else "#fb7185" if net < 0 else "#eef3fb"
wr_ac = "#10b981" if s["win_rate"] >= 55 else "#f43f5e" if s["closed"] and s["win_rate"] < 45 else "#3884ff"
st.markdown(
    '<div class="br-kpis">'
    + _kpi("Reviews / taken", f'{s["reviews"]} / {s["taken"]}', f'{s["closed"]} closed', "#2dd4bf", "#eef3fb", 0)
    + _kpi("Net P&L", f"{net:+.2f}", f'win rate {s["win_rate"]:.1f}%', net_ac, net_vc, 60)
    + _kpi("Avoidable losses", str(len(pm["avoidable_losses"])), "were in profit, reversed", "#fbbf24", "#fde68a", 120)
    + _kpi("Memory", str(s["lessons"]), "lessons + diary", "#a855f7", "#eef3fb", 180)
    + _kpi("Library", str(s["doc_chunks"]), "knowledge chunks", "#3884ff", "#eef3fb", 240)
    + '</div>', unsafe_allow_html=True)

tab_p, tab_c, tab_m, tab_l, tab_b = st.tabs(["Pulse", "Chat", "Memory", "Library", "Lab"])

# ============================ PULSE ========================================= #
with tab_p:
    st.markdown('<div class="br-panel"><div class="br-h">Provider status <span class="tag">all free</span></div>', unsafe_allow_html=True)
    cfg = LLM.configured_providers()
    if cfg:
        st.markdown('<div class="br-note">Configured: ' + ", ".join(f"<strong>{_esc(LLM.PROVIDER_INFO[p]['label'])}</strong>" for p in cfg) +
                    f' · active: <strong>{_esc(LLM.PROVIDER_INFO[prov]["label"])}</strong>. Set BRAIN_PROVIDER to force one.</div>', unsafe_allow_html=True)
    else:
        st.markdown('<div class="br-warn">No LLM key set yet — Chat &amp; daily synthesis are paused, but Memory, Library search, the numeric post-mortem and the Lab all work fully offline. Add a free key per BRAIN_SETUP.md (Groq is the fastest one-click option).</div>', unsafe_allow_html=True)
    cols = st.columns(len(LLM.PROVIDER_INFO))
    for col, key in zip(cols, LLM.PROVIDER_INFO):
        info = LLM.PROVIDER_INFO[key]
        have = "✓ key" if (key == "openai_compat" and os.getenv("OPENAI_COMPAT_BASE") or st.secrets.get("OPENAI_COMPAT_BASE", "") if key == "openai_compat" else (os.getenv(info["env_key"]) or st.secrets.get(info["env_key"], ""))) else "· no key"
        with col:
            st.markdown(f'<div class="br-panel" style="margin:0;"><div class="br-h">{_esc(info["label"])}</div>'
                        f'<div class="br-note">{_esc(have)}<br>{_esc(info["free_note"])}</div></div>', unsafe_allow_html=True)
    if st.button("Test active provider", disabled=prov is None):
        with st.spinner("probing…"):
            ok, dt, msg = LLM.test_provider(prov)
        (st.success if ok else st.error)(msg)
    st.markdown('</div>', unsafe_allow_html=True)

    st.markdown('<div class="br-panel"><div class="br-h">Daily synthesis <span class="tag">brain diary</span></div>'
                '<div class="br-note">The brain reads your post-mortem + memory and writes a one-paragraph diary entry, then saves it to Memory (source = brain). It is a saved note — never a strategy change.</div>', unsafe_allow_html=True)
    if st.button("Write today’s synthesis", type="primary", disabled=prov is None or s["reviews"] == 0):
        q = ("Write a single tight paragraph: today’s trading diary. Name the single biggest leak "
             "(avoidable losses vs fragile wins vs a gatekeeper), the one symbol/hour/regime to favour or "
             "avoid, and one concrete, safe next step. Reference the numbers in the grounding. Do NOT emit a "
             "preset proposal.")
        with st.spinner("synapses firing…"):
            try:
                diary = LLM.chat(KB.build_messages(q, pm, rows, include_recent=False), max_tokens=420)
                KB.add_lesson(diary, tags=["diary", "synthesis"], source="brain", confirmed=True)
                st.success("Saved to Memory.")
                st.markdown(f'<div class="br-bubble"><div class="who">brain · diary</div><div class="br-body">{_fmt(diary)}</div></div>', unsafe_allow_html=True)
            except LLM.BrainLLMError as exc:
                st.error(f"Brain error: {exc}")
    st.markdown('</div>', unsafe_allow_html=True)

    st.markdown('<div class="br-panel"><div class="br-h">At a glance</div>', unsafe_allow_html=True)
    g1, g2 = st.columns(2)
    with g1:
        st.markdown('<div class="mini-h" style="font-family:Sora;font-size:.56rem;font-weight:700;letter-spacing:.14em;text-transform:uppercase;color:#8294b0;margin-bottom:8px;">Gatekeepers</div>', unsafe_allow_html=True)
        gk = pm["gatekeeper_factors"]
        if gk:
            mx = gk[0][1]
            for k, v in gk[:6]:
                lbl = next((lab for kk, lab, _ in KB.FACTORS if kk == k), k)
                st.markdown(f'<div style="display:flex;align-items:center;gap:10px;margin:5px 0;font-family:JetBrains Mono,monospace;font-size:.72rem;">'
                            f'<span style="width:96px;color:#9fb0c9;">{_esc(lbl)}</span>'
                            f'<span class="br-meter" style="flex:1;margin:0;"><span class="f" style="width:{v/mx*100:.0f}%;"></span></span>'
                            f'<span style="width:28px;text-align:right;color:#eef3fb;font-weight:700;">{v}</span></div>', unsafe_allow_html=True)
        else:
            st.caption("No near-miss trending stand-asides yet.")
    with g2:
        st.markdown('<div class="mini-h" style="font-family:Sora;font-size:.56rem;font-weight:700;letter-spacing:.14em;text-transform:uppercase;color:#8294b0;margin-bottom:8px;">Edge by symbol</div>', unsafe_allow_html=True)
        if pm["by_symbol"]:
            st.dataframe(pd.DataFrame(pm["by_symbol"]).T, use_container_width=True, hide_index=True)
        else:
            st.caption("No closed trades yet.")
    st.markdown('</div>', unsafe_allow_html=True)

# ============================ CHAT ========================================== #
with tab_c:
    st.markdown('<div class="br-panel"><div class="br-h">Grounded chat <span class="tag">streams</span></div>'
                '<div class="br-note">Every message is sent with the rulebook + your live post-mortem + your most relevant lessons &amp; library chunks + recent reviews, so the brain answers about <em>your</em> bot and <em>your</em> history.</div></div>', unsafe_allow_html=True)
    for m in st.session_state.br_chat:
        cls = "br-bubble user" if m["role"] == "user" else "br-bubble"
        who = "you" if m["role"] == "user" else "brain"
        body = _esc(m["content"]) if m["role"] == "user" else _fmt(m["content"])
        st.markdown(f'<div class="{cls}"><div class="who">{who}</div><div class="br-body">{body}</div></div>', unsafe_allow_html=True)

    if prov is None:
        st.markdown('<div class="br-warn">Chat needs a free LLM key (see BRAIN_SETUP.md). Everything else on this page works without one.</div>', unsafe_allow_html=True)
    else:
        with st.form("br_chat_form", clear_on_submit=True):
            q = st.text_area("Ask the brain", placeholder="e.g. why do I keep losing on XAUUSD 14:00–15:00 UTC? is my pullback gate too tight?", height=92)
            sent = st.form_submit_button("Send", use_container_width=True)
        if sent and q.strip():
            q = q.strip()
            st.session_state.br_chat.append({"role": "user", "content": q})
            st.markdown(f'<div class="br-bubble user"><div class="who">you</div><div class="br-body">{_esc(q)}</div></div>', unsafe_allow_html=True)
            ph = st.empty()
            ph.markdown('<div class="br-bubble"><div class="who">brain</div><div class="br-typing">thinking<span class="br-caret">▍</span></div></div>', unsafe_allow_html=True)
            messages = KB.build_messages(q, pm, rows)
            buf, used = "", False
            try:
                for delta in LLM.stream_chat(messages, provider=prov):
                    buf += delta
                    used = True
                    ph.markdown(f'<div class="br-bubble"><div class="who">brain</div><div class="br-typing">{_fmt(buf)}<span class="br-caret">▍</span></div></div>', unsafe_allow_html=True)
            except LLM.BrainLLMError:
                if not used:
                    try:
                        buf = LLM.chat(messages, provider=prov)
                    except LLM.BrainLLMError as exc:
                        buf = f"[brain error] {exc}"
            except Exception:
                if not used:
                    buf = "[stream interrupted]"
            final = buf or "[no response]"
            st.session_state.br_chat.append({"role": "assistant", "content": final})
            prop = KB.find_proposal(final)
            if prop:
                st.session_state.br_proposal = prop
            ph.markdown(f'<div class="br-bubble"><div class="who">brain</div><div class="br-body">{_fmt(final)}</div></div>', unsafe_allow_html=True)
            if prop:
                st.success("The brain emitted a preset proposal — loaded in the Lab tab for validation.")
            st.rerun()
        with st.expander("Clear / advanced"):
            if st.button("Clear conversation"):
                st.session_state.br_chat = []
                st.rerun()

# ============================ MEMORY ======================================== #
with tab_m:
    st.markdown('<div class="br-panel"><div class="br-h">Train the memory <span class="tag">compounds</span></div>'
                '<div class="br-note">Save what you notice; tag with a symbol or regime (e.g. <code>XAUUSD</code>, <code>SHORT</code>) to sharpen retrieval. Each lesson is pulled into future answers — this is the safe form of “training”: the corpus grows, the live strategy never moves from a chat.</div></div>', unsafe_allow_html=True)
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
            st.error(f"Could not save: {exc}")

    lc1, lc2, lc3 = st.columns(3)
    with lc1:
        st.download_button("Export lessons (.jsonl)", KB.lessons_bytes(), file_name="brain_lessons.jsonl", mime="application/jsonl", use_container_width=True)
    with lc2:
        st.download_button("Export whole corpus (.md)", KB.kb_markdown_bytes(), file_name="brain_corpus_kb.md", mime="text/markdown", use_container_width=True)
    with lc3:
        up = st.file_uploader("Import lessons (.jsonl)", type=["jsonl", "json", "txt"], label_visibility="collapsed")
        if up is not None:
            stats = KB.import_lessons(up.read())
            st.success(f'Imported {stats["added"]} · skipped {stats["skipped"]} · errors {stats["errors"]}.')
            st.rerun()

    lessons = KB.load_lessons()
    if lessons:
        st.markdown('<div class="br-h" style="margin-top:14px;">Stored (most recent first)</div>', unsafe_allow_html=True)
        for l in reversed(lessons):
            tag_html = "".join(f'<span class="br-tag">{_esc(t)}</span>' for t in l.get("tags", []))
            cls = "br-lesson brain" if l.get("source") == "brain" else "br-lesson"
            st.markdown(f'<div class="{cls}"><div class="meta">{_esc(l.get("ts_utc",""))} · {_esc(l.get("source","user"))} '
                        f'· {"confirmed" if l.get("confirmed", True) else "draft"} {tag_html}</div>{_esc(l.get("text",""))}</div>', unsafe_allow_html=True)
    else:
        st.caption("No lessons yet. Add your first observation above.")

# ============================ LIBRARY ======================================= #
with tab_l:
    st.markdown('<div class="br-panel"><div class="br-h">Knowledge library <span class="tag">free KB</span></div>'
                '<div class="br-note">Your own retrieval corpus — paste playbooks, notes, or a broker memo. It is chunked and searched alongside your lessons. This is the “knowledge base” from the RAG blueprint, rebuilt for free and stored locally (survives on a VPS; export/import on Cloud).</div></div>', unsafe_allow_html=True)
    with st.form("br_doc_form", clear_on_submit=True):
        title = st.text_input("Title", placeholder="My XAUUSD session playbook")
        body = st.text_area("Document text", height=140)
        added = st.form_submit_button("Add to library", use_container_width=True)
    if added and body.strip():
        n = KB.add_document(body, title or "untitled")
        st.success(f"Indexed {n} chunk(s).")
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
            st.markdown('<div class="br-h" style="margin-top:10px;">Top chunks</div>', unsafe_allow_html=True)
            for d in hits:
                st.markdown(f'<div class="br-lesson"><div class="meta">{_esc(d.get("title",""))} · {_esc(d.get("id",""))}</div>{_esc(d.get("text",""))}</div>', unsafe_allow_html=True)
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
            st.success(f"Indexed {n} chunk(s) from {up.name}.")
            st.rerun()

    docs = KB.list_documents()
    if docs:
        st.markdown('<div class="br-h" style="margin-top:14px;">Documents</div>', unsafe_allow_html=True)
        st.dataframe(pd.DataFrame(docs), use_container_width=True, hide_index=True)
    else:
        st.caption("Library empty. Add your first document above.")

# ============================ LAB =========================================== #
with tab_b:
    st.markdown('<div class="br-panel"><div class="br-h">Gate backtest <span class="tag">validate first</span></div>'
                '<div class="br-note">Replays your recorded reviews through re-weighted factors + a threshold. <strong>kept_pnl</strong> is real (trades that still fire). <strong>dropped_pnl</strong> is real too — the P&amp;L this variant would have skipped: negative = avoids losers (good); positive = cuts winners (bad). <strong>added_unknown</strong> need a forward test on demo. Approximate: re-weighting uses the recorded integer factor scores.</div></div>', unsafe_allow_html=True)

    preset_opts = list(KB.PRESETS.keys())
    if st.session_state.br_proposal:
        preset_opts = ["🧠 brain proposal"] + preset_opts
    pick = st.selectbox("Preset", preset_opts, key="bt_preset")
    if pick == "🧠 brain proposal":
        prop = st.session_state.br_proposal or {}
        weights, threshold = prop.get("weights", KB.DEFAULT_WEIGHTS), int(prop.get("threshold", 20))
        st.info(f'Brain proposal “{prop.get("name","")}” — {prop.get("rationale","")}')
    else:
        weights = KB.PRESETS[pick]
        threshold = st.select_slider("Threshold", options=KB.THRESHOLD_OPTIONS, value=20, key="bt_thr")

    if not rows:
        st.info("No journal rows yet — nothing to replay. Run the terminal and let reviews accumulate.")
    else:
        base = KB.baseline(rows)
        res = KB.backtest(rows, weights, threshold)
        st.dataframe(pd.DataFrame([base, res]), use_container_width=True, hide_index=True)
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Kept win rate", f'{res["kept_win_rate"]:.1f}%', f'{base["kept_win_rate"]:.1f}% as-built')
        m2.metric("Dropped P&L", f'{res["dropped_pnl"]:+.2f}', f'{res["dropped_losses_avoided"]} losers avoided')
        m3.metric("Wins lost", str(res["dropped_wins_lost"]), "trades it would cut")
        m4.metric("Newly fired", str(res["added_unknown"]), "unknown — forward-test")
        if res["dropped_pnl"] < 0 and res["dropped_wins_lost"] <= max(1, res["dropped_losses_avoided"] // 2):
            verdict = "Promising: it skips more losing money than winning. Forward-test on demo, then opt in."
        elif res["dropped_pnl"] > 0:
            verdict = "Caution: this variant would have skipped net winners. Probably not an improvement."
        else:
            verdict = "Marginal: little real money moved. Collect more data before deciding."
        st.markdown(f'<div class="br-note"><strong>Read:</strong> {_esc(verdict)}</div>', unsafe_allow_html=True)
        ptxt = KB.preset_text(pick if pick != "🧠 brain proposal" else (st.session_state.br_proposal or {}).get("name", "brain-proposal"),
                              weights, threshold, "" if pick != "🧠 brain proposal" else (st.session_state.br_proposal or {}).get("rationale", ""))
        st.download_button("Download preset (.txt)", ptxt.encode("utf-8"),
                           file_name=f"strategy_preset_{pick.replace(' ','_')}.txt", mime="text/plain", use_container_width=True)
        st.caption("Opt in: add the downloaded preset to config.STRATEGY_SENSITIVITY_PRESETS (or map it onto a preset name), then select it in the terminal sidebar. The bot never applies this on its own.")

st.markdown('<div class="br-note" style="margin-top:18px;text-align:center;">Observe → ask the brain → brain proposes → backtest → opt in. The live strategy is untouched by everything on this page.</div>', unsafe_allow_html=True)
try:
    st.page_link("dashboard.py", label="← Back to Terminal", use_container_width=True)
except Exception:
    pass
