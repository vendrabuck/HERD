"""Unit tests for the email dispatcher internals.

The broader outbound suite stubs `_send_smtp` wholesale, so the actual
message-construction and SMTP-transport branches inside `_send_smtp` and the
no-recipient guard in `EmailDispatcher.send` are never exercised. These tests
drive those paths with a fully stubbed `smtplib.SMTP` so no real connection is
ever opened.
"""

import uuid
from unittest.mock import MagicMock, patch

import pytest
from app.database import Base
from app.services.contact_client import ContactClient, UserContact, set_contact_client
from app.services.dispatchers.base import DispatchMessage
from app.services.dispatchers.email import EmailDispatcher, _send_smtp
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


class _StubContactClient(ContactClient):
    def __init__(self, contact):
        self._contact = contact

    async def get(self, user_id):
        return self._contact

    def invalidate(self, user_id):
        pass


def _msg(user_id=None, dedupe_key="HERD_RESERVATIONS:1"):
    return DispatchMessage(
        user_id=user_id or uuid.uuid4(),
        event_type="reservation.created",
        title="Reservation confirmed",
        body="body text",
        data={"reservation_id": "r1"},
        dedupe_key=dedupe_key,
    )


# --- _send_smtp: message construction + transport branches ---


def _smtp_settings(**overrides):
    base = {
        "smtp_host": "smtp.test",
        "smtp_port": 2525,
        "smtp_username": "",
        "smtp_password": "",
        "smtp_use_tls": False,
        "smtp_timeout_seconds": 7.0,
        "email_from": "herd@test",
    }
    base.update(overrides)
    return base


def test_send_smtp_builds_message_and_sends_plain():
    """No TLS, no auth: the message is built from settings and send_message is called."""
    server = MagicMock()
    cm = MagicMock()
    cm.__enter__.return_value = server
    cm.__exit__.return_value = False

    with patch.multiple("app.services.dispatchers.email.settings", **_smtp_settings()):
        with patch("app.services.dispatchers.email.smtplib.SMTP", return_value=cm) as smtp:
            _send_smtp("dest@example.com", "Subject line", "Body content")

    # Connected with host/port/timeout from settings.
    smtp.assert_called_once_with("smtp.test", 2525, timeout=7.0)
    # No TLS, no login on this config.
    server.starttls.assert_not_called()
    server.login.assert_not_called()
    server.send_message.assert_called_once()

    sent_msg = server.send_message.call_args[0][0]
    assert sent_msg["From"] == "herd@test"
    assert sent_msg["To"] == "dest@example.com"
    assert sent_msg["Subject"] == "Subject line"
    assert sent_msg.get_content().rstrip("\n") == "Body content"


def test_send_smtp_starttls_and_login_when_configured():
    """TLS + credentials: starttls and login are both invoked before send."""
    server = MagicMock()
    cm = MagicMock()
    cm.__enter__.return_value = server
    cm.__exit__.return_value = False

    cfg = _smtp_settings(smtp_use_tls=True, smtp_username="bot", smtp_password="pw")
    with patch.multiple("app.services.dispatchers.email.settings", **cfg):
        with patch("app.services.dispatchers.email.smtplib.SMTP", return_value=cm):
            _send_smtp("dest@example.com", "Subj", "Body")

    server.starttls.assert_called_once()
    server.login.assert_called_once_with("bot", "pw")
    server.send_message.assert_called_once()


def test_send_smtp_tls_without_credentials_skips_login():
    server = MagicMock()
    cm = MagicMock()
    cm.__enter__.return_value = server
    cm.__exit__.return_value = False

    cfg = _smtp_settings(smtp_use_tls=True, smtp_username="", smtp_password="")
    with patch.multiple("app.services.dispatchers.email.settings", **cfg):
        with patch("app.services.dispatchers.email.smtplib.SMTP", return_value=cm):
            _send_smtp("dest@example.com", "Subj", "Body")

    server.starttls.assert_called_once()
    server.login.assert_not_called()


# --- EmailDispatcher.send: no-recipient guard ---


@pytest.mark.asyncio
async def test_send_skips_when_contact_is_none():
    """A user with no resolvable contact skips the channel and never sends."""
    set_contact_client(_StubContactClient(None))
    cfg = {"smtp_host": "smtp.test", "email_from": "herd@test"}
    try:
        with patch.multiple("app.services.dispatchers.email.settings", **cfg):
            with patch("app.services.dispatchers.email._send_smtp") as send:
                await EmailDispatcher().send(_session_factory, _msg())
        send.assert_not_called()
    finally:
        set_contact_client(None)


@pytest.mark.asyncio
async def test_send_skips_when_contact_has_no_email():
    """A contact with an empty email also skips the channel."""
    set_contact_client(
        _StubContactClient(UserContact(user_id=uuid.uuid4(), email="", username="alice"))
    )
    cfg = {"smtp_host": "smtp.test", "email_from": "herd@test"}
    try:
        with patch.multiple("app.services.dispatchers.email.settings", **cfg):
            with patch("app.services.dispatchers.email._send_smtp") as send:
                await EmailDispatcher().send(_session_factory, _msg())
        send.assert_not_called()
    finally:
        set_contact_client(None)


@pytest.mark.asyncio
async def test_send_runs_send_smtp_in_thread_when_configured():
    """End-to-end through the real _do_send: _send_smtp is invoked with the
    resolved recipient + message title/body, via asyncio.to_thread."""
    set_contact_client(
        _StubContactClient(
            UserContact(user_id=uuid.uuid4(), email="to@example.com", username="alice")
        )
    )
    cfg = {"smtp_host": "smtp.test", "email_from": "herd@test"}
    try:
        with patch.multiple("app.services.dispatchers.email.settings", **cfg):
            with patch("app.services.dispatchers.email._send_smtp") as send:
                await EmailDispatcher().send(_session_factory, _msg(dedupe_key=None))
        send.assert_called_once_with("to@example.com", "Reservation confirmed", "body text")
    finally:
        set_contact_client(None)
