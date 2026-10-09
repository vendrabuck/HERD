"""Unit tests for multi-channel fan-out through the consumer.

Acceptance criteria covered:
- an event is delivered to in-app plus any channels the recipient enabled,
  each via its own dispatcher;
- a transport failure on one outbound channel does not prevent delivery on the
  others, proven through the production dispatchers (issue #1135);
- channel selection honors per-channel preference toggles;
- default_dispatchers() includes all four channels in order.
"""

import logging
import uuid
from unittest.mock import MagicMock, patch

import httpx
import pytest
from app.database import Base
from app.models.notification import Notification
from app.models.outbound_delivery import OutboundDelivery
from app.schemas.preferences import NotificationPreferences
from app.services import nats_consumer
from app.services.contact_client import ContactClient, UserContact, set_contact_client
from app.services.dispatchers import default_dispatchers
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


class _StubPrefsClient(PreferencesClient):
    def __init__(self, prefs: NotificationPreferences):
        self._prefs = prefs

    async def get(self, user_id):
        return self._prefs

    def invalidate(self, user_id):
        pass


class _RecordingDispatcher:
    def __init__(self, channel, fail=False):
        self.channel = channel
        self.fail = fail
        self.sent = []

    async def send(self, session_factory, message):
        if self.fail:
            raise RuntimeError(f"{self.channel} transport down")
        self.sent.append(message)


def _event(user_id=None):
    return {
        "event": "reservation.created",
        "user_id": str(user_id or uuid.uuid4()),
        "device_ids": [str(uuid.uuid4())],
        "end_time": "2026-04-21T00:00:00+00:00",
    }


def test_default_dispatchers_lists_all_channels_in_app_first():
    channels = [d.channel for d in default_dispatchers()]
    assert channels == ["in_app", "email", "chat", "webhook"]


@pytest.mark.asyncio
async def test_event_fans_out_to_all_enabled_channels():
    set_preferences_client(
        _StubPrefsClient(
            NotificationPreferences(
                channels={"in_app": True, "email": True, "chat": True, "webhook": True},
                events={},
            )
        )
    )
    dispatchers = [
        _RecordingDispatcher("in_app"),
        _RecordingDispatcher("email"),
        _RecordingDispatcher("chat"),
        _RecordingDispatcher("webhook"),
    ]
    await nats_consumer.handle_event(_event(), _session_factory, dispatchers=dispatchers)
    set_preferences_client(None)
    assert all(len(d.sent) == 1 for d in dispatchers)


@pytest.mark.asyncio
async def test_only_enabled_channels_receive():
    set_preferences_client(
        _StubPrefsClient(
            NotificationPreferences(
                channels={"in_app": True, "email": True, "chat": False, "webhook": False},
                events={},
            )
        )
    )
    in_app = _RecordingDispatcher("in_app")
    email = _RecordingDispatcher("email")
    chat = _RecordingDispatcher("chat")
    webhook = _RecordingDispatcher("webhook")
    await nats_consumer.handle_event(
        _event(), _session_factory, dispatchers=[in_app, email, chat, webhook]
    )
    set_preferences_client(None)
    assert len(in_app.sent) == 1
    assert len(email.sent) == 1
    assert chat.sent == []
    assert webhook.sent == []


CHAT_URL = "http://chat.test/hook"
WEBHOOK_URL = "http://webhook.test/hook"


class _FakeHttpx:
    """Stands in for httpx.AsyncClient in the chat and webhook dispatchers.

    Both modules use the one httpx module, so one fake serves both and tells the
    channels apart by URL. A post to `fail_url` raises a transport error, as an
    unreachable endpoint would.
    """

    def __init__(self, fail_url=None):
        self.fail_url = fail_url
        self.posted: list[str] = []

    def __call__(self, *args, **kwargs):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, **kwargs):
        if url == self.fail_url:
            raise httpx.ConnectError("transport down")
        self.posted.append(url)
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        return resp


class _StubContactClient(ContactClient):
    async def get(self, user_id):
        return UserContact(user_id=user_id, email="user@example.com", username="alice")

    def invalidate(self, user_id):
        pass


async def _ledger_channels() -> set[str]:
    async with _SessionLocal() as session:
        rows = (await session.execute(select(OutboundDelivery))).scalars().all()
    return {r.channel for r in rows}


async def _in_app_count(user_id) -> int:
    async with _SessionLocal() as session:
        rows = (
            (await session.execute(select(Notification).where(Notification.user_id == user_id)))
            .scalars()
            .all()
        )
    return len(rows)


@pytest.mark.asyncio
@pytest.mark.parametrize("failing", ["email", "chat", "webhook"])
async def test_one_channel_failure_does_not_block_others(failing, caplog):
    """A transport error on one outbound channel is swallowed by the REAL dispatcher.

    Runs handle_event with the production default_dispatchers() and every channel
    configured. handle_event itself does not isolate (`_dispatch` awaits each
    send bare), so this passes only because each outbound dispatcher hands its
    send to run_outbound, which logs `outbound_dispatch_failed`, releases the
    ledger claim, and returns (issue #1135). Asserted: handle_event does not
    raise, every other channel delivered, and the failed channel left no claim.
    """
    user_id = uuid.uuid4()
    set_preferences_client(
        _StubPrefsClient(
            NotificationPreferences(
                channels={"in_app": True, "email": True, "chat": True, "webhook": True},
                events={},
            )
        )
    )
    set_contact_client(_StubContactClient())
    fail_url = {"chat": CHAT_URL, "webhook": WEBHOOK_URL}.get(failing)
    fake_httpx = _FakeHttpx(fail_url=fail_url)
    smtp = MagicMock(side_effect=OSError("smtp down") if failing == "email" else None)
    cfg = {
        "smtp_host": "smtp.test",
        "email_from": "herd@test",
        "chat_webhook_url": CHAT_URL,
        "outbound_webhook_url": WEBHOOK_URL,
        "webhook_signing_secret": "s3cret",
    }
    try:
        with (
            patch.multiple("app.config.settings", **cfg),
            patch("app.services.dispatchers.email._send_smtp", smtp),
            patch("app.services.dispatchers.chat.httpx.AsyncClient", fake_httpx),
            caplog.at_level(logging.ERROR, logger="app.services.dispatchers.outbound"),
        ):
            # Must not raise: a raise here would NAK the event and skip later channels.
            await nats_consumer.handle_event(
                _event(user_id), _session_factory, dedupe_key="HERD_RESERVATIONS:42"
            )
    finally:
        set_preferences_client(None)
        set_contact_client(None)

    assert await _in_app_count(user_id) == 1
    smtp.assert_called_once()
    expected_posts = [
        url for ch, url in (("chat", CHAT_URL), ("webhook", WEBHOOK_URL)) if ch != failing
    ]
    assert fake_httpx.posted == expected_posts
    # The failed channel's claim was released so a redelivery retries it; the
    # delivered channels keep theirs.
    assert await _ledger_channels() == {"email", "chat", "webhook"} - {failing}
    failures = [
        r for r in caplog.records if getattr(r, "action", None) == "outbound_dispatch_failed"
    ]
    assert [r.channel for r in failures] == [failing]
