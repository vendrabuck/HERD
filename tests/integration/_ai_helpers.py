"""Shared helpers for AI-feature integration tests.

Kept out of conftest.py because pytest treats conftest as fixture-only and
module-style imports of conftest are ambiguous across the repo (multiple
conftest.py files on sys.path). Plain Python module is unambiguous.
"""

import os


def ai_provider_configured() -> bool:
    """Mirror of the orchestrator's ai_is_configured() resolution.

    Honors the AI_API_KEY canonical var with ANTHROPIC_API_KEY as a
    deprecated fallback for one release; for openai_compat, only requires
    AI_BASE_URL. Relies on conftest.py having already loaded the repo-root
    .env into os.environ (which it does at import time).
    """
    provider = (os.getenv("AI_PROVIDER", "").strip() or "anthropic").lower()
    if provider == "anthropic":
        key = os.getenv("AI_API_KEY", "").strip() or os.getenv("ANTHROPIC_API_KEY", "").strip()
        return bool(key)
    if provider == "openai_compat":
        return bool(os.getenv("AI_BASE_URL", "").strip())
    return False
