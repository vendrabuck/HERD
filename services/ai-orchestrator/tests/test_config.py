"""Unit tests for app.config.Settings validation.

Covers issue #338's decided fix: the assistant idle-conversation TTL is
configurable per deployment (ASSISTANT_CONVERSATION_TTL_HOURS), defaulting to
today's 24h behavior, with 0 or negative rejected at settings-validation time
rather than silently meaning "expire everything instantly".
"""

import pytest
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
