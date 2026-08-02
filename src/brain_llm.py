"""src/brain_llm.py — free, provider-agnostic LLM client with streaming.

Reproduces the "generation" half of the RAG blueprint the user shared
(chat-ui/main.py + the DO RAG README), but with NO paid dependency:

  * Groq          (default; free, fast, OpenAI-compatible)
  * Gemini        (free alternative)
  * openai_compat (OpenRouter :free models / self-hosted vLLM / Ollama server)

Everything is plain HTTP via the standard library (urllib), exactly like the
existing src/api_client.py, so this adds NO pip dependency and cannot break
installation on Streamlit Cloud. Streaming uses SSE where available and falls
back transparently to a blocking call.

The client is an ADVISOR only. It never places trades and never mutates the
live strategy; that boundary is enforced by the page + the gate-backtest.
"""
from __future__ import annotations

import json
import os
import ssl
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
#  provider metadata (drives the UI wizard + docs)                            #
# --------------------------------------------------------------------------- #
PROVIDER_INFO: Dict[str, Dict[str, Any]] = {
    "groq": {
        "label": "Groq",
        "signup": "https://console.groq.com/keys",
        "env_key": "GROQ_API_KEY",
        "env_model": "GROQ_MODEL",
        "default_model": "llama-3.3-70b-versatile",
        "free_note": "Free tier: ~30 req/min, large daily token budget. Fastest free option.",
    },
    "gemini": {
        "label": "Gemini",
        "signup": "https://aistudio.google.com/apikey",
        "env_key": "GEMINI_API_KEY",
        "env_model": "GEMINI_MODEL",
        "default_model": "gemini-2.0-flash",
        "free_note": "Free tier: ~15 req/min. Different model family — good to cross-check advice.",
    },
    "openai_compat": {
        "label": "OpenAI-compatible",
        "signup": "https://openrouter.ai/keys  (or your own Ollama / vLLM host)",
        "env_key": "OPENAI_COMPAT_KEY",
        "env_base": "OPENAI_COMPAT_BASE",
        "env_model": "OPENAI_COMPAT_MODEL",
        "default_model": "",
        "free_note": "OpenRouter ':free' models, or Ollama on your VPS (set BASE=http://localhost:11434/v1, no key).",
    },
}


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


def _model_for(provider: str) -> str:
    info = PROVIDER_INFO[provider]
    return _secret(info["env_model"]) or info["default_model"]


def detect_provider(force: Optional[str] = None) -> Optional[str]:
    """Return the first configured provider, or an explicit override, else None."""
    want = (force or _secret("BRAIN_PROVIDER") or "auto").strip().lower()
    have = {
        "groq": bool(_secret("GROQ_API_KEY")),
        "gemini": bool(_secret("GEMINI_API_KEY")),
        "openai_compat": bool(_secret("OPENAI_COMPAT_BASE")),
    }
    if want != "auto":
        return want if have.get(want) else None
    for p in ("groq", "gemini", "openai_compat"):
        if have[p]:
            return p
    return None


def configured_providers() -> List[str]:
    return [p for p in ("groq", "gemini", "openai_compat")
            if (p == "groq" and _secret("GROQ_API_KEY"))
            or (p == "gemini" and _secret("GEMINI_API_KEY"))
            or (p == "openai_compat" and _secret("OPENAI_COMPAT_BASE"))]


# --------------------------------------------------------------------------- #
#  low-level HTTP                                                             #
# --------------------------------------------------------------------------- #
def _post_json(url: str, headers: Dict[str, str], body: Any, timeout: float) -> Tuple[int, str]:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={**headers, "Content-Type": "application/json"}, method="POST")
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except urllib.error.URLError as exc:
        raise BrainLLMError(f"network error reaching {url}: {exc.reason}") from exc
    except Exception as exc:
        raise BrainLLMError(f"request failed: {exc}") from exc


def _open_stream(url: str, headers: Dict[str, str], body: Any, timeout: float):
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={**headers, "Content-Type": "application/json"}, method="POST")
    ctx = ssl.create_default_context()
    return urllib.request.urlopen(req, timeout=timeout, context=ctx)  # caller MUST close


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
#  message translation                                                        #
# --------------------------------------------------------------------------- #
def _gemini_payload(messages: List[Dict[str, str]], model: str, temperature: float, max_tokens: int, stream: bool) -> Tuple[str, Dict[str, Any]]:
    base = _secret("GEMINI_API_BASE") or "https://generativelanguage.googleapis.com"
    verb = "streamGenerateContent?alt=sse" if stream else "generateContent"
    url = f"{base}/v1beta/models/{model}:{verb}&key={_secret('GEMINI_API_KEY')}"
    system_parts, contents, last = [], [], None
    for m in messages:
        role = m.get("role", "user")
        text = m.get("content", "")
        if role == "system":
            system_parts.append(text)
            continue
        grole = "model" if role == "assistant" else "user"
        if grole == last and contents:
            contents[-1]["parts"].append({"text": text})
        else:
            contents.append({"role": grole, "parts": [{"text": text}]})
            last = grole
    if not contents:
        contents = [{"role": "user", "parts": [{"text": "Hello."}]}]
    body: Dict[str, Any] = {"contents": contents, "generationConfig": {"temperature": temperature, "maxOutputTokens": int(max_tokens)}}
    if system_parts:
        body["systemInstruction"] = {"parts": [{"text": "\n\n".join(system_parts)}]}
    return url, body


def _openai_payload(messages: List[Dict[str, str]], model: str, temperature: float, max_tokens: int, stream: bool) -> Tuple[str, Dict[str, str], Dict[str, Any]]:
    provider = "groq" if model and False else None  # placeholder; real routing below
    return ("", {}, {})  # unused; see _build_openai


def _build_openai(provider: str, messages: List[Dict[str, str]], model: str, temperature: float, max_tokens: int, stream: bool) -> Tuple[str, Dict[str, str], Dict[str, Any]]:
    if provider == "groq":
        base = _secret("GROQ_API_BASE") or "https://api.groq.com/openai"
        key = _secret("GROQ_API_KEY")
    else:  # openai_compat
        base = _secret("OPENAI_COMPAT_BASE").rstrip("/")
        key = _secret("OPENAI_COMPAT_KEY")
    url = f"{base}/v1/chat/completions"
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    body = {"model": model, "messages": messages, "temperature": temperature, "max_tokens": int(max_tokens), "stream": stream}
    return url, headers, body


# --------------------------------------------------------------------------- #
#  delta extractors                                                           #
# --------------------------------------------------------------------------- #
def _delta_openai(obj: dict) -> str:
    try:
        return obj["choices"][0].get("delta", {}).get("content", "") or ""
    except Exception:
        return ""


def _delta_gemini(obj: dict) -> str:
    try:
        return obj["candidates"][0]["content"]["parts"][0].get("text", "") or ""
    except Exception:
        return ""


def _text_openai(obj: dict) -> str:
    try:
        return obj["choices"][0]["message"]["content"] or ""
    except Exception:
        return ""


def _text_gemini(obj: dict) -> str:
    try:
        return obj["candidates"][0]["content"]["parts"][0].get("text", "") or ""
    except Exception:
        return ""


# --------------------------------------------------------------------------- #
#  public API                                                                 #
# --------------------------------------------------------------------------- #
def _resolve(provider: Optional[str]) -> str:
    p = provider or detect_provider()
    if not p:
        raise BrainLLMError("No LLM provider configured. See BRAIN_SETUP.md (all options are free).")
    return p


def chat(messages: List[Dict[str, str]], provider: Optional[str] = None,
         temperature: Optional[float] = None, max_tokens: Optional[int] = None) -> str:
    """Blocking call. Returns the full assistant text."""
    p = _resolve(provider)
    temp = float(_secret("BRAIN_TEMPERATURE") or 0.2) if temperature is None else float(temperature)
    mtok = int(_secret("BRAIN_MAX_TOKENS") or 1024) if max_tokens is None else int(max_tokens)
    model = _model_for(p)
    if p in ("groq", "openai_compat"):
        if not model:
            raise BrainLLMError(f"Set {PROVIDER_INFO[p]['env_model']} (the model name) for the {p} provider.")
        url, headers, body = _build_openai(p, messages, model, temp, mtok, False)
        status, raw = _post_json(url, headers, body, timeout=120.0)
        if not (200 <= status < 300):
            raise BrainLLMError(f"{p} chat failed ({status}): {raw[:300]}")
        return _text_openai(json.loads(raw)).strip()
    # gemini
    if not model:
        raise BrainLLMError("Set GEMINI_MODEL for the gemini provider.")
    url, body = _gemini_payload(messages, model, temp, mtok, stream=False)
    status, raw = _post_json(url, {}, body, timeout=120.0)
    if not (200 <= status < 300):
        raise BrainLLMError(f"gemini chat failed ({status}): {raw[:300]}")
    return _text_gemini(json.loads(raw)).strip()


def stream_chat(messages: List[Dict[str, str]], provider: Optional[str] = None,
                temperature: Optional[float] = None, max_tokens: Optional[int] = None) -> Iterator[str]:
    """Yield text deltas. Connection errors raise (so callers can fall back to
    chat()); mid-stream parse errors are swallowed (stream just ends)."""
    p = _resolve(provider)
    temp = float(_secret("BRAIN_TEMPERATURE") or 0.2) if temperature is None else float(temperature)
    mtok = int(_secret("BRAIN_MAX_TOKENS") or 1024) if max_tokens is None else int(max_tokens)
    model = _model_for(p)
    if p in ("groq", "openai_compat"):
        if not model:
            raise BrainLLMError(f"Set {PROVIDER_INFO[p]['env_model']} for the {p} provider.")
        url, headers, body = _build_openai(p, messages, model, temp, mtok, True)
        resp = _open_stream(url, headers, body, timeout=120.0)  # raises on connect failure
        extractor = _delta_openai
    else:
        if not model:
            raise BrainLLMError("Set GEMINI_MODEL for the gemini provider.")
        url, body = _gemini_payload(messages, model, temp, mtok, stream=True)
        resp = _open_stream(url, {}, body, timeout=120.0)
        extractor = _delta_gemini
    try:
        for obj in _iter_sse(resp):
            d = extractor(obj)
            if d:
                yield d
    finally:
        try:
            resp.close()
        except Exception:
            pass


def test_provider(provider: str) -> Tuple[bool, float, str]:
    """Cheap connectivity probe. Returns (ok, latency_ms, message)."""
    info = PROVIDER_INFO.get(provider)
    if not info:
        return False, 0.0, f"unknown provider '{provider}'"
    if provider == "openai_compat" and not _secret("OPENAI_COMPAT_BASE"):
        return False, 0.0, "OPENAI_COMPAT_BASE not set"
    if provider != "openai_compat" and not _secret(info["env_key"]):
        return False, 0.0, f"{info['env_key']} not set"
    model = _model_for(provider)
    if not model:
        return False, 0.0, f"{info['env_model']} not set (no default for this provider)"
    t0 = time.time()
    try:
        reply = chat([{"role": "user", "content": "Reply with the single word: pong"}], provider=provider, max_tokens=16)
        dt = (time.time() - t0) * 1000.0
        return True, dt, f"{info['label']} · {model} · {dt:.0f}ms · “{reply[:24]}”"
    except BrainLLMError as exc:
        return False, (time.time() - t0) * 1000.0, f"{info['label']}: {exc}"
