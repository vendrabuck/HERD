"""AI-LOOP-7 (issue #1040): the assistant's system prompt gains the
write-tools section if and only if AI_WRITE_TOOLS_ENABLED is on, so the
model is never told about tools that are absent from its tool list."""

from app import config as config_module
from app.services.ai_client import (
    RESERVATION_ASSISTANT_TOOL_SYSTEM_PROMPT,
    RESERVATION_ASSISTANT_WRITE_TOOLS_PROMPT,
    reservation_assistant_system_prompt,
)


def test_system_prompt_has_the_write_tools_section_only_when_enabled(monkeypatch):
    monkeypatch.setattr(config_module.settings, "ai_write_tools_enabled", False)
    off = reservation_assistant_system_prompt()
    assert RESERVATION_ASSISTANT_WRITE_TOOLS_PROMPT not in off
    assert off.startswith(RESERVATION_ASSISTANT_TOOL_SYSTEM_PROMPT)

    monkeypatch.setattr(config_module.settings, "ai_write_tools_enabled", True)
    on = reservation_assistant_system_prompt()
    assert RESERVATION_ASSISTANT_WRITE_TOOLS_PROMPT in on
    assert on.startswith(RESERVATION_ASSISTANT_TOOL_SYSTEM_PROMPT)
    # The section is appended once, never repeated.
    assert on.count(RESERVATION_ASSISTANT_WRITE_TOOLS_PROMPT) == 1
