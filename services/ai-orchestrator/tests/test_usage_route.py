"""Tests for GET /api/ai/usage (admin-only per-user daily AI token usage).

Follows the test_quota_route.py pattern: in-process ASGI transport, JWT helper,
create_all/drop_all per test. Covers auth + admin enforcement, response shape
(including the cache counters), date-range filtering, the empty range, and the
start/end validation errors.
"""

import uuid
from datetime import UTC, datetime, timedelta, timezone

import pytest
from app import config as config_module
from app.database import Base, engine
from app.main import app
from app.services import usage_repo
from httpx import ASGITransport, AsyncClient
from jose import jwt
from sqlalchemy.ext.asyncio import async_sessionmaker

TestSessionLocal = async_sessionmaker(engine, expire_on_commit=False)

_USER_A = uuid.uuid4()
_USER_B = uuid.uuid4()


def _token(role: str = "admin", sub: str | None = None) -> str:
    payload = {
        "sub": sub or str(uuid.uuid4()),
        "role": role,
        "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
    }
    return jwt.encode(
        payload,
        config_module.settings.secret_key,
        algorithm=config_module.settings.algorithm,
    )


def _admin_headers() -> dict:
    return {"Authorization": f"Bearer {_token(role='admin')}"}


def _utc_today():
    return datetime.now(UTC).date()


@pytest.fixture
def async_client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.fixture(autouse=True)
async def setup_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    app.dependency_overrides.clear()


# --- auth + admin gate ---


async def test_usage_requires_auth(async_client):
    async with async_client as client:
        resp = await client.get("/usage")
    assert resp.status_code in (401, 403)


async def test_usage_rejects_non_admin(async_client):
    headers = {"Authorization": f"Bearer {_token(role='user')}"}
    async with async_client as client:
        resp = await client.get("/usage", headers=headers)
    assert resp.status_code == 403


@pytest.mark.parametrize("role", ["admin", "superadmin"])
async def test_usage_allows_admin_roles(async_client, role):
    headers = {"Authorization": f"Bearer {_token(role=role)}"}
    async with async_client as client:
        resp = await client.get("/usage", headers=headers)
    assert resp.status_code == 200


# --- response shape ---


async def test_usage_returns_per_user_daily_rows_with_cache_fields(async_client):
    today = _utc_today()
    async with TestSessionLocal() as db:
        await usage_repo.add_tokens(
            db,
            _USER_A,
            input_tokens=30,
            output_tokens=12,
            cache_creation_input_tokens=500,
            cache_read_input_tokens=700,
        )
    async with async_client as client:
        resp = await client.get(
            "/usage",
            params={"start": today.isoformat(), "end": today.isoformat()},
            headers=_admin_headers(),
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["start"] == today.isoformat()
    assert body["end"] == today.isoformat()
    assert len(body["usage"]) == 1
    row = body["usage"][0]
    assert row == {
        "user_id": str(_USER_A),
        "date": today.isoformat(),
        "input_tokens": 30,
        "output_tokens": 12,
        "cache_creation_input_tokens": 500,
        "cache_read_input_tokens": 700,
    }


async def test_usage_lists_multiple_users(async_client):
    today = _utc_today()
    async with TestSessionLocal() as db:
        await usage_repo.add_tokens(db, _USER_A, input_tokens=1, output_tokens=1)
        await usage_repo.add_tokens(db, _USER_B, input_tokens=2, output_tokens=2)
    async with async_client as client:
        resp = await client.get(
            "/usage",
            params={"start": today.isoformat(), "end": today.isoformat()},
            headers=_admin_headers(),
        )
    body = resp.json()
    user_ids = {r["user_id"] for r in body["usage"]}
    assert user_ids == {str(_USER_A), str(_USER_B)}


# --- date-range filtering ---


async def test_usage_filters_by_date_range(async_client):
    today = _utc_today()
    yesterday = today - timedelta(days=1)
    two_days_ago = today - timedelta(days=2)
    async with TestSessionLocal() as db:
        # One row today (via add_tokens), one row two days ago (direct insert).
        await usage_repo.add_tokens(db, _USER_A, input_tokens=10, output_tokens=0)
        from app.models.ai_usage import AIUsage

        db.add(
            AIUsage(
                user_id=_USER_B,
                usage_date=two_days_ago,
                input_tokens=99,
                output_tokens=0,
            )
        )
        await db.commit()
    # Range [yesterday, today] includes only today's row.
    async with async_client as client:
        resp = await client.get(
            "/usage",
            params={"start": yesterday.isoformat(), "end": today.isoformat()},
            headers=_admin_headers(),
        )
    body = resp.json()
    assert len(body["usage"]) == 1
    assert body["usage"][0]["user_id"] == str(_USER_A)
    assert body["usage"][0]["date"] == today.isoformat()


async def test_usage_empty_range_returns_no_rows(async_client):
    today = _utc_today()
    async with TestSessionLocal() as db:
        await usage_repo.add_tokens(db, _USER_A, input_tokens=10, output_tokens=0)
    # A historical window with no rows.
    past_start = (today - timedelta(days=10)).isoformat()
    past_end = (today - timedelta(days=5)).isoformat()
    async with async_client as client:
        resp = await client.get(
            "/usage",
            params={"start": past_start, "end": past_end},
            headers=_admin_headers(),
        )
    assert resp.status_code == 200
    assert resp.json()["usage"] == []


async def test_usage_defaults_to_last_30_days(async_client):
    """Omitting both params defaults to [today-30, today] and returns today's row."""
    today = _utc_today()
    async with TestSessionLocal() as db:
        await usage_repo.add_tokens(db, _USER_A, input_tokens=5, output_tokens=5)
    async with async_client as client:
        resp = await client.get("/usage", headers=_admin_headers())
    assert resp.status_code == 200
    body = resp.json()
    assert body["end"] == today.isoformat()
    assert body["start"] == (today - timedelta(days=30)).isoformat()
    assert len(body["usage"]) == 1


# --- validation ---


async def test_usage_rejects_start_after_end(async_client):
    today = _utc_today()
    async with async_client as client:
        resp = await client.get(
            "/usage",
            params={
                "start": today.isoformat(),
                "end": (today - timedelta(days=1)).isoformat(),
            },
            headers=_admin_headers(),
        )
    assert resp.status_code == 400
    assert "start must not be after end" in resp.json()["detail"]


async def test_usage_rejects_oversized_range(async_client):
    today = _utc_today()
    async with async_client as client:
        resp = await client.get(
            "/usage",
            params={
                "start": (today - timedelta(days=400)).isoformat(),
                "end": today.isoformat(),
            },
            headers=_admin_headers(),
        )
    assert resp.status_code == 400
    assert "date range too large" in resp.json()["detail"]


async def test_usage_rejects_malformed_date(async_client):
    async with async_client as client:
        resp = await client.get(
            "/usage",
            params={"start": "not-a-date", "end": _utc_today().isoformat()},
            headers=_admin_headers(),
        )
    # FastAPI/pydantic rejects an unparseable date query param with 422.
    assert resp.status_code == 422
