"""Persistence layer for multi-turn assistant conversations.

A conversation is scoped to (user_id, reservation_id). On the first turn the
route creates a conversation with the rendered reservation seed persisted as
the position-0 user message; on subsequent turns the route loads the prior
messages, appends the new user turn and the assistant's response (including
all tool_result blocks dispatched during the loop), and applies eviction
before returning.

Eviction policy (Branch 3 decision, locked in the plan):
- hard cap at settings.assistant_max_turns total messages
- approximate token budget at settings.assistant_history_token_budget,
  estimated as chars/4
- on eviction, drop the oldest USER and ASSISTANT messages (skipping the
  position-0 seed, which is pinned) until both bounds are satisfied
- tool-result messages count toward the budget but never trigger eviction
  on their own (they always pair with the preceding assistant turn)

Stored content_blocks JSON shape matches the dataclasses in
app/services/llm_provider.py: each block has a `type` discriminator
("text" | "tool_use" | "tool_result") and the structural fields of the
matching ContentBlock subclass. load_messages rehydrates back to Message
objects so the AIClient can pass them straight to the provider.
"""

from __future__ import annotations

import dataclasses
import logging
import uuid
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.conversation import AssistantConversation, AssistantMessage, MessageRole
from app.services.llm_provider import (
    ContentBlock,
    Message,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)

logger = logging.getLogger(__name__)


# --- Content-block serde ---


def _block_to_json(block: ContentBlock) -> dict[str, Any]:
    """Convert a dataclass ContentBlock to a JSON-storable dict."""
    return asdict(block)


def _json_to_block(raw: dict[str, Any]) -> ContentBlock:
    """Rehydrate a stored block dict back into a ContentBlock dataclass.

    Raises ValueError on unknown discriminator so a corrupted row fails
    loudly rather than silently dropping content the model needs.
    """
    type_ = raw.get("type")
    if type_ == "text":
        return TextBlock(text=raw["text"])
    if type_ == "tool_use":
        return ToolUseBlock(id=raw["id"], name=raw["name"], input=raw.get("input") or {})
    if type_ == "tool_result":
        return ToolResultBlock(
            tool_use_id=raw["tool_use_id"],
            content=raw["content"],
            is_error=raw.get("is_error", False),
        )
    raise ValueError(f"unknown content block type: {type_!r}")


def _blocks_to_json(blocks: list[ContentBlock]) -> list[dict[str, Any]]:
    return [_block_to_json(b) for b in blocks]


def _blocks_from_json(raws: list[dict[str, Any]]) -> list[ContentBlock]:
    return [_json_to_block(r) for r in raws]


# --- Token estimation (rough) ---


def _estimate_tokens_in_blocks(blocks: list[dict[str, Any]]) -> int:
    """Char-divided-by-4 token estimate. Cheap and good enough for budget
    enforcement; a real tokenizer is not worth the dep here.
    """
    total = 0
    for block in blocks:
        if block.get("type") == "text":
            total += len(block.get("text", ""))
        elif block.get("type") == "tool_use":
            total += len(block.get("name", "")) + len(str(block.get("input") or ""))
        elif block.get("type") == "tool_result":
            total += len(str(block.get("content", "")))
    return max(1, total // 4)


# --- Repository operations ---


async def create(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    reservation_id: uuid.UUID,
    seed_block: str,
) -> AssistantConversation:
    """Create a new conversation and persist the seed as the position-0 user
    message. Returns the persisted conversation with .id populated.
    """
    conv = AssistantConversation(
        user_id=user_id,
        reservation_id=reservation_id,
        seed_block=seed_block,
        turn_count=0,
    )
    db.add(conv)
    await db.flush()  # populate conv.id without committing yet

    seed_message = AssistantMessage(
        conversation_id=conv.id,
        role=MessageRole.USER,
        content_blocks=_blocks_to_json([TextBlock(text=seed_block)]),
        position=0,
    )
    db.add(seed_message)
    await db.commit()
    await db.refresh(conv)
    return conv


async def get_or_404(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    reservation_id: uuid.UUID,
    conversation_id: uuid.UUID,
) -> AssistantConversation | None:
    """Load a conversation by id and enforce (user_id, reservation_id)
    ownership. Returns None when the row does not exist OR is owned by a
    different user OR is scoped to a different reservation. The route turns
    None into 404 rather than 403 to avoid leaking conversation existence.
    """
    result = await db.execute(
        select(AssistantConversation).where(AssistantConversation.id == conversation_id)
    )
    conv = result.scalar_one_or_none()
    if conv is None:
        return None
    if conv.user_id != user_id or conv.reservation_id != reservation_id:
        return None
    return conv


async def load_messages(db: AsyncSession, *, conversation_id: uuid.UUID) -> list[Message]:
    """Return the conversation's messages as a list of Message objects ready
    to pass to the LLM provider. Ordered by position ascending; the seed is
    always position 0.
    """
    result = await db.execute(
        select(AssistantMessage)
        .where(AssistantMessage.conversation_id == conversation_id)
        .order_by(AssistantMessage.position)
    )
    rows = result.scalars().all()
    messages: list[Message] = []
    for row in rows:
        role = "assistant" if row.role == MessageRole.ASSISTANT else "user"
        messages.append(Message(role=role, content=_blocks_from_json(row.content_blocks)))
    return messages


async def set_seed_with_question(
    db: AsyncSession,
    *,
    conversation: AssistantConversation,
    question: str,
) -> None:
    """First-turn helper: rewrite the position-0 user message to combine the
    seed and the wrapped user question into one opening message. Called
    immediately after create() on first-turn requests so the model sees
    grounding context and the question together. Does not commit.
    """
    from sqlalchemy import select

    result = await db.execute(
        select(AssistantMessage)
        .where(AssistantMessage.conversation_id == conversation.id)
        .order_by(AssistantMessage.position)
        .limit(1)
    )
    seed_msg = result.scalar_one()
    combined = f"{conversation.seed_block}\n\n<question>\n{question}\n</question>"
    seed_msg.content_blocks = _blocks_to_json([TextBlock(text=combined)])
    conversation.turn_count = 1


async def append_user_text(
    db: AsyncSession,
    *,
    conversation: AssistantConversation,
    text: str,
) -> None:
    """Append a user-text turn to the conversation. Does not commit; the
    caller commits after the AI loop also persists the assistant turn so the
    full turn is one transactional unit.
    """
    position = await _next_position(db, conversation.id)
    msg = AssistantMessage(
        conversation_id=conversation.id,
        role=MessageRole.USER,
        content_blocks=_blocks_to_json([TextBlock(text=text)]),
        position=position,
    )
    db.add(msg)
    conversation.turn_count += 1
    conversation.last_used_at = datetime.now(UTC)


async def append_assistant_turn(
    db: AsyncSession,
    *,
    conversation: AssistantConversation,
    assistant_blocks: list[ContentBlock],
    tool_result_blocks: list[ContentBlock] | None = None,
) -> None:
    """Append one assistant turn and its tool-result echo (if any). The
    assistant blocks land first, then a single TOOL-role user message
    carrying every tool_result_block from that loop iteration set so the
    next turn's load_messages replays the same shape the provider already
    saw. No commit; the caller commits the full transaction.
    """
    position = await _next_position(db, conversation.id)
    assistant_msg = AssistantMessage(
        conversation_id=conversation.id,
        role=MessageRole.ASSISTANT,
        content_blocks=_blocks_to_json(assistant_blocks),
        position=position,
    )
    db.add(assistant_msg)

    if tool_result_blocks:
        tool_msg = AssistantMessage(
            conversation_id=conversation.id,
            role=MessageRole.TOOL,
            content_blocks=_blocks_to_json(tool_result_blocks),
            position=position + 1,
        )
        db.add(tool_msg)
    conversation.last_used_at = datetime.now(UTC)


async def evict_to_budget(
    db: AsyncSession,
    *,
    conversation: AssistantConversation,
    max_turns: int | None = None,
    token_budget: int | None = None,
) -> int:
    """Drop the oldest USER+ASSISTANT pair (and any TOOL echo between them)
    until both bounds hold. The position-0 seed message is pinned and never
    evicted; without it the model loses its grounding. Returns the number
    of messages dropped so the caller can log it.

    Idempotent: called once per turn after appending; a no-op when both
    bounds are already satisfied.
    """
    max_turns = max_turns if max_turns is not None else settings.assistant_max_turns
    token_budget = (
        token_budget if token_budget is not None else settings.assistant_history_token_budget
    )

    dropped = 0
    while True:
        result = await db.execute(
            select(AssistantMessage)
            .where(AssistantMessage.conversation_id == conversation.id)
            .order_by(AssistantMessage.position)
        )
        rows = list(result.scalars().all())
        if len(rows) <= 1:
            break  # seed only

        total_tokens = sum(_estimate_tokens_in_blocks(r.content_blocks) for r in rows)
        if len(rows) <= max_turns and total_tokens <= token_budget:
            break

        # Drop the oldest non-seed pair: the first USER (position > 0) plus
        # the following ASSISTANT and any TOOL echo immediately after.
        first_evictable_idx = None
        for idx, row in enumerate(rows):
            if row.position == 0:
                continue
            if row.role == MessageRole.USER:
                first_evictable_idx = idx
                break
        if first_evictable_idx is None:
            break  # nothing to drop

        evict_ids: list[uuid.UUID] = [rows[first_evictable_idx].id]
        next_idx = first_evictable_idx + 1
        while next_idx < len(rows) and rows[next_idx].role in (
            MessageRole.ASSISTANT,
            MessageRole.TOOL,
        ):
            evict_ids.append(rows[next_idx].id)
            next_idx += 1

        await db.execute(delete(AssistantMessage).where(AssistantMessage.id.in_(evict_ids)))
        dropped += len(evict_ids)

    if dropped:
        logger.info(
            "conversation_evicted",
            extra={
                "conversation_id": str(conversation.id),
                "dropped_messages": dropped,
            },
        )
    return dropped


async def touch(db: AsyncSession, *, conversation: AssistantConversation) -> None:
    """Bump last_used_at so the sweeper does not expire an active chat."""
    conversation.last_used_at = datetime.now(UTC)


async def expire_idle(db: AsyncSession, *, ttl_hours: int | None = None) -> int:
    """Delete conversations idle longer than ttl_hours. Returns count
    deleted so the sweeper task can log it.
    """
    ttl_hours = ttl_hours if ttl_hours is not None else settings.assistant_conversation_ttl_hours
    cutoff = datetime.now(UTC) - timedelta(hours=ttl_hours)
    result = await db.execute(
        select(AssistantConversation.id).where(AssistantConversation.last_used_at < cutoff)
    )
    ids = [r for r in result.scalars().all()]
    if not ids:
        return 0
    await db.execute(delete(AssistantConversation).where(AssistantConversation.id.in_(ids)))
    await db.commit()
    logger.info(
        "conversation_sweeper_expired",
        extra={"expired_count": len(ids), "ttl_hours": ttl_hours},
    )
    return len(ids)


# --- internal ---


async def _next_position(db: AsyncSession, conversation_id: uuid.UUID) -> int:
    from sqlalchemy import func

    result = await db.execute(
        select(func.coalesce(func.max(AssistantMessage.position), -1)).where(
            AssistantMessage.conversation_id == conversation_id
        )
    )
    return int(result.scalar_one()) + 1


# Re-export so the route can use dataclasses.asdict on dispatched blocks
# without re-importing.
__all__ = [
    "create",
    "get_or_404",
    "load_messages",
    "set_seed_with_question",
    "append_user_text",
    "append_assistant_turn",
    "evict_to_budget",
    "touch",
    "expire_idle",
    "dataclasses",
]
