import io
import uuid
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from app.database import Base, get_db
from app.dependencies.auth import get_current_user_payload
from app.main import app
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"

engine = create_async_engine(TEST_DATABASE_URL, echo=False)
TestSessionLocal = async_sessionmaker(engine, expire_on_commit=False)

_SECRET_GET = "app.services.hypervisor_service.httpx.AsyncClient.get"
_TOKEN = {"X-Internal-Token": "test-token"}


async def override_get_db():
    async with TestSessionLocal() as session:
        yield session


def override_auth_admin():
    return {"sub": "00000000-0000-0000-0000-000000000001", "username": "testadmin", "role": "admin"}


@pytest.fixture(autouse=True)
async def setup_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    # Seed the "No Pool" group so materialized devices can be auto-assigned,
    # matching the admin create path.
    from app.models.device_group import DeviceGroup

    async with TestSessionLocal() as session:
        session.add(DeviceGroup(name="No Pool", description="Default group"))
        await session.commit()
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture(autouse=True)
def _mock_minio():
    with patch("app.services.driver_service.upload_object", side_effect=lambda *a, **k: None):
        yield


@pytest.fixture
async def client():
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user_payload] = override_auth_admin
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


_SECTIONS = [
    {
        "name": "Instance",
        "fields": [
            {"key": "image", "label": "Image", "type": "string", "required": True},
            {"key": "cpu", "label": "CPU", "type": "number", "default": 2},
        ],
    }
]


async def _create_driver(client, connection_type: str, name: str) -> str:
    content = b"PK\x03\x04test"
    resp = await client.post(
        "/drivers",
        data={"name": name, "connection_type": connection_type},
        files={"file": ("driver.zip", io.BytesIO(content), "application/zip")},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _create_hypervisor(client) -> str:
    payload = {
        "name": f"HV-{uuid.uuid4()}",
        "endpoint": "https://pve.example:8006",
        "hypervisor_type": "proxmox",
        "secret_id": str(uuid.uuid4()),
    }
    with patch(_SECRET_GET, new=AsyncMock(return_value=httpx.Response(200))):
        resp = await client.post("/hypervisors", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _create_dynamic_template(client, name="VM Template") -> str:
    driver_id = await _create_driver(client, "Hypervisor", f"Recipe {uuid.uuid4()}")
    hid = await _create_hypervisor(client)
    resp = await client.post(
        "/templates",
        json={
            "name": name,
            "template_type": "dynamic",
            "driver_id": driver_id,
            "hypervisor_id": hid,
            "sections": _SECTIONS,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _create_device_template(client, name="Phys Template") -> str:
    driver_id = await _create_driver(client, "Management", f"Mgmt {uuid.uuid4()}")
    resp = await client.post(
        "/templates",
        json={
            "name": name,
            "template_type": "device",
            "driver_id": driver_id,
            "vendor": "Cisco",
            "model": "X",
            "sections": _SECTIONS,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


# --- Internal create happy path ---


@pytest.mark.asyncio
async def test_internal_create_generates_name(client):
    tid = await _create_dynamic_template(client, "Linux VM")
    rid = uuid.uuid4()
    resp = await client.post(
        "/devices/internal",
        headers=_TOKEN,
        json={"template_id": tid, "reservation_id": str(rid), "field_data": {"image": "ubuntu"}},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["status"] == "RESERVED"
    assert data["topology_type"] == "CLOUD"
    # Generated name: "<template-name>-<first 8 of reservation>-<n>".
    assert data["name"] == f"Linux VM-{str(rid)[:8]}-1"
    # Default applied for cpu.
    assert data["field_data"]["cpu"] == 2
    assert data["field_data"]["image"] == "ubuntu"


@pytest.mark.asyncio
async def test_internal_create_name_collision_disambiguates(client):
    tid = await _create_dynamic_template(client, "Linux VM")
    rid = uuid.uuid4()
    body = {"template_id": tid, "reservation_id": str(rid), "field_data": {"image": "ubuntu"}}
    first = await client.post("/devices/internal", headers=_TOKEN, json=body)
    second = await client.post("/devices/internal", headers=_TOKEN, json=body)
    assert first.json()["name"].endswith("-1")
    assert second.json()["name"].endswith("-2")


# --- A generated device name always fits its column ---

# A character outside the BMP: four bytes in UTF-8, one character to varchar(n) and
# to the schema bounds, which count characters.
_WIDE = "\U0001d11e"
_RID = uuid.UUID("0123abcd-0000-4000-8000-00000000f00d")


@pytest.mark.parametrize("char", ["t", _WIDE, "\u00e9"])
@pytest.mark.parametrize("length", [1, 243, 244, 245, 246, 254, 255])
@pytest.mark.parametrize("n", [1, 9, 10, 99, 100, 10_000])
def test_a_generated_device_name_always_fits_its_column(char, length, n):
    from app.schemas.device import DEVICE_NAME_MAX_LENGTH
    from app.services.inventory_service import generated_device_name

    template_name = char * length
    suffix = f"-0123abcd-{n}"
    name = generated_device_name(template_name, _RID, n)

    assert len(name) <= DEVICE_NAME_MAX_LENGTH == 255
    # The suffix is kept whole; only the template part is cut, from its end.
    assert name.endswith(suffix)
    kept = name[: -len(suffix)]
    assert template_name.startswith(kept)
    if length + len(suffix) <= DEVICE_NAME_MAX_LENGTH:
        assert name == template_name + suffix
    else:
        assert len(name) == DEVICE_NAME_MAX_LENGTH
    # Deterministic, and a different n gives a different name.
    assert generated_device_name(template_name, _RID, n) == name
    assert generated_device_name(template_name, _RID, n + 1) != name


@pytest.mark.asyncio
@pytest.mark.parametrize("char", ["t", _WIDE])
@pytest.mark.parametrize("length", [244, 245, 255])
async def test_internal_create_generated_names_fit_the_column_at_the_boundary_lengths(
    client, char, length
):
    """A dynamic template name of 245 to 255 characters made the generated name
    longer than the devices.name column. At 244 the name fits whole; past it the
    template part is shortened, the suffix kept, and the names stay distinct."""
    template_name = char * length
    tid = await _create_dynamic_template(client, template_name)
    rid = uuid.uuid4()
    body = {"template_id": tid, "reservation_id": str(rid), "field_data": {"image": "u"}}

    first = await client.post("/devices/internal", headers=_TOKEN, json=body)
    second = await client.post("/devices/internal", headers=_TOKEN, json=body)

    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    names = [first.json()["name"], second.json()["name"]]
    for n, name in enumerate(names, start=1):
        suffix = f"-{str(rid)[:8]}-{n}"
        assert len(name) <= 255
        assert name == template_name[: 255 - len(suffix)] + suffix
    if length == 244:
        assert names[0] == f"{template_name}-{str(rid)[:8]}-1"
    else:
        assert len(names[0]) == 255
    assert names[0] != names[1]


@pytest.mark.asyncio
async def test_internal_create_explicit_name(client):
    tid = await _create_dynamic_template(client)
    resp = await client.post(
        "/devices/internal",
        headers=_TOKEN,
        json={
            "template_id": tid,
            "reservation_id": str(uuid.uuid4()),
            "name": "my-vm-01",
            "field_data": {"image": "ubuntu"},
        },
    )
    assert resp.status_code == 201
    assert resp.json()["name"] == "my-vm-01"


@pytest.mark.asyncio
async def test_internal_create_allows_unknown_field_data(client):
    # The recipe's create_instance result contributes non-template keys, which
    # the internal path must accept (unlike the admin/device path).
    tid = await _create_dynamic_template(client)
    resp = await client.post(
        "/devices/internal",
        headers=_TOKEN,
        json={
            "template_id": tid,
            "reservation_id": str(uuid.uuid4()),
            "field_data": {
                "image": "ubuntu",
                "management_address": "10.0.0.5",
                "instance_ref": "vm-900",
            },
        },
    )
    assert resp.status_code == 201
    fd = resp.json()["field_data"]
    assert fd["management_address"] == "10.0.0.5"
    assert fd["instance_ref"] == "vm-900"


@pytest.mark.asyncio
async def test_internal_create_rejects_non_dynamic_template_422(client):
    tid = await _create_device_template(client)
    resp = await client.post(
        "/devices/internal",
        headers=_TOKEN,
        json={
            "template_id": tid,
            "reservation_id": str(uuid.uuid4()),
            "field_data": {"image": "x"},
        },
    )
    assert resp.status_code == 422
    assert resp.json()["detail"] == "Template is not a dynamic template"


@pytest.mark.asyncio
async def test_internal_create_bad_token_403(client):
    tid = await _create_dynamic_template(client)
    resp = await client.post(
        "/devices/internal",
        headers={"X-Internal-Token": "wrong"},
        json={"template_id": tid, "reservation_id": str(uuid.uuid4())},
    )
    assert resp.status_code == 403
    assert resp.json()["detail"] == "Invalid internal token"


# --- Internal create idempotency on request_id (issue #275) ---


@pytest.mark.asyncio
async def test_internal_create_same_request_id_returns_same_device(client):
    # A redelivered create carrying the same request_id must converge on one
    # device row (201 both times, same id), not materialize a duplicate.
    tid = await _create_dynamic_template(client, "Linux VM")
    req = str(uuid.uuid4())
    body = {
        "template_id": tid,
        "reservation_id": str(uuid.uuid4()),
        "request_id": req,
        "field_data": {"image": "ubuntu"},
    }
    first = await client.post("/devices/internal", headers=_TOKEN, json=body)
    second = await client.post("/devices/internal", headers=_TOKEN, json=body)
    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    assert first.json()["name"] == second.json()["name"]


@pytest.mark.asyncio
async def test_internal_create_different_request_ids_create_different_devices(client):
    tid = await _create_dynamic_template(client, "Linux VM")
    rid = str(uuid.uuid4())
    b1 = {
        "template_id": tid,
        "reservation_id": rid,
        "request_id": str(uuid.uuid4()),
        "field_data": {"image": "ubuntu"},
    }
    b2 = {**b1, "request_id": str(uuid.uuid4())}
    first = await client.post("/devices/internal", headers=_TOKEN, json=b1)
    second = await client.post("/devices/internal", headers=_TOKEN, json=b2)
    assert first.json()["id"] != second.json()["id"]


@pytest.mark.asyncio
async def test_internal_create_omitted_request_id_always_creates(client):
    # No request_id: the prior always-create behavior is preserved (two rows).
    tid = await _create_dynamic_template(client, "Linux VM")
    rid = str(uuid.uuid4())
    body = {"template_id": tid, "reservation_id": rid, "field_data": {"image": "ubuntu"}}
    first = await client.post("/devices/internal", headers=_TOKEN, json=body)
    second = await client.post("/devices/internal", headers=_TOKEN, json=body)
    assert first.json()["id"] != second.json()["id"]
    assert first.json()["name"].endswith("-1")
    assert second.json()["name"].endswith("-2")


@pytest.mark.asyncio
async def test_internal_create_name_collision_retry_with_request_id_set(client):
    # The generated-name disambiguation loop must still work when request_id is
    # set: two distinct request_ids on the same reservation collide on the -1
    # name and the second must land on -2, not return the first.
    tid = await _create_dynamic_template(client, "Linux VM")
    rid = str(uuid.uuid4())
    b1 = {
        "template_id": tid,
        "reservation_id": rid,
        "request_id": str(uuid.uuid4()),
        "field_data": {"image": "ubuntu"},
    }
    b2 = {**b1, "request_id": str(uuid.uuid4())}
    first = await client.post("/devices/internal", headers=_TOKEN, json=b1)
    second = await client.post("/devices/internal", headers=_TOKEN, json=b2)
    assert first.json()["name"].endswith("-1")
    assert second.json()["name"].endswith("-2")
    assert first.json()["id"] != second.json()["id"]


@pytest.mark.asyncio
async def test_internal_create_explicit_name_same_request_id_returns_existing(client):
    tid = await _create_dynamic_template(client)
    req = str(uuid.uuid4())
    body = {
        "template_id": tid,
        "reservation_id": str(uuid.uuid4()),
        "name": "my-vm-01",
        "request_id": req,
        "field_data": {"image": "ubuntu"},
    }
    first = await client.post("/devices/internal", headers=_TOKEN, json=body)
    second = await client.post("/devices/internal", headers=_TOKEN, json=body)
    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["id"] == second.json()["id"]


@pytest.mark.asyncio
async def test_internal_create_explicit_name_collision_new_request_id_409(client):
    # A brand-new request_id reusing an already-taken explicit name is a genuine
    # name conflict, not an idempotent replay, so it still 409s with the pinned
    # wording.
    tid = await _create_dynamic_template(client)
    first = await client.post(
        "/devices/internal",
        headers=_TOKEN,
        json={
            "template_id": tid,
            "reservation_id": str(uuid.uuid4()),
            "name": "dup-name",
            "request_id": str(uuid.uuid4()),
            "field_data": {"image": "ubuntu"},
        },
    )
    assert first.status_code == 201
    second = await client.post(
        "/devices/internal",
        headers=_TOKEN,
        json={
            "template_id": tid,
            "reservation_id": str(uuid.uuid4()),
            "name": "dup-name",
            "request_id": str(uuid.uuid4()),
            "field_data": {"image": "ubuntu"},
        },
    )
    assert second.status_code == 409
    assert second.json()["detail"] == "Device with name 'dup-name' already exists"


class _CodedRefusal(Exception):
    """A driver error carrying only what db_refusal reads: a SQLSTATE, or SQLite's
    extended error name; or, with neither, text alone."""

    def __init__(self, text: str, sqlstate: str | None = None, errorname: str | None = None):
        super().__init__(text)
        if sqlstate is not None:
            self.sqlstate = sqlstate
        if errorname is not None:
            self.sqlite_errorname = errorname


_NOT_UNIQUE = [
    pytest.param(_CodedRefusal("fk", sqlstate="23503"), id="23503"),
    pytest.param(_CodedRefusal("check", sqlstate="23514"), id="23514"),
    pytest.param(
        _CodedRefusal("FOREIGN KEY constraint failed", errorname="SQLITE_CONSTRAINT_FOREIGNKEY"),
        id="sqlite-foreign-key",
    ),
    # Text that names a unique column but carries no code: never read as one.
    pytest.param(_CodedRefusal("UNIQUE constraint failed: devices.request_id"), id="text-only"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("named", [False, True], ids=["generated-name", "explicit-name"])
@pytest.mark.parametrize("orig", _NOT_UNIQUE)
async def test_internal_create_a_refusal_that_is_not_a_unique_violation_answers_at_once(
    client, orig, named
):
    """Only a unique violation moves the generated name on to the next n (INV-DYN-10):
    any other refusal answers the generic detail after one insert, where every
    IntegrityError used to read as a taken name and the loop ran 10000 inserts."""
    from app.models.device import Device
    from sqlalchemy.exc import IntegrityError
    from sqlalchemy.ext.asyncio import AsyncSession

    tid = await _create_dynamic_template(client, "Refused VM")
    real_commit = AsyncSession.commit
    inserts = 0

    async def refuse_device_inserts(self):
        nonlocal inserts
        if any(isinstance(obj, Device) for obj in self.new):
            inserts += 1
            raise IntegrityError("INSERT INTO devices ...", {}, orig)
        return await real_commit(self)

    body = {
        "template_id": tid,
        "reservation_id": str(uuid.uuid4()),
        "request_id": str(uuid.uuid4()),
        "field_data": {"image": "ubuntu"},
    }
    if named:
        body["name"] = "refused-vm"
    with patch.object(AsyncSession, "commit", refuse_device_inserts):
        resp = await client.post("/devices/internal", headers=_TOKEN, json=body)
    assert resp.status_code == 409
    assert resp.json() == {"detail": "Device violates a database constraint"}
    assert inserts == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("named", [False, True], ids=["generated-name", "explicit-name"])
async def test_internal_create_that_loses_a_request_id_race_returns_the_winner(client, named):
    """A concurrent create with the same request_id commits between this create's
    fast-path lookup and its insert: the real unique violation is followed by a
    lookup of the request_id, and the winner's device answers (INV-DYN-4)."""
    from app.models.device import Device, DeviceStatus, TopologyType
    from app.services import inventory_service

    tid = await _create_dynamic_template(client, "Raced VM")
    req = uuid.uuid4()
    async with TestSessionLocal() as session:
        winner = Device(
            name="the-winner",
            template_id=uuid.UUID(tid),
            topology_type=TopologyType.CLOUD,
            status=DeviceStatus.RESERVED,
            field_data={"image": "ubuntu"},
            request_id=req,
        )
        session.add(winner)
        await session.commit()
        winner_id = str(winner.id)

    real_lookup = inventory_service._get_device_by_request_id
    lookups = 0

    async def first_lookup_misses(db, request_id):
        nonlocal lookups
        lookups += 1
        return None if lookups == 1 else await real_lookup(db, request_id)

    body = {
        "template_id": tid,
        "reservation_id": str(uuid.uuid4()),
        "request_id": str(req),
        "field_data": {"image": "ubuntu"},
    }
    if named:
        body["name"] = "the-loser"
    with patch.object(inventory_service, "_get_device_by_request_id", first_lookup_misses):
        resp = await client.post("/devices/internal", headers=_TOKEN, json=body)
    assert resp.status_code == 201
    assert resp.json()["id"] == winner_id
    assert lookups == 2


# --- Internal delete ---


@pytest.mark.asyncio
async def test_internal_delete_204(client):
    tid = await _create_dynamic_template(client)
    created = await client.post(
        "/devices/internal",
        headers=_TOKEN,
        json={
            "template_id": tid,
            "reservation_id": str(uuid.uuid4()),
            "field_data": {"image": "x"},
        },
    )
    device_id = created.json()["id"]
    resp = await client.delete(f"/devices/{device_id}/internal", headers=_TOKEN)
    assert resp.status_code == 204
    # Confirm it is gone.
    assert (await client.get(f"/devices/{device_id}/internal", headers=_TOKEN)).status_code == 404


@pytest.mark.asyncio
async def test_internal_delete_absent_404(client):
    resp = await client.delete(f"/devices/{uuid.uuid4()}/internal", headers=_TOKEN)
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_internal_delete_non_dynamic_409(client):
    # A physical device (device template) may not be deleted through this surface.
    tid = await _create_device_template(client)
    created = await client.post(
        "/devices",
        json={
            "name": "phys-1",
            "template_id": tid,
            "topology_type": "PHYSICAL",
            "status": "AVAILABLE",
            "field_data": {"image": "x"},
        },
    )
    assert created.status_code == 201, created.text
    device_id = created.json()["id"]
    resp = await client.delete(f"/devices/{device_id}/internal", headers=_TOKEN)
    assert resp.status_code == 409
    assert resp.json()["detail"] == "Device is not a dynamic instance"


@pytest.mark.asyncio
async def test_internal_delete_bad_token_403(client):
    resp = await client.delete(
        f"/devices/{uuid.uuid4()}/internal", headers={"X-Internal-Token": "wrong"}
    )
    assert resp.status_code == 403
    assert resp.json()["detail"] == "Invalid internal token"


# --- Admin device-create still rejects dynamic templates ---


@pytest.mark.asyncio
async def test_admin_create_device_rejects_dynamic_template_422(client):
    tid = await _create_dynamic_template(client)
    resp = await client.post(
        "/devices",
        json={
            "name": "should-fail",
            "template_id": tid,
            "topology_type": "PHYSICAL",
            "status": "AVAILABLE",
            "field_data": {"image": "x"},
        },
    )
    assert resp.status_code == 422
    # Rejected exactly as a "port" template is: "Template is not a device template".
    assert resp.json()["detail"] == "Template is not a device template"
