"""Direct-call unit tests for app.routers.webhooks.

test_webhooks.py already exercises the router through the ASGI app via
httpx.AsyncClient, but coverage.py's tracer loses line attribution for async
endpoint bodies driven through ASGITransport on this stack (documented HERD
async-coverage artifact: post-await lines and SQLAlchemy async-greenlet DB
work are not attributed to the tracer, even though a passing assertion proves
the line ran). Calling the route functions directly, the same way
app.routers.reservations tests call facade helpers, gets accurate line
attribution for the branches ASGI tests already exercise, plus covers
`_principal_id`'s exception path and the test-only echo sink that has no ASGI
coverage at all (it is registered only when webhook_test_sink_enabled=True).
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from app.database import Base
from app.models.webhook import WebhookDelivery, WebhookSubscription
from app.routers import webhooks as webhooks_mod
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool


@pytest.fixture
async def session_factory():
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


def _payload(role: str = "admin", sub: str | None = None) -> dict:
    return {"sub": sub or str(uuid.uuid4()), "role": role}


# --- _principal_id ----------------------------------------------------------


def test_principal_id_parses_valid_sub():
    uid = uuid.uuid4()
    assert webhooks_mod._principal_id({"sub": str(uid)}) == uid


def test_principal_id_none_when_sub_missing():
    assert webhooks_mod._principal_id({}) is None


def test_principal_id_none_on_malformed_sub():
    """A `sub` claim that is not a valid UUID must not raise; it degrades to
    an unattributed webhook rather than 500ing the create call."""
    assert webhooks_mod._principal_id({"sub": "not-a-uuid"}) is None


# --- create_webhook ----------------------------------------------------------


async def test_create_webhook_direct_sets_created_by_from_sub(session_factory):
    admin_id = uuid.uuid4()
    async with session_factory() as db:
        from app.schemas.webhook import WebhookCreate

        body = WebhookCreate(
            target_url="https://x.example/hook",
            event_types=["reservation.created"],
        )
        result = await webhooks_mod.create_webhook(body, _payload(sub=str(admin_id)), db)

    assert result.created_by == admin_id
    assert result.target_url == "https://x.example/hook"
    assert result.secret  # generated since none was supplied
    assert result.is_active is True


# --- list_webhooks ------------------------------------------------------------


async def test_list_webhooks_direct_orders_newest_first(session_factory):
    # SQLite's `func.now()` server_default has only second resolution, so two
    # inserts made microseconds apart can tie; pin explicit created_at values
    # so the ordering assertion tests the ORDER BY clause, not wall-clock luck.
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    async with session_factory() as db:
        older = WebhookSubscription(
            target_url="https://a.example/h",
            event_types=["reservation.created"],
            secret="s1",
            created_at=base,
        )
        newer = WebhookSubscription(
            target_url="https://b.example/h",
            event_types=["reservation.created"],
            secret="s2",
            created_at=base + timedelta(minutes=1),
        )
        db.add_all([older, newer])
        await db.commit()

        result = await webhooks_mod.list_webhooks(_payload(), db)

    assert [r.target_url for r in result] == ["https://b.example/h", "https://a.example/h"]
    assert all(not hasattr(r, "secret") for r in result)


async def test_list_webhooks_direct_empty(session_factory):
    async with session_factory() as db:
        result = await webhooks_mod.list_webhooks(_payload(), db)
    assert result == []


# --- get_webhook ---------------------------------------------------------------


async def test_get_webhook_direct_found(session_factory):
    async with session_factory() as db:
        sub = WebhookSubscription(
            target_url="https://x.example/h", event_types=["reservation.created"], secret="s1"
        )
        db.add(sub)
        await db.commit()
        await db.refresh(sub)

        result = await webhooks_mod.get_webhook(sub.id, _payload(), db)

    assert result.id == sub.id
    assert result.target_url == "https://x.example/h"


async def test_get_webhook_direct_404_raises_http_exception(session_factory):
    async with session_factory() as db:
        with pytest.raises(HTTPException) as exc_info:
            await webhooks_mod.get_webhook(uuid.uuid4(), _payload(), db)

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "Webhook not found"


# --- delete_webhook --------------------------------------------------------------


async def test_delete_webhook_direct_removes_row(session_factory):
    async with session_factory() as db:
        sub = WebhookSubscription(
            target_url="https://x.example/h", event_types=["reservation.created"], secret="s1"
        )
        db.add(sub)
        await db.commit()
        await db.refresh(sub)
        wid = sub.id

        resp = await webhooks_mod.delete_webhook(wid, _payload(), db)
        assert resp.status_code == 204

    async with session_factory() as db2:
        assert await db2.get(WebhookSubscription, wid) is None


async def test_delete_webhook_direct_404_raises_http_exception(session_factory):
    async with session_factory() as db:
        with pytest.raises(HTTPException) as exc_info:
            await webhooks_mod.delete_webhook(uuid.uuid4(), _payload(), db)

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "Webhook not found"


# --- echo_receiver (test-only sink) ------------------------------------------


async def test_echo_receiver_reports_received_byte_count():
    """The unauthenticated echo sink is a live-gate affordance with no ASGI
    coverage (it is only mounted when webhook_test_sink_enabled=True); build a
    minimal Request whose body() reads back fixed bytes."""
    from starlette.requests import Request

    body = b'{"event":"reservation.created"}'
    sent = False

    async def _receive():
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/webhooks/echo",
        "headers": [],
    }
    request = Request(scope, receive=_receive)

    result = await webhooks_mod.echo_receiver(request)

    assert result == {"ok": True, "received_bytes": len(body)}


# --- list_deliveries -----------------------------------------------------------


async def test_list_deliveries_direct_404_for_missing_webhook(session_factory):
    async with session_factory() as db:
        with pytest.raises(HTTPException) as exc_info:
            await webhooks_mod.list_deliveries(uuid.uuid4(), 100, _payload(), db)

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "Webhook not found"


async def test_list_deliveries_direct_orders_newest_first_and_returns_rows(session_factory):
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    async with session_factory() as db:
        sub = WebhookSubscription(
            target_url="https://x.example/h", event_types=["reservation.created"], secret="s1"
        )
        db.add(sub)
        await db.commit()
        await db.refresh(sub)

        db.add_all(
            [
                WebhookDelivery(
                    subscription_id=sub.id,
                    event_id="evt-1",
                    event_type="reservation.created",
                    status="delivered",
                    attempts=1,
                    response_status=200,
                    created_at=base,
                ),
                WebhookDelivery(
                    subscription_id=sub.id,
                    event_id="evt-2",
                    event_type="reservation.created",
                    status="dead",
                    attempts=3,
                    response_status=500,
                    created_at=base + timedelta(minutes=1),
                ),
            ]
        )
        await db.commit()

        result = await webhooks_mod.list_deliveries(sub.id, 100, _payload(), db)

    assert [r.event_id for r in result] == ["evt-2", "evt-1"]


async def test_list_deliveries_direct_clamps_limit_below_one(session_factory):
    """`limit=max(1, min(limit, 500))`: a caller-supplied 0 (or negative) must
    still return the single newest row, not zero rows straight from SQL LIMIT 0."""
    async with session_factory() as db:
        sub = WebhookSubscription(
            target_url="https://x.example/h", event_types=["reservation.created"], secret="s1"
        )
        db.add(sub)
        await db.commit()
        await db.refresh(sub)

        for i in range(3):
            db.add(
                WebhookDelivery(
                    subscription_id=sub.id,
                    event_id=f"evt-{i}",
                    event_type="reservation.created",
                    status="delivered",
                    attempts=1,
                    response_status=200,
                )
            )
            await db.commit()

        result = await webhooks_mod.list_deliveries(sub.id, 0, _payload(), db)

    assert len(result) == 1


async def test_list_deliveries_direct_clamps_limit_above_500(session_factory):
    async with session_factory() as db:
        sub = WebhookSubscription(
            target_url="https://x.example/h", event_types=["reservation.created"], secret="s1"
        )
        db.add(sub)
        await db.commit()
        await db.refresh(sub)

        for i in range(3):
            db.add(
                WebhookDelivery(
                    subscription_id=sub.id,
                    event_id=f"evt-{i}",
                    event_type="reservation.created",
                    status="delivered",
                    attempts=1,
                    response_status=200,
                )
            )
            await db.commit()

        result = await webhooks_mod.list_deliveries(sub.id, 999999, _payload(), db)

    assert len(result) == 3
