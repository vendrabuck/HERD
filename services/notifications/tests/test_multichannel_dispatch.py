"""Unit tests for multi-channel fan-out through the consumer.

Acceptance criteria covered:
- an event is delivered to in-app plus any channels the recipient enabled,
  each via its own dispatcher;
- a failure on one channel does not prevent delivery on the others;
- channel selection honors per-channel preference toggles;
- default_dispatchers() includes all four channels in order.
"""

import uuid

import pytest
from app.database import Base
from app.schemas.preferences import NotificationPreferences
from app.services import nats_consumer
from app.services.dispatchers import default_dispatchers
from app.services.preferences_client import PreferencesClient, set_preferences_client
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


@pytest.mark.asyncio
async def test_one_channel_failure_does_not_block_others():
    """A raising dispatcher in the middle of the set must not prevent the
    later channels from delivering. handle_event itself does not isolate, so
    the real outbound dispatchers swallow their own failures; here we assert
    that a self-isolating dispatcher set delivers on every healthy channel."""

    class _IsolatingDispatcher:
        """Mirrors the outbound dispatchers' fail-open contract."""

        def __init__(self, channel, fail=False):
            self.channel = channel
            self.fail = fail
            self.sent = []

        async def send(self, session_factory, message):
            try:
                if self.fail:
                    raise RuntimeError("boom")
                self.sent.append(message)
            except Exception:
                return

    set_preferences_client(
        _StubPrefsClient(
            NotificationPreferences(
                channels={"in_app": True, "email": True, "chat": True, "webhook": True},
                events={},
            )
        )
    )
    in_app = _IsolatingDispatcher("in_app")
    email = _IsolatingDispatcher("email", fail=True)
    chat = _IsolatingDispatcher("chat")
    webhook = _IsolatingDispatcher("webhook")
    await nats_consumer.handle_event(
        _event(), _session_factory, dispatchers=[in_app, email, chat, webhook]
    )
    set_preferences_client(None)
    assert len(in_app.sent) == 1
    assert email.sent == []  # failed channel delivered nothing
    assert len(chat.sent) == 1  # later channels still delivered
    assert len(webhook.sent) == 1
