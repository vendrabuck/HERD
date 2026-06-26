"""Unit tests for the multi-turn conversation persistence layer."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.database import Base, engine
from app.models.conversation import AssistantConversation, AssistantMessage, MessageRole
from app.services import conversation_repo
from app.services.llm_provider import TextBlock, ToolResultBlock, ToolUseBlock
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

TestSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
async def setup_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


USER_A = uuid.uuid4()
USER_B = uuid.uuid4()
RES_X = uuid.uuid4()
RES_Y = uuid.uuid4()


# --- create + load_messages ---


@pytest.mark.asyncio
async def test_create_persists_seed_as_first_user_message():
    async with TestSessionLocal() as db:
        conv = await conversation_repo.create(
            db, user_id=USER_A, reservation_id=RES_X, seed_block="<seed/>"
        )
        assert conv.id is not None
        assert conv.user_id == USER_A
        assert conv.reservation_id == RES_X

        messages = await conversation_repo.load_messages(db, conversation_id=conv.id)
    assert len(messages) == 1
    assert messages[0].role == "user"
    assert isinstance(messages[0].content[0], TextBlock)
    assert messages[0].content[0].text == "<seed/>"


# --- _json_to_block fail-loud guard ---


def test_json_to_block_raises_on_unknown_type():
    """A corrupted/unknown discriminator must fail loudly so the loader never
    silently drops content the model needs on the next turn."""
    with pytest.raises(ValueError, match="unknown content block type"):
        conversation_repo._json_to_block({"type": "bogus", "text": "x"})


def test_json_to_block_raises_on_missing_type():
    """A block with no `type` key takes the same fail-loud path (type_ is None)."""
    with pytest.raises(ValueError, match="unknown content block type"):
        conversation_repo._json_to_block({"text": "no discriminator"})


# --- ownership enforcement on get_or_404 ---


@pytest.mark.asyncio
async def test_get_or_404_returns_conversation_for_owner():
    async with TestSessionLocal() as db:
        conv = await conversation_repo.create(
            db, user_id=USER_A, reservation_id=RES_X, seed_block="seed"
        )
        found = await conversation_repo.get_or_404(
            db, user_id=USER_A, reservation_id=RES_X, conversation_id=conv.id
        )
    assert found is not None
    assert found.id == conv.id


@pytest.mark.asyncio
async def test_get_or_404_returns_none_for_other_user():
    async with TestSessionLocal() as db:
        conv = await conversation_repo.create(
            db, user_id=USER_A, reservation_id=RES_X, seed_block="seed"
        )
        result = await conversation_repo.get_or_404(
            db, user_id=USER_B, reservation_id=RES_X, conversation_id=conv.id
        )
    assert result is None


@pytest.mark.asyncio
async def test_get_or_404_returns_none_for_wrong_reservation():
    async with TestSessionLocal() as db:
        conv = await conversation_repo.create(
            db, user_id=USER_A, reservation_id=RES_X, seed_block="seed"
        )
        result = await conversation_repo.get_or_404(
            db, user_id=USER_A, reservation_id=RES_Y, conversation_id=conv.id
        )
    assert result is None


@pytest.mark.asyncio
async def test_get_or_404_returns_none_for_unknown_id():
    async with TestSessionLocal() as db:
        result = await conversation_repo.get_or_404(
            db, user_id=USER_A, reservation_id=RES_X, conversation_id=uuid.uuid4()
        )
    assert result is None


# --- set_seed_with_question + append_user_text ---


@pytest.mark.asyncio
async def test_set_seed_with_question_wraps_question_in_first_message():
    async with TestSessionLocal() as db:
        conv = await conversation_repo.create(
            db, user_id=USER_A, reservation_id=RES_X, seed_block="<seed/>"
        )
        await conversation_repo.set_seed_with_question(
            db, conversation=conv, question="What is broken?"
        )
        await db.commit()

        messages = await conversation_repo.load_messages(db, conversation_id=conv.id)
    assert len(messages) == 1
    text = messages[0].content[0].text
    assert "<seed/>" in text
    assert "<question>" in text
    assert "What is broken?" in text


@pytest.mark.asyncio
async def test_append_user_text_adds_a_new_message_at_next_position():
    async with TestSessionLocal() as db:
        conv = await conversation_repo.create(
            db, user_id=USER_A, reservation_id=RES_X, seed_block="seed"
        )
        await conversation_repo.append_user_text(db, conversation=conv, text="follow-up")
        await db.commit()

        result = await db.execute(
            select(AssistantMessage)
            .where(AssistantMessage.conversation_id == conv.id)
            .order_by(AssistantMessage.position)
        )
        rows = result.scalars().all()
    assert [r.position for r in rows] == [0, 1]
    assert rows[1].role == MessageRole.USER
    assert rows[1].content_blocks[0]["text"] == "follow-up"


# --- append_assistant_turn ---


@pytest.mark.asyncio
async def test_append_assistant_turn_stores_assistant_and_tool_result_in_order():
    async with TestSessionLocal() as db:
        conv = await conversation_repo.create(
            db, user_id=USER_A, reservation_id=RES_X, seed_block="seed"
        )
        await conversation_repo.append_assistant_turn(
            db,
            conversation=conv,
            assistant_blocks=[
                TextBlock(text="calling tool"),
                ToolUseBlock(id="toolu_1", name="get_device", input={"device_id": "d1"}),
            ],
            tool_result_blocks=[
                ToolResultBlock(tool_use_id="toolu_1", content='{"name":"d1"}'),
            ],
        )
        await db.commit()

        result = await db.execute(
            select(AssistantMessage)
            .where(AssistantMessage.conversation_id == conv.id)
            .order_by(AssistantMessage.position)
        )
        rows = result.scalars().all()
    assert len(rows) == 3
    assert rows[0].role == MessageRole.USER  # seed
    assert rows[1].role == MessageRole.ASSISTANT
    assert rows[2].role == MessageRole.TOOL
    # Block discriminators round-trip cleanly.
    assert rows[1].content_blocks[1]["type"] == "tool_use"
    assert rows[2].content_blocks[0]["type"] == "tool_result"


@pytest.mark.asyncio
async def test_load_messages_rehydrates_all_block_types():
    async with TestSessionLocal() as db:
        conv = await conversation_repo.create(
            db, user_id=USER_A, reservation_id=RES_X, seed_block="seed"
        )
        await conversation_repo.append_assistant_turn(
            db,
            conversation=conv,
            assistant_blocks=[
                ToolUseBlock(id="t1", name="x", input={"a": 1}),
            ],
            tool_result_blocks=[
                ToolResultBlock(tool_use_id="t1", content="ok", is_error=False),
            ],
        )
        await db.commit()

        messages = await conversation_repo.load_messages(db, conversation_id=conv.id)
    assert len(messages) == 3
    tu = messages[1].content[0]
    assert isinstance(tu, ToolUseBlock)
    assert tu.input == {"a": 1}
    tr = messages[2].content[0]
    assert isinstance(tr, ToolResultBlock)
    assert tr.tool_use_id == "t1"


# --- evict_to_budget ---


@pytest.mark.asyncio
async def test_evict_to_budget_drops_oldest_pair_when_over_turn_cap():
    async with TestSessionLocal() as db:
        conv = await conversation_repo.create(
            db, user_id=USER_A, reservation_id=RES_X, seed_block="seed"
        )
        # 6 turns: 6 user + 6 assistant = 12 non-seed messages + 1 seed = 13
        for i in range(6):
            await conversation_repo.append_user_text(db, conversation=conv, text=f"q{i}")
            await conversation_repo.append_assistant_turn(
                db,
                conversation=conv,
                assistant_blocks=[TextBlock(text=f"a{i}")],
            )
        await db.commit()

        await conversation_repo.evict_to_budget(
            db, conversation=conv, max_turns=5, token_budget=10_000_000
        )
        await db.commit()

        messages = await conversation_repo.load_messages(db, conversation_id=conv.id)
    assert len(messages) <= 5
    # Seed is pinned at position 0; the dropped pair was the earliest user/assistant.
    assert messages[0].content[0].text == "seed"


@pytest.mark.asyncio
async def test_evict_to_budget_respects_token_budget():
    async with TestSessionLocal() as db:
        conv = await conversation_repo.create(
            db, user_id=USER_A, reservation_id=RES_X, seed_block="seed"
        )
        # Long text bursts the token budget intentionally.
        long = "x" * 1000  # 1000 chars ~= 250 tokens
        for i in range(8):
            await conversation_repo.append_user_text(db, conversation=conv, text=long)
            await conversation_repo.append_assistant_turn(
                db,
                conversation=conv,
                assistant_blocks=[TextBlock(text=long)],
            )
        await db.commit()

        await conversation_repo.evict_to_budget(
            db,
            conversation=conv,
            max_turns=100,
            token_budget=500,  # ~2000 chars
        )
        await db.commit()

        messages = await conversation_repo.load_messages(db, conversation_id=conv.id)
    # Seed always survives.
    assert messages[0].content[0].text == "seed"
    # Eviction trimmed history under the budget.
    total_chars = sum(
        len(b.text) if isinstance(b, TextBlock) else 0 for m in messages for b in m.content
    )
    # 500 tokens * 4 chars/token ~= 2000 char budget; seed is 4 chars.
    assert total_chars <= 2500


# --- expire_idle ---


@pytest.mark.asyncio
async def test_expire_idle_deletes_old_conversations_and_keeps_recent():
    async with TestSessionLocal() as db:
        old = await conversation_repo.create(
            db, user_id=USER_A, reservation_id=RES_X, seed_block="old"
        )
        new = await conversation_repo.create(
            db, user_id=USER_A, reservation_id=RES_Y, seed_block="new"
        )
        # Backdate the old one past the TTL.
        old.last_used_at = datetime.now(UTC) - timedelta(hours=48)
        await db.commit()

        deleted = await conversation_repo.expire_idle(db, ttl_hours=24)
    assert deleted == 1
    async with TestSessionLocal() as db:
        remaining = (await db.execute(select(AssistantConversation))).scalars().all()
    assert {r.id for r in remaining} == {new.id}


@pytest.mark.asyncio
async def test_expire_idle_returns_zero_when_all_fresh():
    async with TestSessionLocal() as db:
        await conversation_repo.create(db, user_id=USER_A, reservation_id=RES_X, seed_block="fresh")
        deleted = await conversation_repo.expire_idle(db, ttl_hours=24)
    assert deleted == 0
