"""src/brain_llm.py — free-first LLM failover chain with model auto-resolution
and self-diagnosing errors.

Priority order: Groq -> OpenRouter (free) -> Cerebras -> OpenAI. All four speak
the OpenAI-compatible /v1/chat/completions protocol, so there is ONE code path;
providers differ only by base URL, key env var, and default model.

Robustness:
  * chat / stream try every CONFIGURED provider in priority order and only fail
    when all of them fail (429 / stale-model 404 / 403 host-block / 402 quota /
    5xx / network all roll over).
  * MODEL AUTO-RESOLUTION: if a provider rejects the configured/default model,
    we query its /v1/models once, pick a live chat model, cache it, and retry.
  * SELF-DIAGNOSING ERRORS: known HTTP/provider codes are translated to plain
    English, and when the chain fails with only ONE provider configured the
    message tells you exactly which Secrets lines to uncomment (Groq is often
    blocked from Streamlit Cloud with 403/1010; a second provider on different
    infra is the fix).
  * Every attempt is recorded in a chain trace the UI can show verbatim.

Stdlib-only HTTP (urllib), like src/api_client.py -> no new pip dependency.
This module is an ADVISOR only: it never places a trade and never mutates the
live strategy.
"""
from __future__ import annotations

import json
import os
import ssl
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Iterator, List, Optional, Tuple

try:
    from src.logger import get_logger
    logger = get_logger("brain_llm")
except Exception:  # pragma: no cover
    import logging
    logger = logging.getLogger("brain_llm")


class BrainLLMError(Exception):
    pass


# --------------------------------------------------------------------------- #
#  Provider specs — priority order is the failover order.                     #
# --------------------------------------------------------------------------- #
PROVIDERS: List[Dict[str, Any]] = [
    {
        "id": "groq", "label": "Groq", "base": "https://api.groq.com/openai",
        "key_env": "GROQ_API_KEY", "model_env": "GROQ_MODEL",
        "default_model": "llama-3.3-70b-versatile",
        "signup": "https://console.groq.com/keys",
        "free_note": "Free tier, very fast. If you see 403/1010 it is Groq blocking the host IP/key (not fixable from code) — add OpenRouter as a backstop.",
    },
    {
        "id": "openrouter", "label": "OpenRouter", "base": "https://openrouter.ai/api",
        "key_env": "OPENROUTER_API_KEY", "model_env": "OPENROUTER_MODEL",
        "default_model": "google/gemini-2.5-flash:free",
        "signup": "https://openrouter.ai/keys",
        "free_note": "Free ':free' models on different infra than Groq — the ideal backstop. If the default 404s, set OPENROUTER_MODEL to a current ':free' model (see openrouter.ai/models?order=pricing-low-to-high).",
    },
    {
        "id": "cerebras", "label": "Cerebras", "base": "https://api.cerebras.ai",
        "key_env": "CEREBRAS_API_KEY", "model_env": "CEREBRAS_MODEL",
        "default_model": "llama-3.3-70b",
        "signup": "https://cloud.cerebras.ai/ (API keys)",
        "free_note": "Fast inference, free credits/tier. Alternatives: qwen-3-32b, llama-4-scout-17b-16e.",
    },
    {
        "id": "openai", "label": "OpenAI", "base": "https://api.openai.com",
        "key_env": "OPENAI_API_KEY", "model_env": "OPENAI_MODEL",
        "default_model": "gpt-4o-mini",
        "signup": "https://platform.openai.com/api-keys",
        "free_note": "Paid — last-resort fallback so the chain almost always has a live backstop.",
    },
]
_BY_ID: Dict[str, Dict[str, Any]] = {p["id"]: p for p in PROVIDERS}

_trace_lock = threading.Lock()
_last_trace: List[Dict[str, str]] = []
_resolved: Dict[str, str] = {}
_resolved_lock = threading.Lock()


def _secret(name: str) -> str:
    val = os.getenv(name)
    if val and str(val).strip():
        return str(val).strip()
    try:
        import streamlit as st
        v = st.secrets.get(name, "")
        return str(v).strip() if v else ""
    except Exception:
        return ""


def _temp() -> float:
    try:
        return float(_secret("BRAIN_TEMPERATURE") or 0.2)
    except Exception:
        return 0.2


def _maxtok(default: int = 1100) -> int:
    try:
        return int(_secret("BRAIN_MAX_TOKENS") or default)
    except Exception:
        return default


def _model_for(provider_id: str) -> str:
    spec = _BY_ID[provider_id]
    with _resolved_lock:
        cached = _resolved.get(provider_id)
    if cached:
        return cached
    return _secret(spec["model_env"]) or spec["default_model"]


def configured_providers() -> List[str]:
    return [p["id"] for p in PROVIDERS if _secret(p["key_env"])]


def detect_provider() -> Optional[str]:
    want = (_secret("BRAIN_PROVIDER") or "auto").strip().lower()
    cfg = configured_providers()
    if want != "auto":
        if want in cfg:
            return want
        spec = _BY_ID.get(want)
        return want if (spec and _secret(spec["key_env"])) else None
    return cfg[0] if cfg else None


def active_provider_id() -> Optional[str]:
    return detect_provider()


def provider_nodes() -> List[Dict[str, Any]]:
    active = active_provider_id()
    cfg = set(configured_providers())
    out = []
    for p in PROVIDERS:
        out.append({
            "id": p["id"], "label": p["label"], "signup": p["signup"],
            "free_note": p["free_note"], "default_model": p["default_model"],
            "model": _model_for(p["id"]),
            "configured": p["id"] in cfg, "active": p["id"] == active,
        })
    return out


def chain_trace() -> List[Dict[str, str]]:
    with _trace_lock:
        return list(_last_trace)


def _set_trace(t: List[Dict[str, str]]) -> None:
    global _last_trace
    with _trace_lock:
        _last_trace = t


# --------------------------------------------------------------------------- #
#  human-readable error translation                                           #
# --------------------------------------------------------------------------- #
def _humanize(status: int, body: str) -> str:
    low = (body or "").lower()
    if status == 401:
        return "invalid or missing API key — re-check the secret in Streamlit Secrets / env"
    if status == 402:
        return ("payment/quota required on this key — for OpenRouter set OPENROUTER_MODEL to a ':free' "
                "model, or rely on the next provider in the chain")
    if status == 403:
        if "1010" in (body or "") or any(t in low for t in ("cloudflare", "challenge", "blocked", "attention", "ray id", "sorry, you have been blocked")):
            return ("provider blocked this host's request (free tiers often sit behind a "
                    "Cloudflare / IP block, code 1010). This cannot be fixed from code — add a "
                    "second provider on different infra (OpenRouter / Cerebras) as a backstop.")
        return "access forbidden — the key may be suspended, out of credits, or region-blocked"
    if status == 404:
        return ("model not found and auto-resolve found no live alternative — set the *_MODEL "
                "env var to a current model name for that provider")
    if status == 429:
        return "rate limit / quota exhausted on this key — the chain tries the next provider"
    if 500 <= status < 600:
        return "provider server error (transient) — the chain tries the next provider"
    txt = (body or "").strip()
    return txt[:160] if txt else f"HTTP {status}"


# --------------------------------------------------------------------------- #
#  low-level HTTP                                                             #
# --------------------------------------------------------------------------- #
def _ctx():
    return ssl.create_default_context()


def _headers_for(provider_id: str) -> Dict[str, str]:
    spec = _BY_ID[provider_id]
    h = {"Content-Type": "application/json"}
    key = _secret(spec["key_env"])
    if key:
        h["Authorization"] = f"Bearer {key}"
    if provider_id == "openrouter":
        site = _secret("OPENROUTER_SITE")
        name = _secret("OPENROUTER_NAME") or "MomentumMaster Brain"
        if site:
            h["HTTP-Referer"] = site
        h["X-Title"] = name
    return h


def _post_json(url: str, headers: Dict[str, str], body: Any, timeout: float) -> Tuple[int, str]:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ctx()) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except urllib.error.URLError as exc:
        raise BrainLLMError(f"network error reaching {url}: {exc.reason}") from exc
    except Exception as exc:
        raise BrainLLMError(f"request failed: {exc}") from exc


def _get_json(url: str, headers: Dict[str, str], timeout: float) -> Tuple[int, str]:
    req = urllib.request.Request(url, headers={k: v for k, v in headers.items() if k != "Content-Type"}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ctx()) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except Exception as exc:
        raise BrainLLMError(f"models lookup failed: {exc}") from exc


def _open_stream(url: str, headers: Dict[str, str], body: Any, timeout: float):
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    return urllib.request.urlopen(req, timeout=timeout, context=_ctx())  # caller closes


def _iter_sse(resp) -> Iterator[dict]:
    for raw in resp:
        line = raw.decode("utf-8", "replace").strip()
        if not line or not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            return
        try:
            yield json.loads(payload)
        except Exception:
            continue


# --------------------------------------------------------------------------- #
#  error classification + model auto-resolution                               #
# --------------------------------------------------------------------------- #
def _is_model_error(status: int, body: str) -> bool:
    if status not in (400, 404):
        return False
    low = body.lower()
    return ("model" in low) and any(k in low for k in ("not found", "does not exist", "invalid", "unsupported", "no model", "not exist"))


def _resolve_model(provider_id: str) -> Optional[str]:
    spec = _BY_ID[provider_id]
    url = f"{spec['base']}/v1/models"
    try:
        status, raw = _get_json(url, _headers_for(provider_id), timeout=20.0)
        if not (200 <= status < 300):
            return None
        data = json.loads(raw)
        ids = [m.get("id") for m in (data.get("data") or []) if isinstance(m, dict) and m.get("id")]
    except Exception:
        return None
    if not ids:
        return None
    hints = ("instruct", "chat", "mini", "flash", "instant", "versatile", "maverick", "scout", "oss")
    ranked = sorted(ids, key=lambda s: (0 if any(h in s.lower() for h in hints) else 1, len(s)))
    ranked = [s for s in ranked if not any(b in s.lower() for b in ("embed", "moderation", "image", "tts", "whisper", "dall"))] or ranked
    pick = ranked[0]
    with _resolved_lock:
        _resolved[provider_id] = pick
    logger.info("Auto-resolved %s model -> %s", provider_id, pick)
    return pick


def _extract_text(obj: dict) -> str:
    try:
        return obj["choices"][0]["message"]["content"] or ""
    except Exception:
        return ""


def _extract_delta(obj: dict) -> str:
    try:
        return obj["choices"][0].get("delta", {}).get("content", "") or ""
    except Exception:
        return ""


# --------------------------------------------------------------------------- #
#  single-provider attempts                                                   #
# --------------------------------------------------------------------------- #
def _chat_one(provider_id: str, messages: List[Dict[str, str]], temperature: float, max_tokens: int) -> str:
    spec = _BY_ID[provider_id]
    url = f"{spec['base']}/v1/chat/completions"
    model = _model_for(provider_id)
    body = {"model": model, "messages": messages, "temperature": temperature, "max_tokens": int(max_tokens), "stream": False}
    status, raw = _post_json(url, _headers_for(provider_id), body, timeout=120.0)
    if 200 <= status < 300:
        txt = _extract_text(json.loads(raw)).strip()
        if txt:
            return txt
        raise BrainLLMError(f"{spec['label']}: empty response")
    if _is_model_error(status, raw):
        new = _resolve_model(provider_id)
        if new and new != model:
            body["model"] = new
            status, raw = _post_json(url, _headers_for(provider_id), body, timeout=120.0)
            if 200 <= status < 300:
                txt = _extract_text(json.loads(raw)).strip()
                if txt:
                    return txt
    raise BrainLLMError(f"{spec['label']} ({status}): {_humanize(status, raw)}")


def _stream_one(provider_id: str, messages: List[Dict[str, str]], temperature: float, max_tokens: int) -> Iterator[str]:
    spec = _BY_ID[provider_id]
    url = f"{spec['base']}/v1/chat/completions"
    model = _model_for(provider_id)
    body = {"model": model, "messages": messages, "temperature": temperature, "max_tokens": int(max_tokens), "stream": True}
    resp = _open_stream(url, _headers_for(provider_id), body, timeout=120.0)
    yielded = False
    try:
        for obj in _iter_sse(resp):
            d = _extract_delta(obj)
            if d:
                yielded = True
                yield d
    finally:
        try:
            resp.close()
        except Exception:
            pass
    if not yielded:
        raise BrainLLMError(f"{spec['label']}: stream produced no tokens")


# --------------------------------------------------------------------------- #
#  public API — chain-aware                                                   #
# --------------------------------------------------------------------------- #
def _trace_add(trace: List[Dict[str, str]], provider_id: str, status: str, detail: str = "") -> None:
    trace.append({"provider": _BY_ID[provider_id]["label"], "status": status, "detail": detail})


def chat(messages: List[Dict[str, str]], provider: Optional[str] = None,
         temperature: Optional[float] = None, max_tokens: Optional[int] = None) -> str:
    temp = _temp() if temperature is None else float(temperature)
    mtok = _maxtok() if max_tokens is None else int(max_tokens)
    order = [provider] if (provider and _secret(_BY_ID.get(provider, {}).get("key_env", "__none__"))) else configured_providers()
    if not order:
        raise BrainLLMError("No LLM provider configured. See BRAIN_SETUP.md (all options have a free tier).")
    trace: List[Dict[str, str]] = []
    last: Optional[Exception] = None
    for pid in order:
        try:
            result = _chat_one(pid, messages, temp, mtok)
            _trace_add(trace, pid, "ok")
            _set_trace(trace)
            return result
        except BrainLLMError as exc:
            last = exc
            _trace_add(trace, pid, "failed", str(exc)[:160])
            logger.warning("chain: %s failed -> %s", pid, exc)
            continue
    _set_trace(trace)
    detail = " | ".join(f"{t['provider']}: {t['detail']}" for t in trace)
    msg = "All providers failed: " + detail
    if len(order) == 1:
        msg += (" — NOTE: only 1 provider is configured, so there was no backstop to fail over to. "
                "Uncomment OPENROUTER_API_KEY and CEREBRAS_API_KEY in Streamlit Secrets (remove the leading #). "
                "Groq is frequently blocked from Streamlit Cloud with 403/1010 (a Cloudflare/IP challenge); "
                "a second provider on different infrastructure is what makes the brain work here.")
    raise BrainLLMError(msg) from last


chat_with_chain = chat


def stream_chat(messages: List[Dict[str, str]], provider: Optional[str] = None,
                temperature: Optional[float] = None, max_tokens: Optional[int] = None) -> Iterator[str]:
    temp = _temp() if temperature is None else float(temperature)
    mtok = _maxtok() if max_tokens is None else int(max_tokens)
    pid = provider or active_provider_id()
    if pid and _secret(_BY_ID.get(pid, {}).get("key_env", "__none__")):
        try:
            yield from _stream_one(pid, messages, temp, mtok)
            _set_trace([{"provider": _BY_ID[pid]["label"], "status": "ok", "detail": "stream"}])
            return
        except Exception as exc:
            logger.warning("stream on %s failed, falling back to chain: %s", pid, exc)
            _set_trace([{"provider": _BY_ID[pid]["label"], "status": "stream-failed", "detail": str(exc)[:160]}])
    text = chat(messages, temperature=temp, max_tokens=mtok)
    if text:
        yield text


def test_provider(provider_id: str) -> Tuple[bool, float, str]:
    spec = _BY_ID.get(provider_id)
    if not spec:
        return False, 0.0, f"unknown provider '{provider_id}'"
    if not _secret(spec["key_env"]):
        return False, 0.0, f"{spec['key_env']} not set"
    t0 = time.time()
    try:
        reply = _chat_one(provider_id, [{"role": "user", "content": "Reply with the single word: pong"}], 0.0, 16)
        dt = (time.time() - t0) * 1000.0
        return True, dt, f"{spec['label']} · {_model_for(provider_id)} · {dt:.0f}ms · “{reply[:24]}”"
    except BrainLLMError as exc:
        return False, (time.time() - t0) * 1000.0, f"{spec['label']}: {exc}"


def test_chain() -> List[Dict[str, Any]]:
    out = []
    for pid in configured_providers():
        ok, dt, msg = test_provider(pid)
        out.append({"id": pid, "label": _BY_ID[pid]["label"], "ok": ok, "ms": round(dt), "msg": msg})
    return out
