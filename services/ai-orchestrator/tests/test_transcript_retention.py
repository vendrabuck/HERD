"""Idle-conversation retention for purpose classification (issues #1039, #1067).

The sweeper keeps an idle conversation while its reservation is not terminal,
or is terminal with the end-of-reservation classification still pending, and
only while this service would send transcripts to the classifier. The answer
comes from reservations' internal GET and fails closed.
"""

import json
import logging
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from app.config import settings
from app.database import Base
from app.models.conversation import AssistantConversation
from app.services import conversation_repo, transcript_retention
from herd_common.logging import JSONFormatter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

TOKEN = "retention-internal-token"
BASE = settings.reservations_service_url.rstrip("/")

engine = create_async_engine("sqlite+aiosqlite:///:memory:")
TestSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
async def setup_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture(autouse=True)
def _classifier_reads_transcripts(monkeypatch):
    monkeypatch.setattr(settings, "ai_purpose_classification_enabled", True)
    monkeypatch.setattr(settings, "ai_purpose_include_transcripts", True)
    monkeypatch.setattr(settings, "internal_api_token", TOKEN)
    monkeypatch.setattr(settings, "assistant_conversation_ttl_hours", 24)


def _status(status: str, pending: bool | None = None) -> httpx.Response:
    body: dict = {"id": str(uuid.uuid4()), "status": status, "is_active": False}
    if pending is not None:
        body["purpose_classification_pending"] = pending
    return httpx.Response(200, json=body)


async def _idle_conversation(reservation_id: uuid.UUID, *, age_hours: float = 48) -> uuid.UUID:
    async with TestSessionLocal() as db:
        conv = await conversation_repo.create(
            db, user_id=uuid.uuid4(), reservation_id=reservation_id, seed_block="seed"
        )
        conv.last_used_at = datetime.now(UTC) - timedelta(hours=age_hours)
        await db.commit()
        return conv.id


async def _remaining() -> set[uuid.UUID]:
    async with TestSessionLocal() as db:
        rows = (await db.execute(select(AssistantConversation.id))).scalars().all()
    return set(rows)


async def _sweep() -> int:
    async with TestSessionLocal() as db:
        return await conversation_repo.expire_idle(db)


# --- The lookup ----------------------------------------------------------


@pytest.mark.parametrize("status", ["PENDING", "PENDING_PROVISION", "ACTIVE"])
async def test_a_live_reservation_keeps_its_transcript(status):
    rid = uuid.uuid4()
    with respx.mock(assert_all_called=True) as mock:
        route = mock.get(f"{BASE}/internal/{rid}").mock(return_value=_status(status))
        assert await transcript_retention.reservation_keeps_transcript(rid) is True
    assert route.calls.last.request.headers["x-internal-token"] == TOKEN


@pytest.mark.parametrize("status", ["COMPLETED", "CANCELLED", "FAILED"])
async def test_a_terminal_reservation_awaiting_classification_keeps_its_transcript(status):
    rid = uuid.uuid4()
    with respx.mock(assert_all_called=True) as mock:
        mock.get(f"{BASE}/internal/{rid}").mock(return_value=_status(status, pending=True))
        assert await transcript_retention.reservation_keeps_transcript(rid) is True


@pytest.mark.parametrize("status", ["COMPLETED", "CANCELLED", "FAILED"])
async def test_a_terminal_reservation_already_classified_releases_its_transcript(status):
    rid = uuid.uuid4()
    with respx.mock(assert_all_called=True) as mock:
        mock.get(f"{BASE}/internal/{rid}").mock(return_value=_status(status, pending=False))
        assert await transcript_retention.reservation_keeps_transcript(rid) is False


async def test_an_unknown_reservation_releases_its_transcript():
    rid = uuid.uuid4()
    with respx.mock(assert_all_called=True) as mock:
        mock.get(f"{BASE}/internal/{rid}").mock(
            return_value=httpx.Response(404, json={"detail": "Reservation not found"})
        )
        assert await transcript_retention.reservation_keeps_transcript(rid) is False


@pytest.mark.parametrize(
    ("response", "why"),
    [
        (httpx.Response(500, json={"detail": "boom"}), "5xx"),
        (httpx.Response(503, text="unavailable"), "503"),
        (httpx.Response(403, json={"detail": "Invalid internal token"}), "403"),
        (httpx.Response(200, text="not json"), "body not JSON"),
        (httpx.Response(200, json=["COMPLETED"]), "body not an object"),
        (httpx.Response(200, json={"status": "SOMETHING_NEW"}), "unknown status"),
        (httpx.Response(200, json={"status": "COMPLETED"}), "pending flag missing"),
        (
            httpx.Response(200, json={"status": "COMPLETED", "purpose_classification_pending": 1}),
            "pending flag not a boolean",
        ),
    ],
)
async def test_an_unclear_answer_keeps_the_transcript(response, why):
    rid = uuid.uuid4()
    with respx.mock(assert_all_called=True) as mock:
        mock.get(f"{BASE}/internal/{rid}").mock(return_value=response)
        assert await transcript_retention.reservation_keeps_transcript(rid) is True, why


async def test_an_unreachable_reservations_service_keeps_the_transcript():
    rid = uuid.uuid4()
    with respx.mock(assert_all_called=True) as mock:
        mock.get(f"{BASE}/internal/{rid}").mock(side_effect=httpx.ConnectError("refused"))
        assert await transcript_retention.reservation_keeps_transcript(rid) is True


async def test_a_missing_internal_token_keeps_the_transcript(monkeypatch):
    monkeypatch.setattr(settings, "internal_api_token", "")
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(url__regex=rf"{BASE}/internal/.*")
        assert await transcript_retention.reservation_keeps_transcript(uuid.uuid4()) is True
    assert route.call_count == 0


async def test_a_failed_lookup_is_logged_by_reason_and_status(caplog):
    rid = uuid.uuid4()
    with caplog.at_level(logging.WARNING, logger="app.services.transcript_retention"):
        with respx.mock(assert_all_called=True) as mock:
            mock.get(f"{BASE}/internal/{rid}").mock(
                return_value=httpx.Response(502, json={"detail": "upstream detail text"})
            )
            await transcript_retention.reservation_keeps_transcript(rid)

    records = [
        r for r in caplog.records if r.getMessage() == "conversation_retention_lookup_failed"
    ]
    assert len(records) == 1
    formatted = JSONFormatter("ai-orchestrator").format(records[0])
    body = json.loads(formatted)
    assert body["reservation_id"] == str(rid)
    assert body["reason"] == "status"
    assert body["status_code"] == 502
    assert "upstream detail text" not in formatted


# --- The sweep -----------------------------------------------------------


async def test_the_sweep_keeps_only_what_the_classifier_still_owes():
    live, pending, classified, gone, down = (uuid.uuid4() for _ in range(5))
    keep_live = await _idle_conversation(live)
    keep_pending = await _idle_conversation(pending)
    drop_classified = await _idle_conversation(classified)
    drop_gone = await _idle_conversation(gone)
    keep_down = await _idle_conversation(down)
    fresh = await _idle_conversation(classified, age_hours=1)

    with respx.mock(assert_all_called=True) as mock:
        mock.get(f"{BASE}/internal/{live}").mock(return_value=_status("ACTIVE"))
        mock.get(f"{BASE}/internal/{pending}").mock(return_value=_status("COMPLETED", pending=True))
        mock.get(f"{BASE}/internal/{classified}").mock(
            return_value=_status("CANCELLED", pending=False)
        )
        mock.get(f"{BASE}/internal/{gone}").mock(return_value=httpx.Response(404))
        mock.get(f"{BASE}/internal/{down}").mock(side_effect=httpx.ReadTimeout("slow"))
        deleted = await _sweep()

    assert deleted == 2
    assert drop_classified not in await _remaining()
    assert drop_gone not in await _remaining()
    assert await _remaining() == {keep_live, keep_pending, keep_down, fresh}


async def test_the_sweep_releases_a_transcript_whose_classification_hit_the_attempt_cap():
    """Issue #1067: once the purpose sweep has used up a terminal reservation's
    attempts without a suggestion, reservations reports it not pending (only the
    manual Classify now, or a backfill reset, can try it again), so its idle
    conversation is released instead of being kept forever. The body is the full
    internal status reservations answers for such a row."""
    rid = uuid.uuid4()
    conv_id = await _idle_conversation(rid)
    capped = {
        "id": str(rid),
        "status": "COMPLETED",
        "is_active": False,
        "start_time": "2026-10-01T10:00:00+00:00",
        "end_time": "2026-10-01T12:00:00+00:00",
        "purpose_classification_pending": False,
    }

    with respx.mock(assert_all_called=True) as mock:
        route = mock.get(f"{BASE}/internal/{rid}").mock(
            return_value=httpx.Response(200, json=capped)
        )
        assert await _sweep() == 1

    assert route.call_count == 1
    assert conv_id not in await _remaining()


async def test_the_sweep_asks_once_per_reservation():
    rid = uuid.uuid4()
    await _idle_conversation(rid)
    await _idle_conversation(rid)

    with respx.mock(assert_all_called=True) as mock:
        route = mock.get(f"{BASE}/internal/{rid}").mock(return_value=_status("ACTIVE"))
        assert await _sweep() == 0

    assert route.call_count == 1
    assert len(await _remaining()) == 2


@pytest.mark.parametrize(
    ("classification", "transcripts"),
    [(False, True), (True, False), (False, False)],
)
async def test_without_a_transcript_reader_the_plain_ttl_applies_with_no_lookup(
    monkeypatch, classification, transcripts
):
    monkeypatch.setattr(settings, "ai_purpose_classification_enabled", classification)
    monkeypatch.setattr(settings, "ai_purpose_include_transcripts", transcripts)
    await _idle_conversation(uuid.uuid4())

    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(url__regex=rf"{BASE}/internal/.*")
        assert await _sweep() == 1

    assert route.call_count == 0
    assert await _remaining() == set()


async def test_a_conversation_used_during_the_lookups_is_not_deleted():
    rid = uuid.uuid4()
    conv_id = await _idle_conversation(rid)

    async def keep_reservation(_rid: uuid.UUID) -> bool:
        async with TestSessionLocal() as other:
            conv = await other.get(AssistantConversation, conv_id)
            conv.last_used_at = datetime.now(UTC)
            await other.commit()
        return False

    async with TestSessionLocal() as db:
        deleted = await conversation_repo.expire_idle(db, keep_reservation=keep_reservation)

    assert deleted == 0
    assert await _remaining() == {conv_id}
