"""src/brain.py — facade so any import path keeps working.

Real logic lives in src.brain_llm (free failover chain) and src.brain_kb
(memory + retrieval + analytics). Re-exports both so existing
`import src.brain as B` keeps resolving without change.
"""
from src.brain_llm import *  # noqa: F401,F403
from src.brain_kb import *  # noqa: F401,F403
