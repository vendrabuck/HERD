"""Dynamic-template visibility and the instance-device grant (issues #1053, #1030).

- #1053: a non-admin sees (and so may book) a dynamic template only when one of
  their user groups holds a permission on the device group named by the
  template's hypervisor (`hypervisors.device_group_id`). A hidden template answers
  the same 404 as an unknown id; a hypervisor with no device group is admin-only.
- #1030 B(a): an instance device (a device carrying `request_id`) is visible to a
  non-admin whose own live reservation holds it, through every read gated by
  `_resolve_visible_device_ids`. Physical devices never gain visibility this way,
  and a reservations lookup that cannot be answered grants nothing.
"""

import logging
import uuid
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from app.database import Base, get_db
from app.dependencies.auth import get_current_user_payload
from app.main import app
from app.models.device import Device, DeviceStatus
from app.models.device_group import DeviceGroup, DeviceGroupDevice, DeviceGroupPermission
from app.models.hypervisor import Hypervisor
from app.models.template import DeviceTemplate
from herd_common.enums import TopologyType
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
Session = async_sessionmaker(engine, expire_on_commit=False)

USER_ID = uuid.UUID("00000000-0000-0000-0000-0000000000aa")
USER_GROUP = uuid.uuid4()
GROUPS = "app.routers.device_groups._fetch_user_group_ids"
CALL = "app.services.device_visibility.call_service"
INTERNAL_TOKEN = "internal-test-token"


async def _override_get_db():
    async with Session() as session:
        yield session


def _user():
    return {"sub": str(USER_ID), "username": "u", "role": "user"}


def _admin():
    return {"sub": str(uuid.uuid4()), "username": "a", "role": "admin"}


@pytest.fixture(autouse=True)
async def setup_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture(autouse=True)
def internal_token(monkeypatch):
    monkeypatch.setattr(
        "app.services.device_visibility.settings.internal_api_token", INTERNAL_TOKEN
    )


async def _client(payload):
    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_current_user_payload] = payload
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": "Bearer user-jwt"},
    )


@pytest.fixture
async def user_client():
    async with await _client(_user) as ac:
        yield ac
    app.dependency_overrides.clear()


@pytest.fixture
async def admin_client():
    async with await _client(_admin) as ac:
        yield ac
    app.dependency_overrides.clear()


async def _add(*rows):
    async with Session() as s:
        for row in rows:
            s.add(row)
            await s.flush()
        await s.commit()


async def _group(*, permitted: bool) -> uuid.UUID:
    gid = uuid.uuid4()
    rows = [DeviceGroup(id=gid, name=f"g-{gid.hex[:8]}")]
    if permitted:
        rows.append(DeviceGroupPermission(device_group_id=gid, user_group_id=USER_GROUP))
    await _add(*rows)
    return gid


async def _hypervisor(device_group_id=None) -> uuid.UUID:
    hid = uuid.uuid4()
    await _add(
        Hypervisor(
            id=hid,
            name=f"hv-{hid.hex[:8]}",
            endpoint="https://pve.example",
            hypervisor_type="proxmox",
            secret_id=uuid.uuid4(),
            device_group_id=device_group_id,
        )
    )
    return hid


async def _template(template_type="dynamic", hypervisor_id=None) -> uuid.UUID:
    tid = uuid.uuid4()
    await _add(
        DeviceTemplate(
            id=tid,
            name=f"t-{tid.hex[:8]}",
            template_type=template_type,
            hypervisor_id=hypervisor_id,
            sections=[],
        )
    )
    return tid


async def _device(template_id, *, instance: bool, group_id=None) -> uuid.UUID:
    did = uuid.uuid4()
    rows = [
        Device(
            id=did,
            name=f"d-{did.hex[:8]}",
            template_id=template_id,
            topology_type=TopologyType.CLOUD if instance else TopologyType.PHYSICAL,
            status=DeviceStatus.RESERVED,
            field_data={},
            request_id=uuid.uuid4() if instance else None,
        )
    ]
    if group_id is not None:
        rows.append(DeviceGroupDevice(device_group_id=group_id, device_id=did))
    await _add(*rows)
    return did


# --- issue #1053: dynamic templates are gated by the hypervisor's device group ---


async def test_dynamic_template_visible_through_its_hypervisors_device_group(user_client):
    tid = await _template(hypervisor_id=await _hypervisor(await _group(permitted=True)))
    with patch(GROUPS, new=AsyncMock(return_value=[USER_GROUP])):
        resp = await user_client.get(f"/templates/{tid}")
    assert resp.status_code == 200
    assert resp.json()["id"] == str(tid)


@pytest.mark.parametrize("assigned", ["unpermitted_group", "no_group"])
async def test_hidden_dynamic_template_answers_like_an_unknown_id(user_client, assigned):
    group = await _group(permitted=False) if assigned == "unpermitted_group" else None
    tid = await _template(hypervisor_id=await _hypervisor(group))
    with patch(GROUPS, new=AsyncMock(return_value=[USER_GROUP])):
        hidden = await user_client.get(f"/templates/{tid}")
        unknown = await user_client.get(f"/templates/{uuid.uuid4()}")
    assert hidden.status_code == unknown.status_code == 404
    assert hidden.json() == unknown.json() == {"detail": "Template not found"}


async def test_admin_sees_every_dynamic_template_without_a_group_lookup(admin_client):
    tid = await _template(hypervisor_id=await _hypervisor(None))
    lookup = AsyncMock(side_effect=AssertionError("admins are not filtered"))
    with patch(GROUPS, new=lookup):
        resp = await admin_client.get(f"/templates/{tid}")
        listing = await admin_client.get("/templates")
    assert resp.status_code == 200
    assert [t["id"] for t in listing.json()["items"]] == [str(tid)]


async def test_physical_template_read_needs_no_group_lookup(user_client):
    tid = await _template(template_type="device")
    with patch(GROUPS, new=AsyncMock(side_effect=AssertionError("not a dynamic template"))):
        resp = await user_client.get(f"/templates/{tid}")
    assert resp.status_code == 200


async def test_dynamic_template_read_fails_closed_when_auth_cannot_answer(user_client):
    from fastapi import HTTPException

    tid = await _template(hypervisor_id=await _hypervisor(await _group(permitted=True)))
    outage = AsyncMock(side_effect=HTTPException(status_code=503, detail="auth down"))
    with patch(GROUPS, new=outage):
        resp = await user_client.get(f"/templates/{tid}")
    assert resp.status_code == 503


async def test_template_list_hides_invisible_dynamic_templates_and_counts_what_it_shows(
    user_client,
):
    physical = await _template(template_type="device")
    visible = await _template(hypervisor_id=await _hypervisor(await _group(permitted=True)))
    await _template(hypervisor_id=await _hypervisor(await _group(permitted=False)))
    await _template(hypervisor_id=await _hypervisor(None))
    with patch(GROUPS, new=AsyncMock(return_value=[USER_GROUP])):
        everything = await user_client.get("/templates")
        dynamic = await user_client.get("/templates", params={"template_type": "dynamic"})
    assert {t["id"] for t in everything.json()["items"]} == {str(physical), str(visible)}
    assert everything.json()["total"] == 2
    assert [t["id"] for t in dynamic.json()["items"]] == [str(visible)]
    assert dynamic.json()["total"] == 1


async def test_template_list_of_another_type_skips_the_group_lookup(user_client):
    await _template(hypervisor_id=await _hypervisor(None))
    physical = await _template(template_type="device")
    with patch(GROUPS, new=AsyncMock(side_effect=AssertionError("no dynamic in this listing"))):
        resp = await user_client.get("/templates", params={"template_type": "device"})
    assert [t["id"] for t in resp.json()["items"]] == [str(physical)]


async def test_user_with_no_groups_sees_no_dynamic_template(user_client):
    tid = await _template(hypervisor_id=await _hypervisor(await _group(permitted=True)))
    with patch(GROUPS, new=AsyncMock(return_value=[])):
        resp = await user_client.get(f"/templates/{tid}")
    assert resp.status_code == 404


# --- issue #1053: the hypervisor's device group is set and validated by admins ---


async def test_hypervisor_device_group_is_stored_validated_and_cleared(admin_client):
    group = await _group(permitted=False)
    hid = await _hypervisor(None)
    ok = await admin_client.put(f"/hypervisors/{hid}", json={"device_group_id": str(group)})
    assert ok.status_code == 200
    assert ok.json()["device_group_id"] == str(group)

    bad = await admin_client.put(f"/hypervisors/{hid}", json={"device_group_id": str(uuid.uuid4())})
    assert bad.status_code == 422
    assert bad.json()["detail"] == "Device group does not exist"

    cleared = await admin_client.put(f"/hypervisors/{hid}", json={"device_group_id": None})
    assert cleared.status_code == 200
    assert cleared.json()["device_group_id"] is None


async def test_hypervisor_create_refuses_an_unknown_device_group(admin_client):
    with patch(
        "app.services.hypervisor_service.validate_secret_exists", new=AsyncMock(return_value=None)
    ):
        resp = await admin_client.post(
            "/hypervisors",
            json={
                "name": "pve",
                "endpoint": "https://pve.example",
                "hypervisor_type": "proxmox",
                "secret_id": str(uuid.uuid4()),
                "device_group_id": str(uuid.uuid4()),
            },
        )
    assert resp.status_code == 422
    assert resp.json()["detail"] == "Device group does not exist"


# --- issue #1030 B(a): the owner sees their own instance device ---


def _held(*device_ids):
    return httpx.Response(200, json={"device_ids": [str(d) for d in device_ids]})


def _reservations(answer):
    """Stand in for the reservations lookup behind the grant."""
    if isinstance(answer, Exception):
        return AsyncMock(side_effect=answer)
    return AsyncMock(return_value=answer)


async def test_owner_sees_the_instance_device_their_live_reservation_holds(user_client):
    instance = await _device(await _template(), instance=True)
    route = _reservations(_held(instance))
    with patch(GROUPS, new=AsyncMock(return_value=[USER_GROUP])), patch(CALL, new=route):
        one = await user_client.get(f"/devices/{instance}")
        batch = await user_client.post("/devices/batch", json={"device_ids": [str(instance)]})
        visible = await user_client.get(
            "/device-groups/visible-devices", params={"user_id": str(USER_ID)}
        )
    assert one.status_code == 200
    assert [d["id"] for d in batch.json()["items"]] == [str(instance)]
    assert str(instance) in visible.json()["device_ids"]
    args, kwargs = route.call_args
    assert args == ("http://reservations:8000", "GET", "/internal/held-devices")
    assert kwargs["params"] == {"user_id": str(USER_ID)}
    assert kwargs["auth"].token == INTERNAL_TOKEN


async def test_instance_device_not_held_by_the_caller_stays_hidden(user_client):
    instance = await _device(await _template(), instance=True)
    with (
        patch(GROUPS, new=AsyncMock(return_value=[USER_GROUP])),
        patch(CALL, new=_reservations(_held())),
    ):
        resp = await user_client.get(f"/devices/{instance}")
    assert resp.status_code == 404
    assert resp.json() == {"detail": "Device not found"}


async def test_physical_device_is_never_granted_through_a_reservation(user_client):
    physical = await _device(await _template(template_type="device"), instance=False)
    await _device(await _template(), instance=True)
    with (
        patch(GROUPS, new=AsyncMock(return_value=[USER_GROUP])),
        patch(CALL, new=_reservations(_held(physical))),
    ):
        resp = await user_client.get(f"/devices/{physical}")
    assert resp.status_code == 404


@pytest.mark.parametrize(
    "answer",
    [
        httpx.Response(500),
        httpx.Response(200, text="not json"),
        httpx.Response(200, json={"device_ids": ["not-a-uuid"]}),
        httpx.Response(200, json=["no", "wrapper"]),
        httpx.ConnectError("refused"),
    ],
    ids=["5xx", "unparseable", "bad-id", "misshapen", "transport"],
)
async def test_unanswerable_grant_lookup_grants_nothing_and_logs(user_client, caplog, answer):
    instance = await _device(await _template(), instance=True)
    with (
        patch(GROUPS, new=AsyncMock(return_value=[USER_GROUP])),
        patch(CALL, new=_reservations(answer)),
        caplog.at_level(logging.WARNING),
    ):
        resp = await user_client.get(f"/devices/{instance}")
    assert resp.status_code == 404
    actions = [getattr(r, "action", None) for r in caplog.records]
    assert "instance_device_grant_unavailable" in actions


async def test_grant_lookup_without_an_internal_token_grants_nothing(user_client, monkeypatch):
    monkeypatch.setattr("app.services.device_visibility.settings.internal_api_token", "")
    instance = await _device(await _template(), instance=True)
    route = _reservations(_held(instance))
    with patch(GROUPS, new=AsyncMock(return_value=[USER_GROUP])), patch(CALL, new=route):
        resp = await user_client.get(f"/devices/{instance}")
    assert resp.status_code == 404
    assert route.call_count == 0


async def test_reservations_is_not_asked_when_no_instance_device_is_hidden(user_client):
    group = await _group(permitted=True)
    instance = await _device(await _template(), instance=True, group_id=group)
    physical = await _device(await _template(template_type="device"), instance=False)
    route = _reservations(_held())
    with patch(GROUPS, new=AsyncMock(return_value=[USER_GROUP])), patch(CALL, new=route):
        seen = await user_client.get(f"/devices/{instance}")
        hidden = await user_client.get(f"/devices/{physical}")
    assert seen.status_code == 200
    assert hidden.status_code == 404
    assert route.call_count == 0
