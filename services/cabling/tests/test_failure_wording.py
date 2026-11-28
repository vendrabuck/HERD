"""Exception and upstream text never reaches an answer, a report row, or a log line.

Each test makes a dependency fail with an exception whose text holds a distinctive
service address, and checks that the answer and every log line carry the exception's
class name or fixed wording only.
"""

import io
import json
import logging
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from app.database import Base, get_db
from app.dependencies import get_current_user_payload
from app.main import app
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

ADDRESS = "http://inventory-marker-7c1d:8000/internal/path"

ADMIN = {"sub": str(uuid.uuid4()), "username": "admin", "role": "admin"}

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
async def client():
    app.dependency_overrides[get_current_user_payload] = lambda: ADMIN
    app.dependency_overrides[get_db] = _override_get_db
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


def _failing_client(exc: Exception) -> MagicMock:
    """A patch for `httpx.AsyncClient` whose get and post raise `exc`."""
    inner = MagicMock()
    inner.get = AsyncMock(side_effect=exc)
    inner.post = AsyncMock(side_effect=exc)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=inner)
    cm.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=cm)


def _no_address_anywhere(caplog, *texts: str) -> None:
    assert "inventory-marker-7c1d" not in caplog.text
    for text in texts:
        assert "inventory-marker-7c1d" not in text


@pytest.mark.asyncio
async def test_the_import_503_names_nothing_of_the_failure(client, caplog):
    """The resolve-by-name call fails the way httpx does for a 5xx, whose text names
    the service's address."""
    caplog.set_level(logging.INFO)
    request = httpx.Request("POST", ADDRESS)
    error = httpx.HTTPStatusError(
        f"Server error '500' for url '{ADDRESS}'",
        request=request,
        response=httpx.Response(500, request=request),
    )
    raw = json.dumps([{"name": "lab", "canvas": {"nodes": [{"data": {"device": {"name": "sw"}}}]}}])
    with patch("app.services.device_resolver.httpx.AsyncClient", _failing_client(error)):
        resp = await client.post(
            "/topologies/import",
            params={"format": "json"},
            files={"file": ("t.json", io.BytesIO(raw.encode()), "application/json")},
        )
    assert resp.status_code == 503
    assert resp.json() == {
        "detail": "Could not resolve device names; the import was not processed. Retry the request."
    }
    assert "import_device_name_resolve_failed error=HTTPStatusError" in caplog.text
    _no_address_anywhere(caplog, resp.text)


@pytest.mark.asyncio
async def test_an_unexpected_row_failure_is_reported_by_class_only(client, caplog):
    caplog.set_level(logging.INFO)
    raw = json.dumps([{"name": "lab", "canvas": {"nodes": [], "edges": []}}, {"name": "lab-2"}])
    real = __import__(
        "app.services.topology_validation", fromlist=["run_full_topology_validation"]
    ).run_full_topology_validation
    calls = {"n": 0}

    async def first_fails(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError(f"statement failed against {ADDRESS}")
        return await real(*args, **kwargs)

    with patch("app.services.topology_validation.run_full_topology_validation", first_fails):
        resp = await client.post(
            "/topologies/import",
            params={"format": "json"},
            files={"file": ("t.json", io.BytesIO(raw.encode()), "application/json")},
        )
    assert resp.status_code == 200, resp.text
    assert [(r["action"], r["identity"], r["reason"]) for r in resp.json()["rows"]] == [
        ("reject", "lab", "row could not be imported: RuntimeError"),
        ("create", "lab-2", None),
    ]
    assert "import_row_failed row=0 error=RuntimeError" in caplog.text
    _no_address_anywhere(caplog, resp.text)


@pytest.mark.asyncio
async def test_the_visibility_lookup_logs_and_raises_the_class_only(caplog):
    from app.services import visible_devices

    caplog.set_level(logging.INFO)
    error = httpx.ConnectError(f"cannot reach {ADDRESS}")
    with patch.object(visible_devices, "call_service", AsyncMock(side_effect=error)):
        with pytest.raises(visible_devices.VisibleDevicesUnavailableError) as exc:
            await visible_devices.fetch_visible_device_ids(uuid.uuid4(), "Bearer t")
    assert str(exc.value) == "ConnectError"
    records = [r for r in caplog.records if r.getMessage() == "visible_devices_unreachable"]
    assert [r.error for r in records] == ["ConnectError"]
    _no_address_anywhere(caplog)


@pytest.mark.asyncio
async def test_the_edit_lock_lookup_logs_the_class_only(caplog):
    from app.services import reservation_guard

    caplog.set_level(logging.INFO)
    factory = _failing_client(httpx.ConnectError(f"cannot reach {ADDRESS}"))
    with patch.object(reservation_guard.httpx, "AsyncClient", factory):
        assert await reservation_guard.find_blocking_reservations(uuid.uuid4()) == []
    records = [r for r in caplog.records if r.getMessage() == "reservation_guard_unreachable"]
    assert [(r.levelname, r.error, r.exc_info) for r in records] == [
        ("WARNING", "ConnectError", None)
    ]
    _no_address_anywhere(caplog)


@pytest.mark.asyncio
async def test_the_delete_guard_logs_the_class_only(caplog):
    from app.services import reservation_guard
    from fastapi import HTTPException

    caplog.set_level(logging.INFO)
    factory = _failing_client(httpx.ConnectError(f"cannot reach {ADDRESS}"))
    with patch.object(reservation_guard.httpx, "AsyncClient", factory):
        with pytest.raises(HTTPException):
            await reservation_guard.find_blocking_reservations_strict(uuid.uuid4())
    assert "for live reservations: ConnectError" in caplog.text
    _no_address_anywhere(caplog)


@pytest.mark.asyncio
async def test_the_delete_guard_logs_an_unreadable_body_by_class_only(caplog):
    """The status of a row is the reservations service's text; the log line does not
    quote it."""
    from app.services import reservation_guard
    from fastapi import HTTPException

    caplog.set_level(logging.INFO)
    topology_id = uuid.uuid4()
    response = MagicMock()
    response.status_code = 200
    response.json = MagicMock(
        return_value=[
            {"id": "r1", "status": "inventory-marker-7c1d", "topology_id": str(topology_id)}
        ]
    )
    inner = MagicMock()
    inner.get = AsyncMock(return_value=response)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=inner)
    cm.__aexit__ = AsyncMock(return_value=False)
    with patch.object(reservation_guard.httpx, "AsyncClient", MagicMock(return_value=cm)):
        with pytest.raises(HTTPException):
            await reservation_guard.find_blocking_reservations_strict(topology_id)
    assert "unparseable by-topology body" in caplog.text
    assert caplog.text.rstrip().endswith("ValueError")
    _no_address_anywhere(caplog)


@pytest.mark.asyncio
async def test_the_device_group_lookup_logs_the_class_only(caplog):
    from app.services import device_group_guard

    caplog.set_level(logging.INFO)
    factory = _failing_client(httpx.ConnectError(f"cannot reach {ADDRESS}"))
    with patch.object(device_group_guard.httpx, "AsyncClient", factory):
        assert await device_group_guard.fetch_device_group_ids(uuid.uuid4(), "tok") is None
    records = [r for r in caplog.records if r.getMessage() == "device_group_guard_unreachable"]
    assert [(r.levelname, r.error, r.exc_info) for r in records] == [
        ("WARNING", "ConnectError", None)
    ]
    _no_address_anywhere(caplog)


@pytest.mark.asyncio
async def test_the_bulk_device_group_lookup_logs_the_class_only(caplog):
    """`fetch_device_group_ids` swallows its own failures; one that still escapes it
    is logged by class."""
    from app.schemas.connection import ConnectionCreate
    from app.services import connection_service

    caplog.set_level(logging.INFO)
    item = ConnectionCreate(
        device_a_id=uuid.uuid4(), port_a="e0", device_b_id=uuid.uuid4(), port_b="e0"
    )
    error = RuntimeError(f"cannot reach {ADDRESS}")
    with patch.object(connection_service, "fetch_device_group_ids", AsyncMock(side_effect=error)):
        cache = await connection_service._resolve_group_cache_for_batch([item], "tok")
    assert set(cache.values()) == {None}
    records = [r for r in caplog.records if r.getMessage() == "device_group_guard_unexpected_error"]
    assert [r.error for r in records] == ["RuntimeError", "RuntimeError"]
    _no_address_anywhere(caplog)


@pytest.mark.asyncio
@pytest.mark.parametrize("which", ["device_types", "config"])
async def test_the_routing_pass_failure_carries_the_class_only(which):
    from app.services import l3_validation

    ctx = l3_validation.L3InventoryContext()
    error = httpx.ConnectError(f"cannot reach {ADDRESS}")
    with patch.object(l3_validation, "call_service", AsyncMock(side_effect=error)):
        with pytest.raises(l3_validation.L3ConfigUnavailable) as exc:
            if which == "device_types":
                await ctx.load_device_types([uuid.uuid4()])
            else:
                await ctx.load_configs([uuid.uuid4()])
    assert str(exc.value).endswith(": ConnectError")
    assert "inventory-marker-7c1d" not in str(exc.value)
