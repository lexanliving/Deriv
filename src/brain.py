"""src/brain.py — facade so any import path keeps working.

The real logic lives in src.brain_llm (free LLM client) and src.brain_kb
(memory + retrieval + analytics). This module re-exports both so existing
imports (`import src.brain as B`) resolve without change.
"""
from src.brain_llm import (  # noqa: F401
    BrainLLMError, PROVIDER_INFO, chat, configured_providers, detect_provider,
    stream_chat, test_provider,
)
from src.brain_kb import (  # noqa: F401
    DEFAULT_WEIGHTS, FACTOR_KEYS, FACTOR_MAX, FACTORS, PRESETS, RULEBOOK,
    THRESHOLD_OPTIONS, add_document, add_lesson, backtest, baseline,
    build_messages, compute_postmortem, docs_bytes, find_proposal, import_kb,
    import_lessons, kb_markdown_bytes, lessons_bytes, list_documents,
    load_lessons, postmortem_text, preset_text, reweight_confidence, retrieve,
)

__all__ = [
    "BrainLLMError", "PROVIDER_INFO", "chat", "configured_providers",
    "detect_provider", "stream_chat", "test_provider",
    "DEFAULT_WEIGHTS", "FACTOR_KEYS", "FACTOR_MAX", "FACTORS", "PRESETS",
    "RULEBOOK", "THRESHOLD_OPTIONS", "add_document", "add_lesson", "backtest",
    "baseline", "build_messages", "compute_postmortem", "docs_bytes",
    "find_proposal", "import_kb", "import_lessons", "kb_markdown_bytes",
    "lessons_bytes", "list_documents", "load_lessons", "postmortem_text",
    "preset_text", "reweight_confidence", "retrieve",
]
