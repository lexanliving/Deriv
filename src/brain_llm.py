"""src/brain_llm.py — free LLM failover chain (Groq -> OpenRouter -> Cerebras -> OpenAI)."""
from __future__ import annotations
import json, os, re, ssl, urllib.error, urllib.request
from typing import Any, Dict, List, Optional
from src.logger import get_logger
logger = get_logger("brain_llm")

_BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36")
_BROWSER_HEADERS = {"User-Agent": _BROWSER_UA,
                    "Accept": "application/json, text/event-stream, */*",
                    "Accept-Language": "en-US,en;q=0.9", "Connection": "keep-alive"}

PROVIDERS = [
    {"id": "groq", "label": "Groq", "base": "https://api.groq.com/openai",
     "key_env": "GROQ_API_KEY", "model_env": "GROQ_MODEL",
     "default_model": "llama-3.3-70b-versatile", "signup": "https://console.groq.com/keys",
     "free_note": "Free tier, very fast."},
    {"id": "openrouter", "label": "OpenRouter", "base": "https://openrouter.ai/api",
     "key_env": "OPENROUTER_API_KEY", "model_env": "OPENROUTER_MODEL", "default_model": "",
     "signup": "https://openrouter.ai/keys",
     "free_note": "Free ':free' models; leave model unset to auto-pick."},
    {"id": "cerebras", "label": "Cerebras", "base": "https://api.cerebras.ai",
     "key_env": "CEREBRAS_API_KEY", "model_env": "CEREBRAS_MODEL",
     "default_model": "llama-3.3-70b", "signup": "https://cloud.cerebras.ai/",
     "free_note": "Fast inference, free credits."},
    {"id": "openai", "label": "OpenAI", "base": "https://api.openai.com",
     "key_env": "OPENAI_API_KEY", "model_env": "OPENAI_MODEL",
     "default_model": "gpt-4o-mini", "signup": "https://platform.openai.com/api-keys",
     "free_note": "Paid backstop."},
]
_BY_ID = {p["id"]: p for p in PROVIDERS}
_OPENROUTER_FALLBACKS = ["deepseek/deepseek-chat-v3-0324:free",
                         "meta-llama/llama-4-maverick:free",
                         "google/gemma-3-27b-it:free",
                         "qwen/qwen3-235b-a22b:free"]

class BrainLLMError(Exception):
    pass

_trace: List[Dict[str, str]] = []
_resolved: Dict[str, str] = {}

def _secret(name: str, default: str = "") -> str:
    val = (os.getenv(name) or "").strip()
    if val:
        return val
    try:
        import streamlit as st
        v = st.secrets.get(name, "")
        return str(v).strip() if v else default
    except Exception:
        return default

def configured_providers() -> List[str]:
    return [p["id"] for p in PROVIDERS if _secret(p["key_env"])]

def active_provider_id() -> Optional[str]:
    want = (_secret("BRAIN_PROVIDER") or "auto").strip().lower()
    cfg = configured_providers()
    if want != "auto":
        return want if want in cfg else None
    return cfg[0] if cfg else None

def _model_for(pid: str) -> str:
    spec = _BY_ID[pid]
    if _resolved.get(pid):
        return _resolved[pid]
    env = _secret(spec["model_env"])
    if env:
        return env
    if pid == "openrouter":
        return _OPENROUTER_FALLBACKS[0]
    return spec["default_model"]

def provider_nodes() -> List[Dict[str, Any]]:
    act = active_provider_id(); cfg = set(configured_providers())
    return [{"id": p["id"], "label": p["label"], "signup": p["signup"],
             "free_note": p["free_note"], "model": _model_for(p["id"]),
             "configured": p["id"] in cfg, "active": p["id"] == act} for p in PROVIDERS]

def chain_trace() -> List[Dict[str, str]]:
    return list(_trace)

def _headers(pid: str) -> Dict[str, str]:
    h = dict(_BROWSER_HEADERS); h["Content-Type"] = "application/json"
    k = _secret(_BY_ID[pid]["key_env"])
    if k:
        h["Authorization"] = f"Bearer {k}"
    if pid == "openrouter":
        h["X-Title"] = _secret("OPENROUTER_NAME") or "MomentumMaster Brain"
        site = _secret("OPENROUTER_SITE")
        if site:
            h["HTTP-Referer"] = site
    return h

def _post(url: str, headers: Dict[str, str], body: Dict[str, Any], timeout: float = 120.0):
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ssl.create_default_context()) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except urllib.error.URLError as e:
        raise BrainLLMError(f"network error: {e.reason}") from e
    except Exception as e:
        raise BrainLLMError(f"request failed: {e}") from e

def _extract_text(raw: str) -> str:
    try:
        data = json.loads(raw)
        return data["choices"][0]["message"]["content"] or ""
    except Exception:
        return ""

def _attempt(pid: str, model: str, messages, temperature, max_tokens) -> str:
    spec = _BY_ID[pid]
    url = f"{spec['base']}/v1/chat/completions"
    body = {"model": model, "messages": messages, "temperature": temperature,
            "max_tokens": int(max_tokens), "stream": False}
    status, raw = _post(url, _headers(pid), body)
    if 200 <= status < 300:
        txt = _extract_text(raw)
        if txt:
            return txt
        raise BrainLLMError(f"{spec['label']}: empty response")
    raise BrainLLMError(f"{spec['label']} ({status}): {raw[:200]}")

def chat(messages, provider=None, temperature=None, max_tokens=None) -> str:
    temp = 0.2 if temperature is None else float(temperature)
    mtok = 1100 if max_tokens is None else int(max_tokens)
    order = [provider] if (provider and _secret(_BY_ID.get(provider, {}).get("key_env", "__none__"))) else configured_providers()
    if not order:
        raise BrainLLMError("No LLM provider configured.")
    last = None
    for pid in order:
        models = [_model_for(pid)]
        if pid == "openrouter" and not _secret(_BY_ID[pid]["model_env"]):
            models = list(_OPENROUTER_FALLBACKS)
        for model in models:
            try:
                out = _attempt(pid, model, messages, temp, mtok)
                _resolved[pid] = model
                _trace.append({"provider": _BY_ID[pid]["label"], "status": "ok", "detail": model})
                return out
            except BrainLLMError as exc:
                last = exc
                _trace.append({"provider": _BY_ID[pid]["label"], "status": "failed", "detail": str(exc)[:160]})
                logger.warning("chain: %s/%s failed -> %s", pid, model, exc)
    raise BrainLLMError("All providers failed: " + (str(last) if last else "unknown"))

chat_with_chain = chat

def stream_chat(messages, provider=None, temperature=None, max_tokens=None):
    yield chat(messages, provider=provider, temperature=temperature, max_tokens=max_tokens)

def test_chain() -> List[Dict[str, Any]]:
    out = []
    for pid in configured_providers():
        try:
            r = _attempt(pid, _model_for(pid), [{"role": "user", "content": "Reply with the single word: pong"}], 0.0, 16)
            out.append({"id": pid, "label": _BY_ID[pid]["label"], "ok": True, "msg": f"{_BY_ID[pid]['label']} · {_model_for(pid)} · “{r[:24]}”"})
        except BrainLLMError as exc:
            out.append({"id": pid, "label": _BY_ID[pid]["label"], "ok": False, "msg": f"{_BY_ID[pid]['label']}: {exc}"})
    return out
