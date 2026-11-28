"""Exception and upstream text never reaches an answer, a report row, or a log line.

Each test makes a dependency fail with an exception whose text holds a distinctive
marker, and checks that the answer and every log line carry the exception's class
name or fixed wording only.
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


# A body that cannot be used ------------------------------------------------------------


def _answering_client(response: httpx.Response) -> MagicMock:
    """A patch for `httpx.AsyncClient` whose get and post answer `response`."""
    inner = MagicMock()
    inner.get = AsyncMock(return_value=response)
    inner.post = AsyncMock(return_value=response)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=inner)
    cm.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=cm)


def _answer(content: str) -> httpx.Response:
    return httpx.Response(200, content=content.encode(), request=httpx.Request("GET", ADDRESS))


@pytest.mark.asyncio
async def test_a_body_fault_in_either_lookup_logs_its_traceback(caplog):
    """A failure that is not the call's own, here an item without an `id`, is logged
    with its class and its traceback; a transport failure has no traceback."""
    from app.services import device_group_guard, reservation_guard

    caplog.set_level(logging.INFO)
    with patch.object(
        device_group_guard.httpx, "AsyncClient", _answering_client(_answer('[{"x": 1}]'))
    ):
        assert await device_group_guard.fetch_device_group_ids(uuid.uuid4(), "t") is None
    with patch.object(reservation_guard.httpx, "AsyncClient", _answering_client(_answer("[1]"))):
        assert await reservation_guard.find_blocking_reservations(uuid.uuid4()) == []
    records = [
        r
        for r in caplog.records
        if r.getMessage() in ("device_group_guard_unreachable", "reservation_guard_unreachable")
    ]
    assert [(r.getMessage(), r.error) for r in records] == [
        ("device_group_guard_unreachable", "KeyError"),
        ("reservation_guard_unreachable", "AttributeError"),
    ]
    assert all(r.exc_info is not None and r.exc_info[1] is not None for r in records)


@pytest.mark.asyncio
async def test_the_delete_guard_answers_a_body_nested_too_deep_with_its_503(caplog):
    from app.services import reservation_guard
    from fastapi import HTTPException

    deep = _answer("[" * 100_000 + "]" * 100_000)
    with patch.object(reservation_guard.httpx, "AsyncClient", _answering_client(deep)):
        with pytest.raises(HTTPException) as exc:
            await reservation_guard.find_blocking_reservations_strict(uuid.uuid4())
    assert exc.value.status_code == 503
    assert "unparseable by-topology body" in caplog.text
    assert "UpstreamBodyUnusable" in caplog.text


_UNUSABLE_VISIBILITY = {
    "not_json": "<html>",
    "a_list": "[]",
    "ids_not_a_list": '{"device_ids": "x"}',
    "an_id_not_a_string": '{"device_ids": [5]}',
    "an_id_not_a_uuid": '{"device_ids": ["not-a-uuid"]}',
    "too_deep": "[" * 100_000 + "]" * 100_000,
}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body", list(_UNUSABLE_VISIBILITY.values()), ids=list(_UNUSABLE_VISIBILITY)
)
async def test_a_visibility_body_that_cannot_be_used_fails_closed(body, caplog):
    from app.services import visible_devices

    with patch.object(visible_devices, "call_service", AsyncMock(return_value=_answer(body))):
        with pytest.raises(visible_devices.VisibleDevicesUnavailableError) as exc:
            await visible_devices.fetch_visible_device_ids(uuid.uuid4(), "Bearer t")
    assert str(exc.value).startswith("inventory body unusable: ")
    assert exc.value.__cause__ is None and exc.value.__suppress_context__
    assert [r.getMessage() for r in caplog.records if r.name == "app.services.visible_devices"] == [
        "visible_devices_bad_body"
    ]


@pytest.mark.asyncio
async def test_a_visibility_body_of_uuid_strings_is_read():
    from app.services import visible_devices

    device_id = uuid.uuid4()
    body = json.dumps({"device_ids": [str(device_id)]})
    with patch.object(visible_devices, "call_service", AsyncMock(return_value=_answer(body))):
        assert await visible_devices.fetch_visible_device_ids(uuid.uuid4(), "Bearer t") == {
            device_id
        }


_UNUSABLE_DEVICE_BATCH = {
    "not_json": "<html>",
    "an_object": "{}",
    "an_item_not_an_object": "[1]",
    "an_item_without_an_id": '[{"connection_type": "x"}]',
    "an_id_not_a_uuid": '[{"id": "not-a-uuid"}]',
    "too_deep": "[" * 100_000 + "]" * 100_000,
}
_UNUSABLE_CONFIG = {
    "not_json": "<html>",
    "a_list": "[1]",
    "a_config_not_an_object": '{"config": "x"}',
    "an_id_not_a_uuid": '{"id": "not-a-uuid", "config": {}}',
    "too_deep": "[" * 100_000 + "]" * 100_000,
}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("which", "body"),
    [("device_types", b) for b in _UNUSABLE_DEVICE_BATCH.values()]
    + [("config", b) for b in _UNUSABLE_CONFIG.values()],
    ids=[f"device_types-{k}" for k in _UNUSABLE_DEVICE_BATCH]
    + [f"config-{k}" for k in _UNUSABLE_CONFIG],
)
async def test_a_routing_pass_body_that_cannot_be_used_fails_closed(which, body):
    from app.services import l3_validation

    ctx = l3_validation.L3InventoryContext()
    with patch.object(l3_validation, "call_service", AsyncMock(return_value=_answer(body))):
        with pytest.raises(l3_validation.L3ConfigUnavailable) as exc:
            if which == "device_types":
                await ctx.load_device_types([uuid.uuid4()])
            else:
                await ctx.load_configs([uuid.uuid4()])
    assert "answered an unusable body: " in str(exc.value)
    assert exc.value.__cause__ is None


@pytest.mark.asyncio
async def test_a_routing_pass_body_of_the_documented_shape_is_read():
    from app.services import l3_validation

    device_id, version_id = uuid.uuid4(), uuid.uuid4()
    ctx = l3_validation.L3InventoryContext()
    batch = json.dumps([{"id": str(device_id), "connection_type": "Layer 3 Switch"}])
    with patch.object(l3_validation, "call_service", AsyncMock(return_value=_answer(batch))):
        await ctx.load_device_types([device_id])
    assert ctx.connection_type(device_id) == "Layer 3 Switch"
    config = json.dumps({"id": str(version_id), "config": {"interfaces": {}}})
    with patch.object(l3_validation, "call_service", AsyncMock(return_value=_answer(config))):
        await ctx.load_configs([device_id])
    assert ctx.config(device_id) == {"interfaces": {}}
    assert ctx.config_version_id(device_id) == version_id
    other = uuid.uuid4()
    with patch.object(l3_validation, "call_service", AsyncMock(return_value=_answer("null"))):
        await ctx.load_configs([other])
    assert (ctx.config(other), ctx.config_version_id(other)) == (None, None)
