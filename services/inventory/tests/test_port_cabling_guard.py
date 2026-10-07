"""Port DELETE and port rename guard (issue #1023).

Two layers: the helper (`find_connections_naming_port`, `assert_port_uncabled`)
exercised through fake `call_service` responses so every transport, status, and
body failure mode is pinned; and the real `PUT /ports/{id}` and
`DELETE /ports/{id}` routes over an in-memory database, asserting the port row
is untouched after every refusal.
"""

import uuid
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from app.config import settings
from app.database import Base, get_db
from app.dependencies.auth import get_current_user_payload
from app.main import app
from app.models.device import Device
from app.models.port import Port
from app.models.template import DeviceTemplate
from app.services import port_cabling_guard as guard
from app.services.port_cabling_guard import (
    PORT_UNVERIFIABLE_DETAIL,
    assert_port_uncabled,
    find_connections_naming_port,
)
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
TestSessionLocal = async_sessionmaker(engine, expire_on_commit=False)

DEVICE = uuid.UUID("00000000-0000-0000-0000-0000000000d1")
CONN_IDS = ["44444444-4444-4444-4444-444444444444", "33333333-3333-3333-3333-333333333333"]


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    monkeypatch.setattr(settings, "internal_api_token", "tok")


@pytest.fixture(autouse=True)
async def setup_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


async def _override_get_db():
    async with TestSessionLocal() as session:
        yield session


@pytest.fixture
async def client():
    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_current_user_payload] = lambda: {
        "sub": "00000000-0000-0000-0000-000000000001",
        "username": "admin",
        "role": "admin",
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


def _cabling(response=None, *, exc: Exception | None = None):
    mock = AsyncMock(side_effect=exc) if exc is not None else AsyncMock(return_value=response)
    return patch.object(guard, "call_service", new=mock)


def _answer(count: int, ids: list[str]):
    return httpx.Response(200, json={"connection_count": count, "connection_ids": ids})


# --- helper -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_uncabled_port_passes_and_sends_device_and_port_name():
    with _cabling(_answer(0, [])) as call:
        assert await assert_port_uncabled(DEVICE, "ge-0/0/1") is None
    args, kwargs = call.call_args
    assert args[1:] == ("GET", "/connections/internal/by-port")
    assert kwargs["params"] == {"device_id": str(DEVICE), "port_name": "ge-0/0/1"}
    assert kwargs["auth"].token == "tok"


@pytest.mark.asyncio
async def test_cabled_port_is_409_port_cabled_with_true_count_and_sorted_ids():
    with _cabling(_answer(12, CONN_IDS)):
        with pytest.raises(HTTPException) as info:
            await assert_port_uncabled(DEVICE, "eth0")
    assert info.value.status_code == 409
    assert info.value.detail == {
        "error": "port_cabled",
        "connection_count": 12,
        "connection_ids": sorted(CONN_IDS),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exc", [httpx.ConnectError("refused"), httpx.ReadTimeout("slow"), RuntimeError("no token")]
)
async def test_transport_failure_is_503(exc):
    with _cabling(exc=exc):
        with pytest.raises(HTTPException) as info:
            await find_connections_naming_port(DEVICE, "eth0")
    assert info.value.status_code == 503
    assert info.value.detail == PORT_UNVERIFIABLE_DETAIL == "Could not verify port is not cabled"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [403, 404, 422, 500, 503])
async def test_non_200_is_503(status):
    with _cabling(httpx.Response(status, json={"connection_count": 0, "connection_ids": []})):
        with pytest.raises(HTTPException) as info:
            await find_connections_naming_port(DEVICE, "eth0")
    assert info.value.status_code == 503
    assert info.value.detail == PORT_UNVERIFIABLE_DETAIL


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {},
        {"connection_ids": []},
        {"connection_count": 0},
        {"connection_count": "0", "connection_ids": []},
        {"connection_count": True, "connection_ids": []},
        {"connection_count": -1, "connection_ids": []},
        {"connection_count": 0, "connection_ids": "none"},
        ["not", "an", "object"],
    ],
)
async def test_unparseable_body_is_503_never_uncabled(body):
    with _cabling(httpx.Response(200, json=body)):
        with pytest.raises(HTTPException) as info:
            await assert_port_uncabled(DEVICE, "eth0")
    assert info.value.status_code == 503
    assert info.value.detail == PORT_UNVERIFIABLE_DETAIL


@pytest.mark.asyncio
async def test_non_json_body_is_503():
    with _cabling(httpx.Response(200, content=b"<html>")):
        with pytest.raises(HTTPException) as info:
            await assert_port_uncabled(DEVICE, "eth0")
    assert info.value.status_code == 503


# --- routes -----------------------------------------------------------------


async def _seed_port(name: str = "ge-0/0/1") -> tuple[uuid.UUID, uuid.UUID]:
    async with TestSessionLocal() as db:
        dev_tpl = DeviceTemplate(
            name="Switch", template_type="device", vendor="V", model="M", sections=[]
        )
        port_tpl = DeviceTemplate(
            name="GigE",
            template_type="port",
            vendor="V",
            model="M",
            sections=[{"name": "S", "fields": [{"key": "k", "label": "K", "type": "string"}]}],
        )
        db.add_all([dev_tpl, port_tpl])
        await db.flush()
        device = Device(
            name="sw1", template_id=dev_tpl.id, topology_type="PHYSICAL", status="AVAILABLE"
        )
        db.add(device)
        await db.flush()
        port = Port(name=name, device_id=device.id, template_id=port_tpl.id, field_data={})
        db.add(port)
        await db.commit()
        return device.id, port.id


async def _port_row(port_id: uuid.UUID) -> Port | None:
    async with TestSessionLocal() as db:
        return (
            (await db.execute(select(Port).where(Port.id == port_id))).unique().scalar_one_or_none()
        )


@pytest.mark.asyncio
async def test_delete_cabled_port_is_409_and_port_survives(client):
    device_id, port_id = await _seed_port()
    with _cabling(_answer(2, CONN_IDS)) as call:
        resp = await client.delete(f"/ports/{port_id}")
    assert resp.status_code == 409
    assert resp.json()["detail"] == {
        "error": "port_cabled",
        "connection_count": 2,
        "connection_ids": sorted(CONN_IDS),
    }
    assert call.call_args.kwargs["params"] == {
        "device_id": str(device_id),
        "port_name": "ge-0/0/1",
    }
    assert await _port_row(port_id) is not None


@pytest.mark.asyncio
async def test_delete_when_cabling_down_is_503_and_port_survives(client):
    _, port_id = await _seed_port()
    with _cabling(exc=httpx.ConnectError("refused")):
        resp = await client.delete(f"/ports/{port_id}")
    assert resp.status_code == 503
    assert resp.json()["detail"] == "Could not verify port is not cabled"
    assert await _port_row(port_id) is not None


@pytest.mark.asyncio
async def test_delete_uncabled_port_is_204(client):
    _, port_id = await _seed_port()
    with _cabling(_answer(0, [])):
        resp = await client.delete(f"/ports/{port_id}")
    assert resp.status_code == 204
    assert await _port_row(port_id) is None


@pytest.mark.asyncio
async def test_delete_unknown_port_is_404_without_asking_cabling(client):
    with _cabling(_answer(0, [])) as call:
        resp = await client.delete(f"/ports/{uuid.uuid4()}")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Port not found"
    call.assert_not_awaited()


@pytest.mark.asyncio
async def test_rename_cabled_port_is_409_and_name_unchanged(client):
    _, port_id = await _seed_port()
    with _cabling(_answer(1, CONN_IDS[:1])) as call:
        resp = await client.put(f"/ports/{port_id}", json={"name": "ge-0/0/9"})
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "port_cabled"
    # The guard asks about the CURRENT name: that is what cables reference.
    assert call.call_args.kwargs["params"]["port_name"] == "ge-0/0/1"
    assert (await _port_row(port_id)).name == "ge-0/0/1"


@pytest.mark.asyncio
async def test_rename_when_cabling_down_is_503_and_name_unchanged(client):
    _, port_id = await _seed_port()
    with _cabling(httpx.Response(500)):
        resp = await client.put(f"/ports/{port_id}", json={"name": "ge-0/0/9"})
    assert resp.status_code == 503
    assert resp.json()["detail"] == "Could not verify port is not cabled"
    assert (await _port_row(port_id)).name == "ge-0/0/1"


@pytest.mark.asyncio
async def test_rename_uncabled_port_succeeds(client):
    _, port_id = await _seed_port()
    with _cabling(_answer(0, [])):
        resp = await client.put(f"/ports/{port_id}", json={"name": "ge-0/0/9"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["name"] == "ge-0/0/9"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body", [{"name": "ge-0/0/1"}, {"field_data": {"k": "v"}}, {}], ids=["same", "fields", "empty"]
)
async def test_update_that_is_not_a_rename_never_asks_cabling(client, body):
    """A cabled port stays editable: only a NAME change is guarded."""
    _, port_id = await _seed_port()
    with _cabling(_answer(5, CONN_IDS)) as call:
        resp = await client.put(f"/ports/{port_id}", json=body)
    assert resp.status_code == 200, resp.text
    call.assert_not_awaited()
