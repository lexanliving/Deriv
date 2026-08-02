"""pages/brain.py — the Trading Brain cockpit.

A grounded, trainable intelligence layer over the running Deriv bot. Auto-added
to the Streamlit sidebar by multipage discovery (no edit to dashboard.py needed).

Four tabs:
  Findings  — the brain reads your trades (post-mortem + your lessons) and writes
              the plain-language read; numeric cards show even when the brain is
              offline. A brain proposal (if any) is captured for the Backtest tab.
  Chat      — grounded conversation (rulebook + post-mortem + lessons + recent
              reviews). Degrades to a friendly message if the agent is not set up.
  Lessons   — the trainable memory: add / list / export / import observations,
              and export them as a knowledge-base document.
  Backtest  — replay your recorded reviews through any preset or a brain proposal
              and see the REAL pnl of dropped trades. Works with NO agent.

Nothing here places a trade or mutates the live strategy. Ever.
"""
import inspect as _inspect
import json
import os
import sys

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

import src.brain as B  # noqa: E402  (after sys.path fix)
from src.journal import get_journal  # noqa: E402

st.set_page_config(page_title="Trading Brain", page_icon="🧠", layout="wide", initial_sidebar_state="expanded")

st.markdown("""<style>
@import url('https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;600;700&family=IBM+Plex+Sans:wght@400;500;600&family=JetBrains+Mono:wght@500;600;700&display=swap');
html,body,.stApp{background-color:#060912;color:#c7d2e0;font-family:'IBM Plex Sans',sans-serif;}
.stApp{background-image:radial-gradient(900px 460px at 6% -8%, rgba(45,212,191,0.10), transparent 60%),radial-gradient(820px 440px at 100% 108%, rgba(56,132,255,0.09), transparent 60%),radial-gradient(rgba(120,150,190,0.05) 1px, transparent 1px);background-size:auto,auto,22px 22px;background-attachment:fixed;}
[data-testid="stMainBlockContainer"]{max-width:1480px;padding-top:1.2rem;}
[data-testid="stSidebar"]{background-color:#0a0f1c;border-right:1px solid #1b2740;}
#MainMenu,footer{visibility:hidden;}
.br-head{display:flex;align-items:flex-end;justify-content:space-between;padding:4px 2px 14px 2px;position:relative;}
.br-head::after{content:"";position:absolute;left:0;right:0;bottom:0;height:2px;background:linear-gradient(90deg,#2dd4bf,#3884ff 45%,transparent 92%);background-size:220% 100%;animation:br-scan 6s linear infinite;border-radius:2px;}
@keyframes br-scan{0%{background-position:120% 0;}100%{background-position:-120% 0;}}
.br-logo{font-family:'Space Grotesk',sans-serif;font-weight:700;font-size:1.32rem;letter-spacing:.16em;color:#eef3fb;text-transform:uppercase;}
.br-logo .dot{color:#2dd4bf;}
.br-eyebrow{font-family:'Space Grotesk',sans-serif;font-size:.58rem;font-weight:600;letter-spacing:.22em;color:#4f6080;text-transform:uppercase;margin-top:5px;}
.br-status{font-family:'JetBrains Mono',monospace;font-size:.7rem;text-align:right;}
.br-dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:7px;vertical-align:middle;}
.br-on{background:#2dd4bf;box-shadow:0 0 8px rgba(45,212,191,.7);} .br-off{background:#5b6b85;}
.br-kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin:6px 0 18px 0;}
@media(max-width:900px){.br-kpis{grid-template-columns:1fr 1fr;}}
.br-kpi{position:relative;background:linear-gradient(150deg,#0c1426,#0e1830);border:1px solid #1d2c49;border-radius:11px;padding:15px 17px;overflow:hidden;transition:transform .16s ease,border-color .16s ease,box-shadow .16s ease;}
.br-kpi:hover{transform:translateY(-3px);border-color:#33507e;box-shadow:0 10px 26px rgba(0,0,0,.4);}
.br-kpi::before{content:"";position:absolute;top:0;left:0;right:0;height:3px;background:var(--ac,#33507e);}
.br-kpi .l{font-family:'Space Grotesk',sans-serif;font-size:.6rem;font-weight:600;letter-spacing:.15em;text-transform:uppercase;color:#6b7c97;}
.br-kpi .v{font-family:'JetBrains Mono',monospace;font-weight:700;font-size:1.5rem;margin-top:8px;color:var(--vc,#eef3fb);}
.br-kpi .s{font-family:'JetBrains Mono',monospace;font-size:.66rem;color:#6b7c97;margin-top:6px;}
.br-panel{background:linear-gradient(160deg,#0c1426,#0b1222);border:1px solid #1d2c49;border-radius:12px;padding:15px 17px;margin:12px 0;}
.br-h{font-family:'Space Grotesk',sans-serif;font-size:.64rem;font-weight:700;letter-spacing:.16em;text-transform:uppercase;color:#8294b0;margin-bottom:10px;}
.br-bubble{background:#0e1830;border:1px solid #233452;border-radius:12px;padding:12px 14px;margin:10px 0;font-size:.86rem;line-height:1.6;}
.br-bubble.user{background:rgba(56,132,255,.08);border-color:rgba(56,132,255,.3);}
.br-bubble .who{font-family:'Space Grotesk',sans-serif;font-size:.58rem;font-weight:700;letter-spacing:.14em;text-transform:uppercase;color:#6b7c97;margin-bottom:6px;}
.br-lesson{background:#0e1830;border:1px solid #233452;border-left:3px solid #2dd4bf;border-radius:10px;padding:10px 12px;margin:8px 0;font-size:.82rem;line-height:1.5;}
.br-lesson .meta{font-family:'JetBrains Mono',monospace;font-size:.62rem;color:#6b7c97;margin-bottom:4px;}
.br-tag{display:inline-block;font-family:'JetBrains Mono',monospace;font-size:.58rem;font-weight:700;padding:1px 7px;border-radius:6px;background:rgba(45,212,191,.12);color:#5eead4;border:1px solid rgba(45,212,191,.3);margin-right:5px;}
.br-note{font-size:.78rem;line-height:1.6;color:#9fb0c9;}
.br-warn{background:rgba(251,191,36,.08);border:1px solid rgba(251,191,36,.3);border-radius:10px;padding:11px 13px;font-size:.8rem;color:#fde68a;margin:10px 0;}
.pos{color:#4ade80;} .neg{color:#fb7185;} .mut{color:#6b7c97;}
[data-testid="stDataFrame"]{border:0;}
[data-testid="stButton"] button{border-radius:8px;font-family:'Space Grotesk',sans-serif;font-weight:600;letter-spacing:.04em;}
</style>""", unsafe_allow_html=True)


def _glitch(where, exc):
    st.markdown(f'<div class="br-warn">⚠ {where} hit a snag — your data is intact, this is a render edge case.</div>',
                unsafe_allow_html=True)
    with st.expander("Technical details"):
        st.exception(exc)


def _kpi(label, value, sub, ac, vc):
    return (f'<div class="br-kpi" style="--ac:{ac};--vc:{vc};"><div class="l">{label}</div>'
            f'<div class="v">{value}</div><div class="s">{sub}</div></div>')


# ---- load journal + compute post-mortem (always, no network) --------------- #
try:
    rows = get_journal().read_archive_merged()
except Exception as _e:
    rows = []
    _glitch("journal read", _e)

try:
    pm = B.compute_postmortem(rows)
except Exception as _e:
    pm = {"summary": {"reviews": 0, "taken": 0, "closed": 0, "wins": 0, "losses": 0,
                      "win_rate": 0.0, "net_pnl": 0.0, "profit_factor": 0.0,
                      "expectancy": 0.0, "lessons": 0},
          "avoidable_losses": [], "fragile_wins": [], "gatekeeper_factors": [],
          "by_symbol": {}, "by_hour": {}, "by_regime": {}, "by_duration": {}}
    _glitch("post-mortem", _e)

client = B.BrainClient()
s = pm["summary"]

# ---- header ---------------------------------------------------------------- #
st.markdown(
    f'<div class="br-head"><div><div class="br-logo">Trading<span class="dot">·</span>Brain</div>'
    f'<div class="br-eyebrow">Grounded analyst over your live journal — proposes, never auto-applies</div></div>'
    f'<div class="br-status"><span class="br-dot {"br-on" if client.configured else "br-off"}"></span>'
    f'{client.status()}</div></div>',
    unsafe_allow_html=True,
)

# ---- KPI strip (works offline) -------------------------------------------- #
net = s["net_pnl"]
net_ac = "#10b981" if net > 0 else "#f43f5e" if net < 0 else "#33507e"
net_vc = "#34d399" if net > 0 else "#fb7185" if net < 0 else "#eef3fb"
wr_ac = "#10b981" if s["win_rate"] >= 55 else "#f43f5e" if s["closed"] and s["win_rate"] < 45 else "#3884ff"
st.markdown(
    '<div class="br-kpis">'
    + _kpi("Reviews / taken", f'{s["reviews"]} / {s["taken"]}', f'{s["closed"]} closed', "#2dd4bf", "#eef3fb")
    + _kpi("Net P&L", f"{net:+.2f}", f'win rate {s["win_rate"]:.1f}%', net_ac, net_vc)
    + _kpi("Avoidable losses", str(len(pm["avoidable_losses"])), "were in profit, then reversed", "#fbbf24", "#fde68a")
    + _kpi("Lessons in memory", str(s["lessons"]), "the trainable corpus", "#a855f7", "#eef3fb")
    + '</div>',
    unsafe_allow_html=True,
)

if "brain_proposal" not in st.session_state:
    st.session_state.brain_proposal = None
if "brain_chat" not in st.session_state:
    st.session_state.brain_chat = []

tab_f, tab_c, tab_l, tab_b = st.tabs(["Findings", "Chat", "Lessons", "Backtest"])

# ============================ FINDINGS ===================================== #
with tab_f:
    st.markdown('<div class="br-panel"><div class="br-h">What the brain sees in your trades</div>'
                '<div class="br-note">The numeric lenses below are computed locally and are always correct. '
                'Press the button to let the brain translate them into a plain-language read, cross-referenced '
                'against your saved lessons. If the evidence is strong it may emit one preset proposal, which is '
                'captured for the Backtest tab — it is never applied automatically.</div></div>',
                unsafe_allow_html=True)

    gk = pm["gatekeeper_factors"]
    c1, c2 = st.columns(2)
    with c1:
        st.markdown('<div class="br-panel"><div class="br-h">Avoidable losses — exit / duration problem</div>', unsafe_allow_html=True)
        if pm["avoidable_losses"]:
            st.dataframe(pd.DataFrame(pm["avoidable_losses"]), use_container_width=True, hide_index=True)
        else:
            st.caption("None — your losers were not handed back from profit. Good.")
        st.markdown('</div>', unsafe_allow_html=True)
    with c2:
        st.markdown('<div class="br-panel"><div class="br-h">Fragile wins — entry timing</div>', unsafe_allow_html=True)
        if pm["fragile_wins"]:
            st.dataframe(pd.DataFrame(pm["fragile_wins"]), use_container_width=True, hide_index=True)
        else:
            st.caption("None — your wins are arriving cleanly.")
        st.markdown('</div>', unsafe_allow_html=True)

    st.markdown('<div class="br-panel"><div class="br-h">Gatekeepers — the factor blocking your near-miss trends</div>'
                + ('<div class="br-note">' + ", ".join(f"<b>{k}</b> ({v})" for k, v in gk[:6]) + '</div>'
                   if gk else '<div class="br-note">No near-miss trending stand-asides yet.</div>')
                + '</div>', unsafe_allow_html=True)

    if client.configured:
        if st.button("Ask the brain to read my trades", type="primary", use_container_width=True):
            with st.spinner("The brain is reading your journal and lessons…"):
                try:
                    q = ("Analyse my post-mortem and lessons. In plain language: (1) what is costing me most — "
                         "avoidable losses, fragile wins, or a mis-calibrated gate; (2) which symbols/hours/regimes "
                         "to favour or avoid; (3) whether any single change is justified. If and only if the evidence "
                         "is strong, end with exactly one ```json preset proposal. Otherwise state that no change is "
                         "warranted yet and what data to collect.")
                    reply = client.chat(B.build_messages(q, pm, rows))
                    st.session_state.brain_chat.append({"role": "assistant", "content": reply})
                    prop = B.find_proposal(reply)
                    if prop:
                        st.session_state.brain_proposal = prop
                except B.BrainError as exc:
                    st.error(f"Brain error: {exc}")
                    reply = None
            if reply:
                st.markdown(f'<div class="br-bubble"><div class="who">brain</div>{st.markdown(reply) or ""}</div>',
                            unsafe_allow_html=True)
                st.markdown(reply)
                if st.session_state.brain_proposal:
                    st.success("The brain emitted a preset proposal — it is loaded in the Backtest tab for validation.")
    else:
        st.markdown('<div class="br-warn">The conversational brain is not configured yet, so the plain-language read '
                    'is unavailable — but every numeric lens above and the Backtest tab work fully offline. '
                    'See BRAIN_SETUP.md to connect the agent.</div>', unsafe_allow_html=True)

# ============================ CHAT ========================================= #
with tab_c:
    st.markdown('<div class="br-panel"><div class="br-h">Grounded chat</div>'
                '<div class="br-note">Every message is sent with the rulebook + your live post-mortem + your most '
                'relevant lessons + recent reviews, so the brain answers about <i>your</i> bot and <i>your</i> history.</div></div>',
                unsafe_allow_html=True)
    for m in st.session_state.brain_chat:
        cls = "br-bubble user" if m["role"] == "user" else "br-bubble"
        who = "you" if m["role"] == "user" else "brain"
        st.markdown(f'<div class="{cls}"><div class="who">{who}</div>{m["content"]}</div>', unsafe_allow_html=True)

    if client.configured:
        with st.form("brain_chat_form", clear_on_submit=True):
            q = st.text_area("Ask the brain", placeholder="e.g. why do I keep losing on XAUUSD between 14:00 and 15:00 UTC?", height=90)
            sent = st.form_submit_button("Send", use_container_width=True)
        if sent and q.strip():
            st.session_state.brain_chat.append({"role": "user", "content": q.strip()})
            with st.spinner("thinking…"):
                try:
                    reply = client.chat(B.build_messages(q.strip(), pm, rows))
                    st.session_state.brain_chat.append({"role": "assistant", "content": reply})
                    prop = B.find_proposal(reply)
                    if prop:
                        st.session_state.brain_proposal = prop
                except B.BrainError as exc:
                    st.session_state.brain_chat.append({"role": "assistant", "content": f"[brain error] {exc}"})
            st.rerun()
        with st.expander("Connection test / advanced"):
            if st.button("Ping the brain"):
                st.code(client.ping())
    else:
        st.markdown('<div class="br-warn">Chat needs the agent. Set AGENT_UUID + DO_API_TOKEN (or a pinned '
                    'AGENT_ENDPOINT + AGENT_API_KEY) per BRAIN_SETUP.md. Lessons and Backtest work without it.</div>',
                    unsafe_allow_html=True)

# ============================ LESSONS ====================================== #
with tab_l:
    st.markdown('<div class="br-panel"><div class="br-h">Train the memory</div>'
                '<div class="br-note">Save what you notice. Each lesson is retrieved into future answers, so the brain '
                'compounds your wisdom. Tag with a symbol or regime (e.g. XAUUSD, SHORT) to make retrieval sharper. '
                'This is the safe form of "training": the corpus grows; the live strategy never moves from a chat.</div></div>',
                unsafe_allow_html=True)
    with st.form("add_lesson_form", clear_on_submit=True):
        txt = st.text_area("Observation", placeholder="XAUUSD chops hard 14:00-15:00 UTC on low volume — my losses there were duration, not entry.", height=90)
        tags = st.text_input("Tags (comma separated)", placeholder="XAUUSD, SHORT, duration")
        saved = st.form_submit_button("Save lesson", use_container_width=True)
    if saved:
        try:
            tag_list = [t.strip() for t in (tags or "").split(",") if t.strip()]
            B.add_lesson(txt, tag_list, source="user", confirmed=True)
            st.success("Lesson saved — it will shape the next brain answer.")
            st.rerun()
        except Exception as exc:
            st.error(f"Could not save: {exc}")

    lessons = B.load_lessons()
    lc1, lc2, lc3 = st.columns(3)
    with lc1:
        st.download_button("Export lessons (.jsonl)", B.lessons_bytes(), file_name="brain_lessons.jsonl",
                           mime="application/jsonl", use_container_width=True)
    with lc2:
        kb_doc = ("# Trading Brain — lessons corpus\n# Drop this file into the DigitalOcean knowledge base so the "
                  "managed agent retrieves your wisdom server-side too.\n\n"
                  + "\n\n".join(f"- [{l.get('ts_utc','')[:10]}|{','.join(l.get('tags',[])) or '-'}] {l.get('text','')}"
                                for l in lessons))
        st.download_button("Export as KB document (.md)", kb_doc.encode("utf-8"), file_name="brain_lessons_kb.md",
                           mime="text/markdown", use_container_width=True)
    with lc3:
        up = st.file_uploader("Import lessons (.jsonl)", type=["jsonl", "json", "txt"], label_visibility="collapsed")
        if up is not None:
            stats = B.import_lessons(up.read())
            st.success(f'Imported {stats["added"]} · skipped {stats["skipped"]} · errors {stats["errors"]}.')
            st.rerun()

    if lessons:
        st.markdown('<div class="br-h" style="margin-top:14px;">Stored lessons (most recent first)</div>', unsafe_allow_html=True)
        for l in reversed(lessons):
            tag_html = "".join(f'<span class="br-tag">{t}</span>' for t in l.get("tags", []))
            st.markdown(f'<div class="br-lesson"><div class="meta">{l.get("ts_utc","")} · {l.get("source","user")} '
                        f'· {"confirmed" if l.get("confirmed", True) else "draft"} {tag_html}</div>{l.get("text","")}</div>',
                        unsafe_allow_html=True)
    else:
        st.caption("No lessons yet. Add your first observation above.")

# ============================ BACKTEST ===================================== #
with tab_b:
    st.markdown('<div class="br-panel"><div class="br-h">Gate backtest — validate before you opt in</div>'
                '<div class="br-note">Replays your recorded reviews through re-weighted factors + a threshold. '
                '<b>kept_pnl</b> is real (trades that still fire). <b>dropped_pnl</b> is real too — the P&L this '
                'variant would have skipped: negative = it avoids losers (good); positive = it cuts winners (bad). '
                '<b>added_unknown</b> are trades it would newly fire — outcome genuinely unknown, needs a forward '
                'test on demo. Approximate: re-weighting uses the recorded integer factor scores.</div></div>',
                unsafe_allow_html=True)

    preset_opts = list(B.PRESETS.keys())
    if st.session_state.brain_proposal:
        preset_opts = ["🧠 brain proposal"] + preset_opts
    pick = st.selectbox("Preset", preset_opts, key="bt_preset")

    if pick == "🧠 brain proposal":
        prop = st.session_state.brain_proposal or {}
        weights = prop.get("weights", B.DEFAULT_WEIGHTS)
        threshold = int(prop.get("threshold", 20))
        st.info(f'Brain proposal "{prop.get("name","")}" — {prop.get("rationale","")}')
    else:
        weights = B.PRESETS[pick]
        threshold = st.select_slider("Threshold", options=B.THRESHOLD_OPTIONS, value=20, key="bt_thr")

    if not rows:
        st.info("No journal rows yet — nothing to replay. Run the terminal and let reviews accumulate.")
    else:
        base = B.baseline(rows)
        res = B.backtest(rows, weights, threshold)
        df = pd.DataFrame([base, res])
        st.dataframe(df, use_container_width=True, hide_index=True)

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Kept win rate", f'{res["kept_win_rate"]:.1f}%', f'{base["kept_win_rate"]:.1f}% as-built')
        m2.metric("Dropped P&L", f'{res["dropped_pnl"]:+.2f}', f'{res["dropped_losses_avoided"]} losers avoided')
        m3.metric("Wins lost", str(res["dropped_wins_lost"]), "trades it would cut")
        m4.metric("Newly fired", str(res["added_unknown"]), "unknown — forward-test")

        verdict = ""
        if res["dropped_pnl"] < 0 and res["dropped_wins_lost"] <= max(1, res["dropped_losses_avoided"] // 2):
            verdict = "Looks promising: it skips more losing money than winning money. Forward-test on demo, then opt in."
        elif res["dropped_pnl"] > 0:
            verdict = "Caution: this variant would have skipped net winners. Probably not an improvement."
        else:
            verdict = "Marginal: little real money moved. Collect more data before deciding."
        st.markdown(f'<div class="br-note"><b>Read:</b> {verdict}</div>', unsafe_allow_html=True)

        ptxt = B.preset_text(pick if pick != "🧠 brain proposal" else (st.session_state.brain_proposal or {}).get("name", "brain-proposal"),
                             weights, threshold,
                             "" if pick != "🧠 brain proposal" else (st.session_state.brain_proposal or {}).get("rationale", ""))
        st.download_button("Download preset (.txt)", ptxt.encode("utf-8"),
                           file_name=f"strategy_preset_{pick.replace(' ','_')}.txt", mime="text/plain",
                           use_container_width=True)
        st.caption("Opt-in path: add the downloaded preset to config.STRATEGY_SENSITIVITY_PRESETS (or map it onto a "
                   "preset name), then select it in the terminal sidebar. The bot never applies this on its own.")

st.markdown('<div class="br-note" style="margin-top:18px;text-align:center;">Observe → ask the brain → brain proposes → '
            'backtest → opt in. The live strategy is untouched by everything on this page.</div>', unsafe_allow_html=True)
try:
    st.page_link("dashboard.py", label="← Back to Terminal", use_container_width=True)
except Exception:
    pass
