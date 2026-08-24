"""Unit tests for outbound webhook subscriptions and delivery (issue #33).

No real NATS and no real network: httpx is faked per test and the DB is an
in-memory SQLite engine (StaticPool so every session sees the same database).
"""

import hashlib
import hmac
import json
import uuid

import httpx
import pytest
from app.database import Base, get_db
from app.main import app
from app.models.webhook import WebhookDelivery, WebhookSubscription
from app.schemas.webhook import KNOWN_EVENT_TYPES
from app.services import delivery as delivery_mod
from app.services.delivery import Target, deliver_one, load_matching_targets
from herd_common.webhooks import WEBHOOK_SIGNATURE_HEADER, sign_body
from httpx import ASGITransport, AsyncClient
from jose import jwt
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

SECRET = "test-secret"


@pytest.fixture
async def session_factory():
    """A shared in-memory SQLite session factory with the webhook tables created."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


def _token(role: str = "admin") -> str:
    return jwt.encode({"sub": str(uuid.uuid4()), "role": role}, SECRET, algorithm="HS256")


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


class _FakeResponse:
    def __init__(self, status_code: int):
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "error", request=httpx.Request("POST", "http://x"), response=self
            )


def _install_fake_httpx(monkeypatch, *, status_code=None, raise_exc=None):
    """Replace httpx.AsyncClient in the delivery module with a fake. Returns a
    dict whose `calls` list records each POST so tests can assert send counts."""
    record = {"calls": []}

    class _FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, content=None, headers=None):
            record["calls"].append({"url": url, "content": content, "headers": headers})
            if raise_exc is not None:
                raise raise_exc
            return _FakeResponse(status_code)

    monkeypatch.setattr(delivery_mod.httpx, "AsyncClient", _FakeClient)
    return record


# --- signer ---------------------------------------------------------------


def test_sign_body_is_stable_and_verifiable():
    body = b'{"event":"reservation.created"}'
    secret = "shh"
    sig = sign_body(body, secret)
    assert sig.startswith("sha256=")
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert sig == f"sha256={expected}"
    # Deterministic.
    assert sign_body(body, secret) == sig
    # Sensitive to body and key.
    assert sign_body(b"other", secret) != sig
    assert sign_body(body, "different") != sig


# --- subscription matching ------------------------------------------------


async def test_load_matching_targets_filters_by_event_and_active(session_factory):
    async with session_factory() as s:
        s.add_all(
            [
                WebhookSubscription(
                    target_url="https://a.example/hook",
                    event_types=["reservation.created", "reservation.failed"],
                    secret="s1",
                ),
                WebhookSubscription(
                    target_url="https://b.example/hook",
                    event_types=["reservation.completed"],
                    secret="s2",
                ),
                WebhookSubscription(
                    target_url="https://c.example/hook",
                    event_types=["reservation.created"],
                    secret="s3",
                    is_active=False,
                ),
            ]
        )
        await s.commit()

    created = await load_matching_targets(session_factory, "reservation.created")
    assert {t.target_url for t in created} == {"https://a.example/hook"}

    completed = await load_matching_targets(session_factory, "reservation.completed")
    assert {t.target_url for t in completed} == {"https://b.example/hook"}


# --- delivery -------------------------------------------------------------


async def _make_target(session_factory, event_types) -> Target:
    async with session_factory() as s:
        sub = WebhookSubscription(
            target_url="https://recv.example/hook", event_types=event_types, secret="topsecret"
        )
        s.add(sub)
        await s.commit()
        return Target(id=sub.id, target_url=sub.target_url, secret=sub.secret)


async def _deliveries(session_factory, subscription_id):
    from sqlalchemy import select

    async with session_factory() as s:
        return (
            (
                await s.execute(
                    select(WebhookDelivery).where(
                        WebhookDelivery.subscription_id == subscription_id
                    )
                )
            )
            .scalars()
            .all()
        )


async def test_delivery_2xx_writes_delivered_row(session_factory, monkeypatch):
    record = _install_fake_httpx(monkeypatch, status_code=200)
    target = await _make_target(session_factory, ["reservation.created"])
    body = b'{"event":"reservation.created","event_id":"evt-1"}'

    result = await deliver_one(
        session_factory, target, body, "evt-1", "reservation.created", timeout=1.0, attempts=3
    )
    assert result == "delivered"
    rows = await _deliveries(session_factory, target.id)
    assert len(rows) == 1
    assert rows[0].status == "delivered"
    assert rows[0].response_status == 200
    assert rows[0].delivered_at is not None
    # The signed header matches the exact body bytes sent.
    assert len(record["calls"]) == 1
    sent = record["calls"][0]
    assert sent["headers"][WEBHOOK_SIGNATURE_HEADER] == sign_body(body, target.secret)
    assert sent["content"] == body


async def test_delivery_persistent_failure_retries_then_dead(session_factory, monkeypatch):
    record = _install_fake_httpx(monkeypatch, status_code=500)
    target = await _make_target(session_factory, ["reservation.created"])
    body = b'{"event":"reservation.created","event_id":"evt-2"}'

    result = await deliver_one(
        session_factory, target, body, "evt-2", "reservation.created", timeout=1.0, attempts=2
    )
    assert result == "dead"
    assert len(record["calls"]) == 2  # retried up to `attempts`
    rows = await _deliveries(session_factory, target.id)
    assert len(rows) == 1
    assert rows[0].status == "dead"
    assert rows[0].attempts == 2
    assert rows[0].response_status == 500
    assert rows[0].last_error


async def test_delivery_connection_error_is_retried_then_dead(session_factory, monkeypatch):
    record = _install_fake_httpx(monkeypatch, raise_exc=httpx.ConnectError("refused"))
    target = await _make_target(session_factory, ["reservation.created"])
    body = b'{"event":"reservation.created","event_id":"evt-3"}'

    result = await deliver_one(
        session_factory, target, body, "evt-3", "reservation.created", timeout=1.0, attempts=2
    )
    assert result == "dead"
    assert len(record["calls"]) == 2
    rows = await _deliveries(session_factory, target.id)
    assert rows[0].status == "dead"


async def test_redelivered_event_with_delivered_row_is_skipped(session_factory, monkeypatch):
    record = _install_fake_httpx(monkeypatch, status_code=200)
    target = await _make_target(session_factory, ["reservation.created"])
    # Pre-seed a delivered ledger row for this (subscription, event).
    async with session_factory() as s:
        s.add(
            WebhookDelivery(
                subscription_id=target.id,
                event_id="evt-dup",
                event_type="reservation.created",
                status="delivered",
                attempts=1,
                response_status=200,
            )
        )
        await s.commit()

    result = await deliver_one(
        session_factory,
        target,
        b"{}",
        "evt-dup",
        "reservation.created",
        timeout=1.0,
        attempts=3,
    )
    assert result == "skipped"
    assert record["calls"] == []  # no second POST
    rows = await _deliveries(session_factory, target.id)
    assert len(rows) == 1  # no duplicate ledger row


# --- CRUD -----------------------------------------------------------------


def _client(session_factory) -> AsyncClient:
    async def _override_get_db():
        async with session_factory() as s:
            yield s

    app.dependency_overrides[get_db] = _override_get_db
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    app.dependency_overrides.clear()


async def test_create_requires_admin(session_factory):
    async with _client(session_factory) as c:
        resp = await c.post(
            "/webhooks",
            json={"target_url": "https://x.example/h", "event_types": ["reservation.created"]},
            headers=_auth(_token(role="user")),
        )
    assert resp.status_code == 403


async def test_create_generates_secret_when_omitted(session_factory):
    async with _client(session_factory) as c:
        resp = await c.post(
            "/webhooks",
            json={"target_url": "https://x.example/h", "event_types": ["reservation.created"]},
            headers=_auth(_token()),
        )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["secret"]  # generated
    assert len(body["secret"]) >= 20


async def test_create_accepts_explicit_secret(session_factory):
    async with _client(session_factory) as c:
        resp = await c.post(
            "/webhooks",
            json={
                "target_url": "https://x.example/h",
                "event_types": ["reservation.created"],
                "secret": "password",
            },
            headers=_auth(_token()),
        )
    assert resp.status_code == 201
    assert resp.json()["secret"] == "password"


async def test_create_rejects_unknown_event_type(session_factory):
    async with _client(session_factory) as c:
        resp = await c.post(
            "/webhooks",
            json={"target_url": "https://x.example/h", "event_types": ["reservation.exploded"]},
            headers=_auth(_token()),
        )
    assert resp.status_code == 422


async def test_create_rejects_non_http_url(session_factory):
    async with _client(session_factory) as c:
        resp = await c.post(
            "/webhooks",
            json={"target_url": "ftp://x.example/h", "event_types": ["reservation.created"]},
            headers=_auth(_token()),
        )
    assert resp.status_code == 422


async def test_list_omits_secret_and_get_404(session_factory):
    async with _client(session_factory) as c:
        created = await c.post(
            "/webhooks",
            json={"target_url": "https://x.example/h", "event_types": ["reservation.created"]},
            headers=_auth(_token()),
        )
        wid = created.json()["id"]

        listed = await c.get("/webhooks", headers=_auth(_token()))
        assert listed.status_code == 200
        assert all("secret" not in item for item in listed.json())

        got = await c.get(f"/webhooks/{wid}", headers=_auth(_token()))
        assert got.status_code == 200
        assert "secret" not in got.json()

        missing = await c.get(f"/webhooks/{uuid.uuid4()}", headers=_auth(_token()))
        assert missing.status_code == 404


async def test_delete_returns_204(session_factory):
    async with _client(session_factory) as c:
        created = await c.post(
            "/webhooks",
            json={"target_url": "https://x.example/h", "event_types": ["reservation.created"]},
            headers=_auth(_token()),
        )
        wid = created.json()["id"]
        deleted = await c.delete(f"/webhooks/{wid}", headers=_auth(_token()))
        assert deleted.status_code == 204
        again = await c.get(f"/webhooks/{wid}", headers=_auth(_token()))
        assert again.status_code == 404


async def test_deliveries_endpoint_lists_ledger(session_factory):
    async with _client(session_factory) as c:
        created = await c.post(
            "/webhooks",
            json={"target_url": "https://x.example/h", "event_types": ["reservation.created"]},
            headers=_auth(_token()),
        )
        wid = created.json()["id"]

    # Seed a ledger row directly.
    async with session_factory() as s:
        s.add(
            WebhookDelivery(
                subscription_id=uuid.UUID(wid),
                event_id="evt-x",
                event_type="reservation.created",
                status="delivered",
                attempts=1,
                response_status=200,
            )
        )
        await s.commit()

    async with _client(session_factory) as c:
        rows = await c.get(f"/webhooks/{wid}/deliveries", headers=_auth(_token()))
    assert rows.status_code == 200
    data = rows.json()
    assert len(data) == 1
    assert data[0]["status"] == "delivered"
    assert data[0]["event_id"] == "evt-x"


async def test_deliveries_endpoint_404_for_missing_webhook(session_factory):
    async with _client(session_factory) as c:
        resp = await c.get(f"/webhooks/{uuid.uuid4()}/deliveries", headers=_auth(_token()))
    assert resp.status_code == 404
    assert resp.json() == {"detail": "Webhook not found"}


async def test_deliveries_endpoint_clamps_limit(session_factory):
    """`limit = max(1, min(limit, 500))` must clamp rather than pass 0/negative
    straight to SQL's LIMIT (which for limit=0 would return zero rows, not the
    newest one)."""
    async with _client(session_factory) as c:
        created = await c.post(
            "/webhooks",
            json={"target_url": "https://x.example/h", "event_types": ["reservation.created"]},
            headers=_auth(_token()),
        )
        wid = created.json()["id"]

    async with session_factory() as s:
        for i in range(3):
            s.add(
                WebhookDelivery(
                    subscription_id=uuid.UUID(wid),
                    event_id=f"evt-{i}",
                    event_type="reservation.created",
                    status="delivered",
                    attempts=1,
                    response_status=200,
                )
            )
        await s.commit()

    async with _client(session_factory) as c:
        clamped = await c.get(f"/webhooks/{wid}/deliveries?limit=0", headers=_auth(_token()))
        assert clamped.status_code == 200
        assert len(clamped.json()) == 1

        huge = await c.get(f"/webhooks/{wid}/deliveries?limit=999999", headers=_auth(_token()))
        assert huge.status_code == 200
        assert len(huge.json()) == 3


def test_known_event_types_are_the_documented_six():
    assert KNOWN_EVENT_TYPES == {
        "reservation.created",
        "reservation.updated",
        "reservation.cancelled",
        "reservation.completed",
        "reservation.failed",
        "reservation.expiring_soon",
    }


def test_event_payload_round_trips_for_signing():
    """The body signed is the exact JSON bytes; a receiver recomputes over them."""
    payload = {"event": "reservation.created", "reservation_id": "r1", "event_id": "e1"}
    body = json.dumps(payload).encode()
    sig = sign_body(body, "k")
    assert hmac.compare_digest(sig, sign_body(body, "k"))
