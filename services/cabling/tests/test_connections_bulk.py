"""Tests for POST /connections/bulk and the create_connections_bulk service function.

Fixtures mirror test_connections.py's local (non-conftest) pattern: this repo
keeps HTTP client fixtures file-local rather than centralizing them.
"""

import logging
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from app.config import settings
from app.database import Base, get_db
from app.dependencies import get_current_user_payload, require_admin
from app.main import app
from app.schemas.connection import ConnectionCreate
from app.services.connection_service import create_connections_bulk, list_connections
from app.services.device_group_guard import DeviceNotFoundError
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

ADMIN_PAYLOAD = {"sub": str(uuid.uuid4()), "username": "admin", "role": "admin"}
USER_PAYLOAD = {"sub": str(uuid.uuid4()), "username": "viewer", "role": "user"}


def _override_admin():
    return ADMIN_PAYLOAD


def _override_user():
    return USER_PAYLOAD


# DB fixtures (separate in-memory engine per test file, matching test_connections.py)
test_engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
TestSessionLocal = async_sessionmaker(test_engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
async def setup_db():
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


async def _override_get_db() -> AsyncSession:
    async with TestSessionLocal() as session:
        yield session


@pytest.fixture
async def admin_client():
    app.dependency_overrides[get_current_user_payload] = _override_admin
    app.dependency_overrides[require_admin] = _override_admin
    app.dependency_overrides[get_db] = _override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


@pytest.fixture
async def user_client_no_admin():
    """Client where require_admin is NOT overridden, so user role gets 403."""
    app.dependency_overrides[get_current_user_payload] = _override_user
    app.dependency_overrides[get_db] = _override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


@pytest.fixture
async def unauthenticated_client():
    """Client with zero dependency overrides; real JWT validation runs."""
    app.dependency_overrides[get_db] = _override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


def _row(device_a=None, port_a="eth0", device_b=None, port_b="eth1"):
    return {
        "device_a_id": device_a or str(uuid.uuid4()),
        "port_a": port_a,
        "device_b_id": device_b or str(uuid.uuid4()),
        "port_b": port_b,
        "connection_type": "ethernet",
    }


# --- HTTP-level tests ---


@pytest.mark.asyncio
async def test_bulk_happy_path_all_created(admin_client):
    items = [_row() for _ in range(5)]
    resp = await admin_client.post("/connections/bulk", json={"items": items})
    assert resp.status_code == 200
    data = resp.json()
    assert data["created"] == 5
    assert data["rejected"] == 0
    assert len(data["rows"]) == 5
    ids = set()
    for i, row in enumerate(data["rows"]):
        assert row["index"] == i
        assert row["status"] == "created"
        assert row["error"] is None
        assert row["connection_id"] is not None
        ids.add(row["connection_id"])
    assert len(ids) == 5  # all distinct real ids

    # Verify they are actually persisted.
    list_resp = await admin_client.get("/connections")
    assert list_resp.json()["total"] == 5


@pytest.mark.asyncio
async def test_bulk_mixed_batch_indexes_line_up(admin_client):
    valid1 = _row()
    device_id = str(uuid.uuid4())
    self_loop = {
        "device_a_id": device_id,
        "port_a": "eth0",
        "device_b_id": device_id,
        "port_b": "eth0",
        "connection_type": "ethernet",
    }
    valid2 = _row()
    resp = await admin_client.post("/connections/bulk", json={"items": [valid1, self_loop, valid2]})
    assert resp.status_code == 200
    data = resp.json()
    assert data["created"] == 2
    assert data["rejected"] == 1
    rows = data["rows"]
    assert len(rows) == 3
    assert rows[0]["index"] == 0
    assert rows[0]["status"] == "created"
    assert rows[0]["connection_id"] is not None
    assert rows[1]["index"] == 1
    assert rows[1]["status"] == "rejected"
    assert rows[1]["connection_id"] is None
    assert "Cannot connect a port to itself" in rows[1]["error"]
    assert rows[2]["index"] == 2
    assert rows[2]["status"] == "created"
    assert rows[2]["connection_id"] is not None

    list_resp = await admin_client.get("/connections")
    assert list_resp.json()["total"] == 2


@pytest.mark.asyncio
async def test_bulk_self_loop_rejected_siblings_created(admin_client):
    """A self-loop row is rejected without blocking valid siblings elsewhere
    in the batch (order: reject in the middle, valid before and after)."""
    device_id = str(uuid.uuid4())
    self_loop = {
        "device_a_id": device_id,
        "port_a": "p1",
        "device_b_id": device_id,
        "port_b": "p1",
        "connection_type": "ethernet",
    }
    items = [_row(), self_loop, _row(), _row()]
    resp = await admin_client.post("/connections/bulk", json={"items": items})
    assert resp.status_code == 200
    data = resp.json()
    assert data["created"] == 3
    assert data["rejected"] == 1
    statuses = [r["status"] for r in data["rows"]]
    assert statuses == ["created", "rejected", "created", "created"]


@pytest.mark.asyncio
async def test_bulk_duplicates_all_created_none_rejected(admin_client):
    """Duplicate rows (including exact repeats) are all created; duplicate
    detection is deliberately absent (product decision, see single-create's
    test_create_duplicate_connection)."""
    dev_a = str(uuid.uuid4())
    dev_b = str(uuid.uuid4())
    row = _row(device_a=dev_a, device_b=dev_b)
    items = [dict(row) for _ in range(4)]
    resp = await admin_client.post("/connections/bulk", json={"items": items})
    assert resp.status_code == 200
    data = resp.json()
    assert data["created"] == 4
    assert data["rejected"] == 0
    ids = {r["connection_id"] for r in data["rows"]}
    assert len(ids) == 4  # distinct rows despite identical content

    list_resp = await admin_client.get("/connections")
    assert list_resp.json()["total"] == 4


@pytest.mark.asyncio
async def test_bulk_cap_enforcement_over_200_rejected(admin_client):
    items = [_row() for _ in range(201)]
    resp = await admin_client.post("/connections/bulk", json={"items": items})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_bulk_empty_items_rejected(admin_client):
    resp = await admin_client.post("/connections/bulk", json={"items": []})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_bulk_at_cap_200_accepted(admin_client):
    items = [_row() for _ in range(200)]
    resp = await admin_client.post("/connections/bulk", json={"items": items})
    assert resp.status_code == 200
    assert resp.json()["created"] == 200


@pytest.mark.asyncio
async def test_bulk_malformed_row_rejected_at_schema_level(admin_client):
    """A row missing required fields 422s the whole request (schema-level,
    not a per-row rejection in the report)."""
    resp = await admin_client.post(
        "/connections/bulk", json={"items": [{"device_a_id": str(uuid.uuid4())}]}
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_bulk_non_admin_forbidden(user_client_no_admin):
    resp = await user_client_no_admin.post("/connections/bulk", json={"items": [_row()]})
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_bulk_unauthenticated_401(unauthenticated_client):
    resp = await unauthenticated_client.post("/connections/bulk", json={"items": [_row()]})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_bulk_route_forwards_bearer_token_to_group_guard(admin_client, monkeypatch):
    """Regression test for the route's bearer-token extraction (review Finding
    3): the extraction is duplicated from the single-create endpoint and
    nothing else in this suite drives it through the real route, since every
    other HTTP-level test here runs with enforcement disabled and the
    memoization tests below call create_connections_bulk directly, bypassing
    the route entirely. A regression here degrades to a silent fail-open (or,
    post fail-closed change, an unwarranted 503) with the rest of the suite
    still green."""
    monkeypatch.setattr(settings, "enforce_device_group_boundaries", True)
    fetch = AsyncMock(return_value=set())
    with patch("app.services.connection_service.fetch_device_group_ids", fetch):
        resp = await admin_client.post(
            "/connections/bulk",
            json={"items": [_row()]},
            headers={"Authorization": "Bearer my-real-token"},
        )
    assert resp.status_code == 200
    assert resp.json()["created"] == 1
    assert fetch.await_args_list
    for call in fetch.await_args_list:
        assert call.args[1] == "my-real-token"


@pytest.mark.asyncio
async def test_bulk_unverifiable_device_503_creates_nothing_http(admin_client, monkeypatch):
    """A device whose group membership cannot be determined aborts the whole
    HTTP request with 503 and creates nothing, verified end to end through
    the real route and a real read-back."""
    monkeypatch.setattr(settings, "enforce_device_group_boundaries", True)
    fetch = AsyncMock(return_value=None)
    with patch("app.services.connection_service.fetch_device_group_ids", fetch):
        resp = await admin_client.post("/connections/bulk", json={"items": [_row(), _row()]})
    assert resp.status_code == 503
    assert "Could not verify device-group membership" in resp.json()["detail"]

    list_resp = await admin_client.get("/connections")
    assert list_resp.json()["total"] == 0


# --- Service-level tests: memoization and cache semantics ---


@pytest.fixture(autouse=True)
def _disable_group_enforcement(monkeypatch):
    """Default the boundary check off, matching test_service_unit.py's pattern,
    so the HTTP-level tests above stay hermetic. The memoization tests below
    re-enable it explicitly."""
    monkeypatch.setattr(settings, "enforce_device_group_boundaries", False)


def _cc(device_a, device_b, suffix):
    return ConnectionCreate(
        device_a_id=device_a,
        port_a=f"a{suffix}",
        device_b_id=device_b,
        port_b=f"b{suffix}",
        connection_type="L1",
    )


@pytest.mark.asyncio
async def test_bulk_memoizes_group_lookups_across_batch(monkeypatch):
    """20 rows spanning exactly 2 devices must call fetch_device_group_ids
    exactly twice (once per distinct device id), not 2*N times. This is the
    performance invariant the bulk endpoint exists to preserve."""
    monkeypatch.setattr(settings, "enforce_device_group_boundaries", True)
    dev1, dev2 = uuid.uuid4(), uuid.uuid4()
    items = [_cc(dev1, dev2, i) for i in range(20)]
    fetch = AsyncMock(return_value=set())
    with patch("app.services.connection_service.fetch_device_group_ids", fetch):
        async with TestSessionLocal() as db:
            report = await create_connections_bulk(db, items, "admin")
    assert report.created == 20
    assert report.rejected == 0
    assert fetch.call_count == 2


@pytest.mark.asyncio
async def test_bulk_unverifiable_device_fails_closed_creates_nothing(monkeypatch):
    """A device whose membership cannot be determined (None) now aborts the
    WHOLE batch with 503 and creates nothing, unlike the single-create path,
    which still fails open (see test_allows_when_inventory_unavailable_fail_open
    in test_service_unit.py). Before this fix a cached None silently
    suppressed the boundary check for every remaining row in the batch
    instead of refusing the write (review Finding 1/2)."""
    monkeypatch.setattr(settings, "enforce_device_group_boundaries", True)
    dev1, dev2 = uuid.uuid4(), uuid.uuid4()
    items = [_cc(dev1, dev2, i) for i in range(6)]
    fetch = AsyncMock(return_value=None)
    with patch("app.services.connection_service.fetch_device_group_ids", fetch):
        async with TestSessionLocal() as db:
            with pytest.raises(HTTPException) as exc:
                await create_connections_bulk(db, items, "admin")
    assert exc.value.status_code == 503
    assert fetch.call_count == 2  # resolved once per distinct device id, still

    async with TestSessionLocal() as db:
        _, total = await list_connections(db)
    assert total == 0


@pytest.mark.asyncio
async def test_bulk_cached_device_not_found_rejects_every_row(monkeypatch):
    """A device inventory confirms is gone (404) is a cached hard reject for
    every row referencing it, without re-fetching per row."""
    monkeypatch.setattr(settings, "enforce_device_group_boundaries", True)
    missing = uuid.uuid4()
    other = uuid.uuid4()

    async def fake_fetch(device_id, _token):
        if device_id == missing:
            raise DeviceNotFoundError(missing)
        return set()

    fetch = AsyncMock(side_effect=fake_fetch)
    items = [_cc(missing, other, i) for i in range(5)]
    with patch("app.services.connection_service.fetch_device_group_ids", fetch):
        async with TestSessionLocal() as db:
            report = await create_connections_bulk(db, items, "admin")
    assert report.created == 0
    assert report.rejected == 5
    for row in report.rows:
        assert row.status == "rejected"
        assert row.connection_id is None
        assert row.error == f"Device {missing} does not exist"
    # Both distinct device ids (missing and other) are resolved concurrently
    # in the batch pre-pass before any row is validated, so this is 2 calls
    # now, not 1: the old "only look at device_a" short-circuit no longer
    # applies once the whole cache is pre-resolved up front.
    assert fetch.call_count == 2


@pytest.mark.asyncio
async def test_bulk_confirmed_not_found_rejects_only_its_rows_siblings_created(monkeypatch):
    """A confirmed-404 device rejects only the rows that reference it; sibling
    rows referencing unrelated, valid devices are still created."""
    monkeypatch.setattr(settings, "enforce_device_group_boundaries", True)
    missing = uuid.uuid4()
    other = uuid.uuid4()
    sib_a, sib_b = uuid.uuid4(), uuid.uuid4()

    async def fake_fetch(device_id, _token):
        if device_id == missing:
            raise DeviceNotFoundError(missing)
        return set()

    fetch = AsyncMock(side_effect=fake_fetch)
    items = [
        _cc(missing, other, 0),
        _cc(sib_a, sib_b, 1),
        _cc(missing, other, 2),
        _cc(sib_a, sib_b, 3),
    ]
    with patch("app.services.connection_service.fetch_device_group_ids", fetch):
        async with TestSessionLocal() as db:
            report = await create_connections_bulk(db, items, "admin")
    assert report.created == 2
    assert report.rejected == 2
    assert [row.status for row in report.rows] == ["rejected", "created", "rejected", "created"]
    for row in report.rows:
        if row.status == "rejected":
            assert row.error == f"Device {missing} does not exist"
        else:
            assert row.connection_id is not None


@pytest.mark.asyncio
async def test_bulk_resolves_group_ids_once_per_distinct_device_id(monkeypatch):
    """N distinct devices across the batch means exactly N fetch_device_group_ids
    calls, one per distinct id, regardless of how many rows reference them:
    the concurrency invariant the batch pre-pass exists to preserve."""
    monkeypatch.setattr(settings, "enforce_device_group_boundaries", True)
    device_ids = [uuid.uuid4() for _ in range(9)]
    items = [_cc(device_ids[i % 9], device_ids[(i + 1) % 9], i) for i in range(30)]
    distinct = {d for it in items for d in (it.device_a_id, it.device_b_id)}
    assert len(distinct) == 9

    fetch = AsyncMock(return_value=set())
    with patch("app.services.connection_service.fetch_device_group_ids", fetch):
        async with TestSessionLocal() as db:
            report = await create_connections_bulk(db, items, "admin")
    assert report.created == 30
    assert fetch.call_count == len(distinct)


@pytest.mark.asyncio
async def test_bulk_single_commit_not_per_row(monkeypatch):
    """The bulk path commits once for the whole batch, not once per row."""
    dev_a, dev_b = uuid.uuid4(), uuid.uuid4()
    items = [_cc(dev_a, dev_b, i) for i in range(7)]
    async with TestSessionLocal() as db:
        commit_calls = 0
        original_commit = db.commit

        async def counting_commit():
            nonlocal commit_calls
            commit_calls += 1
            await original_commit()

        monkeypatch.setattr(db, "commit", counting_commit)
        report = await create_connections_bulk(db, items, "admin")
    assert report.created == 7
    assert commit_calls == 1


@pytest.mark.asyncio
async def test_bulk_all_rejected_no_commit(monkeypatch):
    """An all-rejected batch never commits (nothing to insert)."""
    device_id = uuid.uuid4()
    items = [
        ConnectionCreate(device_a_id=device_id, port_a="p", device_b_id=device_id, port_b="p")
        for _ in range(3)
    ]
    async with TestSessionLocal() as db:
        commit_calls = 0
        original_commit = db.commit

        async def counting_commit():
            nonlocal commit_calls
            commit_calls += 1
            await original_commit()

        monkeypatch.setattr(db, "commit", counting_commit)
        report = await create_connections_bulk(db, items, "admin")
    assert report.created == 0
    assert report.rejected == 3
    assert commit_calls == 0


@pytest.mark.asyncio
async def test_bulk_emits_per_connection_audit_log(caplog):
    """Bulk-created cables get a per-connection audit log line, matching the
    single-create route's fields plus both ports, not just the aggregate
    summary line (review Finding 5)."""
    dev_a, dev_b = uuid.uuid4(), uuid.uuid4()
    items = [_cc(dev_a, dev_b, 0), _cc(dev_a, dev_b, 1)]
    with caplog.at_level(logging.INFO, logger="app.services.connection_service"):
        async with TestSessionLocal() as db:
            report = await create_connections_bulk(db, items, "admin-user")
    assert report.created == 2

    audit_records = [r for r in caplog.records if r.message == "Bulk connection created"]
    assert len(audit_records) == 2
    logged_ids = {r.connection_id for r in audit_records}
    assert logged_ids == {str(row.connection_id) for row in report.rows}
    for record in audit_records:
        assert record.device_a_id == str(dev_a)
        assert record.device_b_id == str(dev_b)
        assert record.port_a.startswith("a")
        assert record.port_b.startswith("b")
        assert record.created_by == "admin-user"
