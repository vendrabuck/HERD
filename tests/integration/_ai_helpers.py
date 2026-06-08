"""Shared helpers for AI-feature integration tests.

Kept out of conftest.py because pytest treats conftest as fixture-only and
module-style imports of conftest are ambiguous across the repo (multiple
conftest.py files on sys.path). Plain Python module is unambiguous.
"""

import os


def ai_provider_configured() -> bool:
    """Mirror of the orchestrator's ai_is_configured() resolution.

    Anthropic is configured when EITHER AI_API_KEY OR AI_BASE_URL is set: a
    local Anthropic-compatible endpoint (e.g. vLLM) needs only a base URL,
    while the hosted API needs a key. openai_compat needs only AI_BASE_URL.
    Relies on conftest.py having already loaded the repo-root .env into
    os.environ (which it does at import time).
    """
    provider = (os.getenv("AI_PROVIDER", "").strip() or "anthropic").lower()
    if provider == "anthropic":
        key = os.getenv("AI_API_KEY", "").strip()
        base_url = os.getenv("AI_BASE_URL", "").strip()
        return bool(key or base_url)
    if provider == "openai_compat":
        return bool(os.getenv("AI_BASE_URL", "").strip())
    return False
