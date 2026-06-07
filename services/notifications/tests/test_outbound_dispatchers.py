"""Unit tests for the outbound dispatchers (email, chat, webhook).

Covers, per the issue's acceptance criteria:
- each dispatcher delivers via its own transport when configured;
- a NATS redelivery (same dedupe_key) does not resend (ledger dedupe);
- an unconfigured channel is a clean no-op;
- the webhook is HMAC-signed over the exact body bytes.

Transports are stubbed so no network or SMTP server is required. The
in-memory SQLite session factory mirrors the other notifications suites.
"""

import hashlib
import hmac
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.database import Base
from app.models.outbound_delivery import OutboundDelivery
from app.services.contact_client import ContactClient, UserContact, set_contact_client
from app.services.dispatchers.base import DispatchMessage
from app.services.dispatchers.chat import ChatDispatcher
from app.services.dispatchers.email import EmailDispatcher
from app.services.dispatchers.outbound import _release, run_outbound
from app.services.dispatchers.webhook import WebhookDispatcher, sign_body
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


class _StubContactClient(ContactClient):
    def __init__(self, contact: UserContact | None):
        self._contact = contact

    async def get(self, user_id):
        return self._contact

    def invalidate(self, user_id):
        pass


@pytest.fixture(autouse=True)
def _contact():
    set_contact_client(
        _StubContactClient(
            UserContact(user_id=uuid.uuid4(), email="user@example.com", username="alice")
        )
    )
    yield
    set_contact_client(None)


def _msg(user_id=None, dedupe_key="HERD_RESERVATIONS:1"):
    return DispatchMessage(
        user_id=user_id or uuid.uuid4(),
        event_type="reservation.created",
        title="Reservation confirmed",
        body="body text",
        data={"reservation_id": "r1"},
        dedupe_key=dedupe_key,
    )


async def _ledger_count(channel: str) -> int:
    async with _SessionLocal() as session:
        rows = (
            (
                await session.execute(
                    select(OutboundDelivery).where(OutboundDelivery.channel == channel)
                )
            )
            .scalars()
            .all()
        )
    return len(rows)


# --- Email ---


@pytest.mark.asyncio
async def test_email_sends_when_configured():
    cfg = {"smtp_host": "smtp.test", "email_from": "herd@test"}
    with patch.multiple("app.services.dispatchers.email.settings", **cfg):
        with patch("app.services.dispatchers.email._send_smtp") as send:
            await EmailDispatcher().send(_session_factory, _msg())
    send.assert_called_once()
    args = send.call_args[0]
    assert args[0] == "user@example.com"
    assert args[1] == "Reservation confirmed"
    assert await _ledger_count("email") == 1


@pytest.mark.asyncio
async def test_email_noop_when_not_configured():
    with patch.multiple("app.services.dispatchers.email.settings", smtp_host="", email_from=""):
        with patch("app.services.dispatchers.email._send_smtp") as send:
            await EmailDispatcher().send(_session_factory, _msg())
    send.assert_not_called()
    assert await _ledger_count("email") == 0


@pytest.mark.asyncio
async def test_email_redelivery_does_not_resend():
    cfg = {"smtp_host": "smtp.test", "email_from": "herd@test"}
    user_id = uuid.uuid4()
    with patch.multiple("app.services.dispatchers.email.settings", **cfg):
        with patch("app.services.dispatchers.email._send_smtp") as send:
            await EmailDispatcher().send(_session_factory, _msg(user_id, "HERD_RESERVATIONS:7"))
            await EmailDispatcher().send(_session_factory, _msg(user_id, "HERD_RESERVATIONS:7"))
    assert send.call_count == 1
    assert await _ledger_count("email") == 1


@pytest.mark.asyncio
async def test_email_send_failure_releases_ledger_for_retry():
    cfg = {"smtp_host": "smtp.test", "email_from": "herd@test"}
    user_id = uuid.uuid4()
    with patch.multiple("app.services.dispatchers.email.settings", **cfg):
        with patch(
            "app.services.dispatchers.email._send_smtp",
            side_effect=OSError("smtp down"),
        ):
            # Must not raise: failure is isolated.
            await EmailDispatcher().send(_session_factory, _msg(user_id, "HERD_RESERVATIONS:9"))
    # Ledger row was released so a later redelivery retries.
    assert await _ledger_count("email") == 0


# --- Chat ---


@pytest.mark.asyncio
async def test_chat_posts_when_configured():
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    client = AsyncMock()
    client.post.return_value = resp
    client.__aenter__.return_value = client
    with patch("app.services.dispatchers.chat.settings.chat_webhook_url", "http://chat/hook"):
        with patch("app.services.dispatchers.chat.httpx.AsyncClient", return_value=client):
            await ChatDispatcher().send(_session_factory, _msg())
    client.post.assert_awaited_once()
    _, kwargs = client.post.call_args
    assert "alice" in kwargs["json"]["text"]
    assert await _ledger_count("chat") == 1


@pytest.mark.asyncio
async def test_chat_noop_when_not_configured():
    with patch("app.services.dispatchers.chat.settings.chat_webhook_url", ""):
        with patch("app.services.dispatchers.chat.httpx.AsyncClient") as cli:
            await ChatDispatcher().send(_session_factory, _msg())
    cli.assert_not_called()
    assert await _ledger_count("chat") == 0


# --- Webhook ---


def test_sign_body_is_hmac_sha256_hex():
    body = b'{"a":1}'
    secret = "topsecret"
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert sign_body(body, secret) == f"sha256={expected}"


@pytest.mark.asyncio
async def test_webhook_posts_signed_when_configured():
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    client = AsyncMock()
    client.post.return_value = resp
    client.__aenter__.return_value = client
    cfg = {"outbound_webhook_url": "http://hook", "webhook_signing_secret": "s3cr3t"}
    with patch.multiple("app.services.dispatchers.webhook.settings", **cfg):
        with patch("app.services.dispatchers.webhook.httpx.AsyncClient", return_value=client):
            await WebhookDispatcher().send(_session_factory, _msg())
    client.post.assert_awaited_once()
    _, kwargs = client.post.call_args
    body = kwargs["content"]
    sig = kwargs["headers"]["X-HERD-Signature"]
    # Signature must verify against the exact bytes sent.
    assert sig == sign_body(body, "s3cr3t")
    assert await _ledger_count("webhook") == 1


@pytest.mark.asyncio
async def test_webhook_noop_when_secret_missing():
    with patch.multiple(
        "app.services.dispatchers.webhook.settings",
        outbound_webhook_url="http://hook",
        webhook_signing_secret="",
    ):
        with patch("app.services.dispatchers.webhook.httpx.AsyncClient") as cli:
            await WebhookDispatcher().send(_session_factory, _msg())
    cli.assert_not_called()
    assert await _ledger_count("webhook") == 0


@pytest.mark.asyncio
async def test_webhook_redelivery_does_not_repost():
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    client = AsyncMock()
    client.post.return_value = resp
    client.__aenter__.return_value = client
    cfg = {"outbound_webhook_url": "http://hook", "webhook_signing_secret": "s3cr3t"}
    user_id = uuid.uuid4()
    with patch.multiple("app.services.dispatchers.webhook.settings", **cfg):
        with patch("app.services.dispatchers.webhook.httpx.AsyncClient", return_value=client):
            await WebhookDispatcher().send(_session_factory, _msg(user_id, "HERD_RESERVATIONS:3"))
            await WebhookDispatcher().send(_session_factory, _msg(user_id, "HERD_RESERVATIONS:3"))
    assert client.post.await_count == 1


@pytest.mark.asyncio
async def test_null_dedupe_key_is_not_deduplicated():
    """A message with no dedupe_key (not event-sourced) sends every time and
    writes no ledger row."""
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    client = AsyncMock()
    client.post.return_value = resp
    client.__aenter__.return_value = client
    cfg = {"outbound_webhook_url": "http://hook", "webhook_signing_secret": "s3cr3t"}
    user_id = uuid.uuid4()
    with patch.multiple("app.services.dispatchers.webhook.settings", **cfg):
        with patch("app.services.dispatchers.webhook.httpx.AsyncClient", return_value=client):
            await WebhookDispatcher().send(_session_factory, _msg(user_id, dedupe_key=None))
            await WebhookDispatcher().send(_session_factory, _msg(user_id, dedupe_key=None))
    assert client.post.await_count == 2
    assert await _ledger_count("webhook") == 0


# --- run_outbound / _release directly ---


@pytest.mark.asyncio
async def test_run_outbound_releases_reserved_slot_on_send_failure():
    """A reserved ledger slot is dropped when the send raises so a later
    redelivery retries (covers the failure -> _release -> return path)."""
    msg = _msg(dedupe_key="HERD_RESERVATIONS:42")

    async def _boom():
        raise RuntimeError("transport down")

    await run_outbound(_session_factory, "email", msg, _boom)
    # The reserved row was released: nothing left in the ledger.
    assert await _ledger_count("email") == 0


@pytest.mark.asyncio
async def test_release_is_noop_for_null_dedupe_key():
    """A null dedupe_key never wrote a ledger row, so release returns early
    without touching the DB."""
    msg = _msg(dedupe_key=None)
    # Must not raise and must not write/delete anything.
    await _release(_session_factory, "email", msg)
    assert await _ledger_count("email") == 0


@pytest.mark.asyncio
async def test_release_swallows_session_failure():
    """If acquiring or using the session raises, _release logs and swallows it
    rather than crashing the consumer loop."""

    def _broken_session_factory():
        raise RuntimeError("db connection pool exhausted")

    msg = _msg(dedupe_key="HERD_RESERVATIONS:99")
    # Best-effort cleanup: the error is swallowed, not propagated.
    await _release(_broken_session_factory, "email", msg)
