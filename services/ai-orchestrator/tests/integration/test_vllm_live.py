"""Live smoke test against an OpenAI-compatible LLM server.

Gated on `HERD_VLLM_BASE_URL`: without it set, every test in this file
auto-skips so default unit-test runs ignore it. Set it to the v1 base of
your bench, for example:

    HERD_VLLM_BASE_URL=http://qwen-bench:8000/v1 \
    HERD_VLLM_MODEL=Qwen/Qwen3-35B-Instruct \
    pytest services/ai-orchestrator/tests/integration/

Verifies that OpenAICompatProvider can complete a text turn AND a forced
tool_use turn against a real server. The unit tests cover everything that
mocked transport can; this catches wire-format issues a mock can't see,
specifically: vLLM's tool-parser flag configuration, finish_reason
mapping under real load, and JSON-schema rejection on the parameters field.

If `test_vllm_returns_tool_use` fails with "expected stop_reason='tool_use'":
the vLLM bench is missing `--enable-auto-tool-choice` and the right
`--tool-call-parser` flag for the model. Fix the bench, not the code.
"""

import os

import jsonschema
import pytest
from app.services.ai_client import TOPOLOGY_TOOL
from app.services.llm_provider import (
    Message,
    TextBlock,
    ToolChoiceAuto,
    ToolChoiceTool,
    ToolUseBlock,
)
from app.services.providers.openai_provider import OpenAICompatProvider

LIVE_VLLM_URL = os.environ.get("HERD_VLLM_BASE_URL")
LIVE_VLLM_MODEL = os.environ.get("HERD_VLLM_MODEL", "default")
LIVE_VLLM_KEY = os.environ.get("HERD_VLLM_API_KEY", "EMPTY")

pytestmark = pytest.mark.skipif(
    not LIVE_VLLM_URL,
    reason="set HERD_VLLM_BASE_URL to enable live vLLM tests",
)


@pytest.fixture
def live_provider() -> OpenAICompatProvider:
    return OpenAICompatProvider(
        api_key=LIVE_VLLM_KEY, base_url=LIVE_VLLM_URL, model=LIVE_VLLM_MODEL
    )


@pytest.mark.asyncio
async def test_vllm_responds_to_simple_prompt(live_provider):
    """One-shot text completion. The bench is configured and serving."""
    resp = await live_provider.call(
        system="You are a terse assistant. Answer in one word.",
        messages=[Message(role="user", content=[TextBlock(text="What color is the sky?")])],
        tools=None,
        tool_choice=ToolChoiceAuto(),
        max_tokens=64,
        timeout_s=30.0,
    )
    text_blocks = [b for b in resp.content if isinstance(b, TextBlock)]
    assert text_blocks, f"expected at least one TextBlock, got: {resp.content!r}"
    assert text_blocks[0].text.strip(), "TextBlock was empty"
    assert resp.stop_reason in ("end_turn", "max_tokens"), (
        f"unexpected stop_reason {resp.stop_reason!r}; "
        "check the vLLM server's finish_reason emission"
    )
    assert resp.usage.input_tokens > 0, "usage.input_tokens did not propagate"


@pytest.mark.asyncio
async def test_vllm_returns_tool_use(live_provider):
    """Forced tool_use call. Bench must be launched with
    `--enable-auto-tool-choice` plus the model's tool-call parser flag
    (`--tool-call-parser hermes` for Hermes-style Qwen3, or the model-specific
    parser if vLLM ships one for your model).
    """
    inventory = "- Generic Router: 2\n- Generic Switch: 1"
    resp = await live_provider.call(
        system=(
            "You are a lab automation assistant. You MUST call the "
            "propose_topology tool exactly once. Use only template_names "
            f"from this inventory:\n{inventory}"
        ),
        messages=[
            Message(
                role="user",
                content=[TextBlock(text="Build a tiny topology: two routers connected at L3.")],
            )
        ],
        tools=[TOPOLOGY_TOOL],
        tool_choice=ToolChoiceTool(name="propose_topology"),
        max_tokens=2048,
        timeout_s=60.0,
    )
    tool_uses = [b for b in resp.content if isinstance(b, ToolUseBlock)]
    assert tool_uses, (
        f"expected at least one ToolUseBlock; got content={resp.content!r} "
        f"stop_reason={resp.stop_reason!r}. Likely vLLM tool-parser flags "
        "are not set."
    )
    assert resp.stop_reason == "tool_use", (
        f"expected stop_reason='tool_use', got {resp.stop_reason!r}. "
        "Check vLLM tool-parser configuration."
    )
    proposal = tool_uses[0].input
    # Validate against the canonical schema; if this fails the model emitted
    # something the rest of the pipeline would also reject.
    jsonschema.validate(proposal, TOPOLOGY_TOOL.input_schema)
