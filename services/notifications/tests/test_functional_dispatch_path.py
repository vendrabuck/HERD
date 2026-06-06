"""Functional test: a NATS message drives the full real dispatch path.

This goes one level above the per-dispatcher unit tests: it runs an actual
process_message -> handle_event -> default_dispatchers() flow against an
in-memory SQLite DB, with only the outbound transports (SMTP, httpx) stubbed.
It asserts the feature's end-to-end behavior through the service's real
machinery:

- in-app row persisted and each enabled outbound channel sent once;
- a redelivery (same JetStream stream sequence) is fully idempotent across every
  channel: no second in-app row, no second email/chat/webhook send;
- channel selection honors the user's preferences.
"""

import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.database import Base
from app.models.notification import Notification
from app.models.outbound_delivery import OutboundDelivery
from app.schemas.preferences import NotificationPreferences
from app.services import nats_consumer
from app.services.contact_client import ContactClient, UserContact, set_contact_client
from app.services.preferences_client import PreferencesClient, set_preferences_client
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"
_engine = create_async_engine(TEST_DATABASE_URL, echo=False)
_SessionLocal = async_sessionmaker(_engine, expire_on_commit=False)


def _session_factory():
    class _Ctx:
        async def __aenter__(self):
            self._s = _SessionLocal()
            return self._s

        async def __aexit__(self, *args):
            await self._s.close()

    return _Ctx()


@pytest.fixture(autouse=True)
async def _db():
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


class _StubPrefs(PreferencesClient):
    def __init__(self, prefs):
        self._prefs = prefs

    async def get(self, user_id):
        return self._prefs

    def invalidate(self, user_id):
        pass


class _StubContacts(ContactClient):
    def __init__(self):
        pass

    async def get(self, user_id):
        return UserContact(user_id=user_id, email="user@example.com", username="alice")

    def invalidate(self, user_id):
        pass


@pytest.fixture(autouse=True)
def _wire_clients():
    set_preferences_client(
        _StubPrefs(
            NotificationPreferences(
                channels={"in_app": True, "email": True, "chat": True, "webhook": True},
                events={},
            )
        )
    )
    set_contact_client(_StubContacts())
    yield
    set_preferences_client(None)
    set_contact_client(None)


class _FakeMsg:
    def __init__(self, data, num_delivered=1, stream_seq=None, stream="HERD_RESERVATIONS"):
        self.data = data
        seq = type("Seq", (), {"stream": stream_seq}) if stream_seq is not None else None
        self.metadata = type(
            "M", (), {"num_delivered": num_delivered, "sequence": seq, "stream": stream}
        )
        self.ack = AsyncMock()
        self.nak = AsyncMock()


def _payload(user_id):
    return json.dumps(
        {
            "event": "reservation.created",
            "user_id": str(user_id),
            "device_ids": [str(uuid.uuid4())],
            "end_time": "2026-04-21T00:00:00+00:00",
        }
    ).encode()


def _http_stub():
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    client = AsyncMock()
    client.post.return_value = resp
    client.__aenter__.return_value = client
    return client


async def _counts(user_id):
    async with _SessionLocal() as session:
        notif = (
            (await session.execute(select(Notification).where(Notification.user_id == user_id)))
            .scalars()
            .all()
        )
        ledger = (
            (
                await session.execute(
                    select(OutboundDelivery).where(OutboundDelivery.user_id == user_id)
                )
            )
            .scalars()
            .all()
        )
    return len(notif), {row.channel for row in ledger}


@pytest.mark.asyncio
async def test_full_fanout_then_redelivery_is_idempotent():
    user_id = uuid.uuid4()
    js = AsyncMock()
    email_cfg = {"smtp_host": "smtp.test", "email_from": "herd@test"}
    chat_url = "http://chat/hook"
    wh_cfg = {"outbound_webhook_url": "http://hook", "webhook_signing_secret": "s3cr3t"}

    # The chat and webhook dispatchers share the same httpx module, so a single
    # patch on httpx.AsyncClient covers both. Each post() call counts toward the
    # shared stub: chat + webhook = 2 on first delivery, 0 more on redelivery.
    http_client = _http_stub()

    with (
        patch.multiple("app.services.dispatchers.email.settings", **email_cfg),
        patch("app.services.dispatchers.email._send_smtp") as smtp,
        patch("app.services.dispatchers.chat.settings.chat_webhook_url", chat_url),
        patch.multiple("app.services.dispatchers.webhook.settings", **wh_cfg),
        patch("httpx.AsyncClient", return_value=http_client),
    ):
        first = _FakeMsg(_payload(user_id), num_delivered=1, stream_seq=11)
        second = _FakeMsg(_payload(user_id), num_delivered=2, stream_seq=11)

        r1 = await nats_consumer.process_message(
            first, js, nats_consumer.handle_event, _session_factory
        )
        r2 = await nats_consumer.process_message(
            second, js, nats_consumer.handle_event, _session_factory
        )

    assert r1 == "ack" and r2 == "ack"
    # Email fired once; the two HTTP-based channels (chat + webhook) fired once
    # each on the first delivery and not at all on the redelivery.
    assert smtp.call_count == 1
    assert http_client.post.await_count == 2
    # One in-app row; ledger has all three outbound channels.
    notif_count, channels = await _counts(user_id)
    assert notif_count == 1
    assert channels == {"email", "chat", "webhook"}
