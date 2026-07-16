"""Unit tests for app.config: Settings validation and startup env checks.

Two issues share this module:

- Issue #338: the assistant idle-conversation TTL is configurable per
  deployment (ASSISTANT_CONVERSATION_TTL_HOURS), defaulting to today's 24h
  behavior, with 0 or negative rejected at settings-validation time rather
  than silently meaning "expire everything instantly".
- Issue #339: ANTHROPIC_API_KEY was documented (.env.example,
  config_schema.py) as a one-release fallback for AI_API_KEY, but no code
  ever read it: an operator who set only ANTHROPIC_API_KEY got no AI features
  with no signal why. The tests pin the exact startup warning wording and
  confirm the check stays silent once AI_API_KEY is set, or when
  ANTHROPIC_API_KEY was never set at all.
"""

import logging

import pytest
from app import config as config_module
from app.config import Settings
from pydantic import ValidationError


def test_default_ttl_hours_is_24():
    assert Settings().assistant_conversation_ttl_hours == 24


def test_custom_positive_ttl_hours_is_accepted():
    assert Settings(assistant_conversation_ttl_hours=1).assistant_conversation_ttl_hours == 1
    assert Settings(assistant_conversation_ttl_hours=72).assistant_conversation_ttl_hours == 72


@pytest.mark.parametrize("bad_value", [0, -1, -24])
def test_zero_or_negative_ttl_hours_rejected(bad_value):
    with pytest.raises(ValidationError) as exc_info:
        Settings(assistant_conversation_ttl_hours=bad_value)
    message = str(exc_info.value)
    assert "assistant_conversation_ttl_hours must be a positive integer" in message
    assert f"got {bad_value}" in message
    assert "expire every conversation instantly" in message


def test_warns_when_anthropic_key_set_and_ai_api_key_blank(monkeypatch, caplog):
    monkeypatch.setattr(config_module.settings, "anthropic_api_key", "sk-ant-legacy")
    monkeypatch.setattr(config_module.settings, "ai_api_key", "")

    with caplog.at_level(logging.WARNING, logger="app.config"):
        config_module.warn_if_anthropic_api_key_unused()

    assert caplog.messages == [config_module.LEGACY_ANTHROPIC_ENV_WARNING]
    assert caplog.messages[0] == (
        "ANTHROPIC_API_KEY is set but is not honored by ai-orchestrator and has no "
        "effect; set AI_API_KEY instead. AI features remain disabled until "
        "AI_API_KEY is set."
    )


def test_no_warning_when_ai_api_key_set(monkeypatch, caplog):
    """AI_API_KEY set: the legacy var being present too is harmless, no warning."""
    monkeypatch.setattr(config_module.settings, "anthropic_api_key", "sk-ant-legacy")
    monkeypatch.setattr(config_module.settings, "ai_api_key", "sk-ai-real")

    with caplog.at_level(logging.WARNING, logger="app.config"):
        config_module.warn_if_anthropic_api_key_unused()

    assert caplog.messages == []


def test_no_warning_when_anthropic_key_blank(monkeypatch, caplog):
    """Neither var set: nothing to warn about."""
    monkeypatch.setattr(config_module.settings, "anthropic_api_key", "")
    monkeypatch.setattr(config_module.settings, "ai_api_key", "")

    with caplog.at_level(logging.WARNING, logger="app.config"):
        config_module.warn_if_anthropic_api_key_unused()

    assert caplog.messages == []
