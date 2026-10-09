"""A scheduled apply's reservation_id must name an ACTIVE reservation that holds the
device and, for a non-admin, belongs to the caller (CFG-JOB-4, CFG-JOB-5; issues #704
and #1104).

The check reads reservations' GET /internal/{id} (is the reservation active now) and
GET /internal/by-device/{device_id} (which reservations hold the device, with their
owners), both with the internal token, after the time checks, the 404s, the 403, and
the driver gate, and before the dry-run gate and the job row. It fails closed with
503. Before #1104 the second read was GET /internal/active, which only proved that the
caller owned SOME active reservation holding the device, so a caller could name an
unrelated reservation.
"""

import io
import json
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from app.database import Base, get_db
from app.dependencies.auth import get_current_user_payload
from app.main import app
from app.models.device_config_apply_job import DeviceConfigApplyJob
from app.routers import apply_jobs
from app.routers.apply_jobs import (
    RESERVATION_MISMATCH_ADMIN_ERROR,
    RESERVATION_MISMATCH_ERROR,
    RESERVATION_UNAVAILABLE_ERROR,
)
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

_RealAsyncClient = httpx.AsyncClient

engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
TestSessionLocal = async_sessionmaker(engine, expire_on_commit=False)

ADMIN_ID = "00000000-0000-0000-0000-000000000001"
USER_ID = "00000000-0000-0000-0000-000000000002"
OTHER_USER_ID = "00000000-0000-0000-0000-000000000003"
ADMIN_PAYLOAD = {"sub": ADMIN_ID, "username": "admin", "role": "admin"}
USER_PAYLOAD = {"sub": USER_ID, "username": "viewer", "role": "user"}

_storage: dict[str, bytes] = {}


async def _override_get_db():
    async with TestSessionLocal() as session:
        yield session


@pytest.fixture(autouse=True)
async def setup_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    _storage.clear()
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture(autouse=True)
def _mock_storage():
    def upload(key: str, data: bytes, **_: object) -> None:
        _storage[key] = data

    with (
        patch("app.services.driver_service.upload_object", side_effect=upload),
        patch("app.services.driver_service.delete_object", side_effect=_storage.pop),
    ):
        yield


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def _internal_token(monkeypatch):
    monkeypatch.setattr(apply_jobs.settings, "internal_api_token", "internal-token")


def _client_as(payload: dict) -> AsyncClient:
    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_current_user_payload] = lambda: payload
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _driver_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("driver.py", "class Driver:\n    pass\n")
        zf.writestr("driver_metadata.json", json.dumps({"supports_dry_run": True}))
    return buf.getvalue()


async def _seed() -> tuple[str, str]:
    """As admin: a Management device with one config version; (device_id, version_id)."""
    async with _client_as(ADMIN_PAYLOAD) as ac:
        drv = await ac.post(
            "/drivers",
            data={"name": f"ScopeDrv-{uuid.uuid4().hex[:6]}", "connection_type": "Management"},
            files={"file": ("d.zip", io.BytesIO(_driver_zip()), "application/zip")},
        )
        assert drv.status_code == 201, drv.text
        tpl = await ac.post(
            "/templates",
            json={
                "name": f"Tpl-{uuid.uuid4().hex[:6]}",
                "vendor": "V",
                "model": "M",
                "driver_id": drv.json()["id"],
                "sections": [
                    {
                        "name": "General",
                        "fields": [{"key": "model", "label": "Model", "type": "string"}],
                    }
                ],
            },
        )
        assert tpl.status_code == 201, tpl.text
        dev = await ac.post(
            "/devices",
            json={
                "name": f"d-{uuid.uuid4().hex[:6]}",
                "template_id": tpl.json()["id"],
                "topology_type": "PHYSICAL",
                "status": "AVAILABLE",
                "field_data": {"model": "X"},
            },
        )
        assert dev.status_code == 201, dev.text
        device_id = dev.json()["id"]
        cv = await ac.post(f"/devices/{device_id}/config-versions", json={"config": {"vlan": 7}})
        assert cv.status_code == 201, cv.text
    app.dependency_overrides.clear()
    return device_id, cv.json()["id"]


def _future(seconds: int = 120) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


async def _job_count() -> int:
    async with TestSessionLocal() as session:
        return (await session.execute(select(func.count(DeviceConfigApplyJob.id)))).scalar_one()


def _reservations(monkeypatch, handler, seen: list | None = None) -> None:
    """Route every httpx.AsyncClient the router opens through `handler`."""

    def wrapped(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return handler(request)

    def factory(*args, **kwargs):
        return _RealAsyncClient(*args, transport=httpx.MockTransport(wrapped), **kwargs)

    monkeypatch.setattr(apply_jobs.httpx, "AsyncClient", factory)


def _answer(*, status: httpx.Response, holders: httpx.Response, owns_active: bool = False):
    """reservations: the by-device read gets `holders`, the status read `status`.

    `owns_active` is what GET /internal/active would answer (does the caller own
    ANY active reservation holding the device); the check no longer asks it, so a
    test that sets it True proves the old proxy is not what decides.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/internal/by-device/"):
            return holders
        if request.url.path == "/internal/active":
            return httpx.Response(200, json={"owns_active": owns_active})
        return status

    return handler


def _active(reservation_id: str) -> httpx.Response:
    return httpx.Response(200, json={"id": reservation_id, "status": "ACTIVE", "is_active": True})


def _holder(reservation_id: str, user_id: str, device_id: str) -> dict:
    return {
        "id": reservation_id,
        "user_id": user_id,
        "device_id": device_id,
        "status": "ACTIVE",
        "end_time": _future(3600),
    }


async def _schedule(payload: dict, device_id: str, version_id: str, reservation_id: str | None):
    body: dict = {"scheduled_for": _future()}
    if reservation_id is not None:
        body["reservation_id"] = reservation_id
    # A non-admin passes the authorization check (manage grant or reservation
    # owner); this file is about the reservation_id check that follows it.
    with patch("app.routers.apply_jobs._user_can_manage_device", new=AsyncMock(return_value=True)):
        async with _client_as(payload) as ac:
            return await ac.post(
                f"/devices/{device_id}/config-versions/{version_id}/schedule", json=body
            )


# --- The happy paths --------------------------------------------------------


@pytest.mark.asyncio
async def test_reservation_id_valid_and_owned_schedules_successfully(monkeypatch):
    """A non-admin naming their own ACTIVE reservation that holds the device: 201,
    and exactly two reads, the status read then the by-device read, both with the
    internal token; GET /internal/active is no longer asked."""
    device_id, version_id = await _seed()
    rid = str(uuid.uuid4())
    seen: list = []
    holders = httpx.Response(200, json=[_holder(rid, USER_ID, device_id)])
    _reservations(monkeypatch, _answer(status=_active(rid), holders=holders), seen)

    resp = await _schedule(USER_PAYLOAD, device_id, version_id, rid)

    assert resp.status_code == 201, resp.text
    assert resp.json()["reservation_id"] == rid
    assert [r.url.path for r in seen] == [
        f"/internal/{rid}",
        f"/internal/by-device/{device_id}",
    ]
    assert all(r.headers["X-Internal-Token"] == "internal-token" for r in seen)
    assert await _job_count() == 1


@pytest.mark.asyncio
async def test_admin_may_name_another_users_active_reservation_holding_the_device(monkeypatch):
    """An admin is exempt from ownership: another user's ACTIVE reservation that
    holds the device is accepted, and the admin need not own any reservation."""
    device_id, version_id = await _seed()
    rid = str(uuid.uuid4())
    holders = httpx.Response(200, json=[_holder(rid, OTHER_USER_ID, device_id)])
    _reservations(monkeypatch, _answer(status=_active(rid), holders=holders))

    resp = await _schedule(ADMIN_PAYLOAD, device_id, version_id, rid)

    assert resp.status_code == 201, resp.text
    assert resp.json()["reservation_id"] == rid


@pytest.mark.asyncio
async def test_schedule_without_a_reservation_asks_nothing(monkeypatch):
    device_id, version_id = await _seed()
    seen: list = []
    _reservations(monkeypatch, lambda request: httpx.Response(500), seen)

    resp = await _schedule(USER_PAYLOAD, device_id, version_id, None)

    assert resp.status_code == 201, resp.text
    assert resp.json()["reservation_id"] is None
    assert seen == []


# --- The refusals (422, nothing written) --------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "detail"),
    [(USER_PAYLOAD, RESERVATION_MISMATCH_ERROR), (ADMIN_PAYLOAD, RESERVATION_MISMATCH_ADMIN_ERROR)],
    ids=["user", "admin"],
)
async def test_foreign_reservation_id_returns_422_and_writes_no_row(monkeypatch, payload, detail):
    """An id reservations does not know (404 on the status read) is 422, and the
    by-device read is never made."""
    device_id, version_id = await _seed()
    rid = str(uuid.uuid4())
    seen: list = []
    _reservations(
        monkeypatch,
        _answer(status=httpx.Response(404, json={"detail": "x"}), holders=httpx.Response(500)),
        seen,
    )

    resp = await _schedule(payload, device_id, version_id, rid)

    assert resp.status_code == 422
    assert resp.json() == {"detail": detail}
    assert [r.url.path for r in seen] == [f"/internal/{rid}"]
    assert await _job_count() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "detail"),
    [(USER_PAYLOAD, RESERVATION_MISMATCH_ERROR), (ADMIN_PAYLOAD, RESERVATION_MISMATCH_ADMIN_ERROR)],
    ids=["user", "admin"],
)
async def test_reservation_id_inactive_returns_422_and_writes_no_row(monkeypatch, payload, detail):
    """A reservation that holds the device and is the caller's, but is not active
    now (PENDING, or past its window), is 422 for every caller."""
    device_id, version_id = await _seed()
    rid = str(uuid.uuid4())
    status = httpx.Response(200, json={"id": rid, "status": "PENDING", "is_active": False})
    owner = payload["sub"]
    holders = httpx.Response(200, json=[_holder(rid, owner, device_id)])
    _reservations(monkeypatch, _answer(status=status, holders=holders))

    resp = await _schedule(payload, device_id, version_id, rid)

    assert resp.status_code == 422
    assert resp.json() == {"detail": detail}
    assert await _job_count() == 0


@pytest.mark.asyncio
async def test_reservation_id_active_but_not_owned_by_caller_returns_422(monkeypatch):
    """A non-admin naming another user's ACTIVE reservation that holds the device
    is 422, even though they pass the authorization check."""
    device_id, version_id = await _seed()
    rid = str(uuid.uuid4())
    holders = httpx.Response(200, json=[_holder(rid, OTHER_USER_ID, device_id)])
    _reservations(monkeypatch, _answer(status=_active(rid), holders=holders))

    resp = await _schedule(USER_PAYLOAD, device_id, version_id, rid)

    assert resp.status_code == 422
    assert resp.json() == {"detail": RESERVATION_MISMATCH_ERROR}
    assert await _job_count() == 0


@pytest.mark.asyncio
async def test_non_admin_cannot_name_their_own_reservation_without_the_device(monkeypatch):
    """The #1104 case: the caller owns reservation A, which holds the device (so
    they pass authorization), and names their own ACTIVE reservation B, which does
    not. B is not listed by the by-device read, so the schedule is 422."""
    device_id, version_id = await _seed()
    holding = str(uuid.uuid4())
    named = str(uuid.uuid4())
    holders = httpx.Response(200, json=[_holder(holding, USER_ID, device_id)])
    _reservations(monkeypatch, _answer(status=_active(named), holders=holders, owns_active=True))

    resp = await _schedule(USER_PAYLOAD, device_id, version_id, named)

    assert resp.status_code == 422
    assert resp.json() == {"detail": RESERVATION_MISMATCH_ERROR}
    assert await _job_count() == 0


@pytest.mark.asyncio
async def test_admin_cannot_name_an_active_reservation_without_the_device(monkeypatch):
    """An admin is not exempt from the device check. A row whose id is not a UUID
    is skipped, not a crash."""
    device_id, version_id = await _seed()
    named = str(uuid.uuid4())
    holders = httpx.Response(
        200,
        json=[
            _holder(str(uuid.uuid4()), ADMIN_ID, device_id),
            _holder("not-a-uuid", ADMIN_ID, device_id),
        ],
    )
    _reservations(monkeypatch, _answer(status=_active(named), holders=holders))

    resp = await _schedule(ADMIN_PAYLOAD, device_id, version_id, named)

    assert resp.status_code == 422
    assert resp.json() == {"detail": RESERVATION_MISMATCH_ADMIN_ERROR}
    assert await _job_count() == 0


# --- Fail closed (503, nothing written) ---------------------------------------


def _raise(exc: Exception):
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc

    return handler


def _status_ok_then(holders_handler):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/internal/by-device/"):
            return holders_handler(request)
        return httpx.Response(200, json={"is_active": True})

    return handler


def _status_answer(response: httpx.Response):
    def handler(request: httpx.Request) -> httpx.Response:
        return response

    return handler


_UNANSWERABLE = {
    "status-transport-error": _raise(httpx.ConnectError("reservations down at 10.0.0.9")),
    "status-500": _status_answer(httpx.Response(500, text="boom")),
    "status-403": _status_answer(httpx.Response(403, json={"detail": "Invalid internal token"})),
    "status-not-json": _status_answer(httpx.Response(200, text="<html>")),
    "holders-transport-error": _status_ok_then(_raise(httpx.ReadTimeout("slow"))),
    "holders-500": _status_ok_then(lambda r: httpx.Response(500)),
    "holders-not-json": _status_ok_then(lambda r: httpx.Response(200, text="nope")),
    "holders-object": _status_ok_then(lambda r: httpx.Response(200, json={"id": "x"})),
    "holders-list-of-strings": _status_ok_then(lambda r: httpx.Response(200, json=["x"])),
    "holders-row-without-user": _status_ok_then(
        lambda r: httpx.Response(200, json=[{"id": str(uuid.uuid4())}])
    ),
    "holders-id-not-a-string": _status_ok_then(
        lambda r: httpx.Response(200, json=[{"id": 7, "user_id": USER_ID}])
    ),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("handler", list(_UNANSWERABLE.values()), ids=list(_UNANSWERABLE))
async def test_reservation_id_validation_fails_closed_when_unreachable(monkeypatch, handler):
    """Any read that cannot be answered is the fixed 503, never upstream text and
    never an unhandled 500, and no job row is written."""
    device_id, version_id = await _seed()
    _reservations(monkeypatch, handler)

    resp = await _schedule(USER_PAYLOAD, device_id, version_id, str(uuid.uuid4()))

    assert resp.status_code == 503, resp.text
    assert resp.json() == {"detail": RESERVATION_UNAVAILABLE_ERROR}
    assert await _job_count() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("which", ["status", "holders"])
@pytest.mark.parametrize("body", [[{"is_active": True}], "yes", 1, None])
async def test_reservation_id_answer_not_an_object_fails_closed_503(monkeypatch, which, body):
    """A 200 whose JSON has the wrong shape (a status answer that is not an object,
    a by-device answer that is not a list of objects with string id and user_id) is
    the same fail-closed 503 (issue #1096)."""
    device_id, version_id = await _seed()
    rid = str(uuid.uuid4())
    status = _active(rid)
    holders = httpx.Response(200, json=[_holder(rid, USER_ID, device_id)])
    if which == "status":
        status = httpx.Response(200, json=body)
    else:
        holders = httpx.Response(200, json=body)
    _reservations(monkeypatch, _answer(status=status, holders=holders))

    resp = await _schedule(USER_PAYLOAD, device_id, version_id, rid)

    assert resp.status_code == 503, resp.text
    assert resp.json() == {"detail": RESERVATION_UNAVAILABLE_ERROR}
    assert await _job_count() == 0


@pytest.mark.asyncio
async def test_reservation_check_without_an_internal_token_fails_closed(monkeypatch):
    device_id, version_id = await _seed()
    monkeypatch.setattr(apply_jobs.settings, "internal_api_token", "")
    seen: list = []
    _reservations(monkeypatch, lambda request: httpx.Response(200, json=[]), seen)

    resp = await _schedule(USER_PAYLOAD, device_id, version_id, str(uuid.uuid4()))

    assert resp.status_code == 503
    assert resp.json() == {"detail": RESERVATION_UNAVAILABLE_ERROR}
    assert seen == []
    assert await _job_count() == 0


# --- Order: time, 404, 403 before the reservation check -----------------------


@pytest.mark.asyncio
async def test_reservation_check_runs_after_the_time_checks_404s_and_403(monkeypatch):
    """The reservation is asked about only after every earlier refusal: a past
    time is 422, an unknown device or version 404, and a non-admin without
    authority 403, each without a reservations read."""
    device_id, version_id = await _seed()
    seen: list = []
    _reservations(monkeypatch, lambda request: httpx.Response(500), seen)
    rid = str(uuid.uuid4())
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()

    async with _client_as(USER_PAYLOAD) as ac:
        late = await ac.post(
            f"/devices/{device_id}/config-versions/{version_id}/schedule",
            json={"scheduled_for": past, "reservation_id": rid},
        )
        unknown_device = await ac.post(
            f"/devices/{uuid.uuid4()}/config-versions/{version_id}/schedule",
            json={"scheduled_for": _future(), "reservation_id": rid},
        )
        unknown_version = await ac.post(
            f"/devices/{device_id}/config-versions/{uuid.uuid4()}/schedule",
            json={"scheduled_for": _future(), "reservation_id": rid},
        )
        with patch(
            "app.routers.apply_jobs._user_can_manage_device", new=AsyncMock(return_value=False)
        ):
            forbidden = await ac.post(
                f"/devices/{device_id}/config-versions/{version_id}/schedule",
                json={"scheduled_for": _future(), "reservation_id": rid},
            )

    assert late.status_code == 422
    assert late.json() == {"detail": "scheduled_for must be in the future"}
    assert unknown_device.status_code == 404
    assert unknown_device.json() == {"detail": "Device not found"}
    assert unknown_version.status_code == 404
    assert unknown_version.json() == {"detail": "Config version not found"}
    assert forbidden.status_code == 403
    assert seen == []
    assert await _job_count() == 0
