"""Device-configuration rules that held without a test (issue #1100).

CFG-AUTH-6: the write routes do not check device visibility.
CFG-VER-10: a restore without a description is labelled `Restored from v<N>`.
CFG-VER-15: no route deletes or edits a version; deleting the device removes its
versions and apply jobs through the foreign keys.
CFG-JOB-3: the schedule's time checks run before the device and version lookups.
CFG-JOB-13: a confirm repeats none of the schedule-time checks and skips visibility.

The engine enforces foreign keys (PRAGMA foreign_keys=ON), as Postgres does, so the
cascade of CFG-VER-15 runs the same way it does in production.
"""

import io
import json
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from app.config import settings
from app.database import Base, get_db
from app.dependencies.auth import get_current_user_payload
from app.main import app
from app.models.device import Device
from app.models.device_config_apply_job import DeviceConfigApplyJob
from app.models.device_config_version import DeviceConfigVersion
from app.models.driver_package import DriverPackage
from app.models.template import DeviceTemplate
from app.routers import apply_jobs
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)


@event.listens_for(engine.sync_engine, "connect")
def _enable_sqlite_fk(dbapi_connection, _):
    cur = dbapi_connection.cursor()
    cur.execute("PRAGMA foreign_keys=ON")
    cur.close()


TestSessionLocal = async_sessionmaker(engine, expire_on_commit=False)

ADMIN_PAYLOAD = {
    "sub": "00000000-0000-0000-0000-000000000001",
    "username": "admin",
    "role": "admin",
}
USER_PAYLOAD = {
    "sub": "00000000-0000-0000-0000-000000000002",
    "username": "viewer",
    "role": "user",
}

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
def _no_restore_blockers():
    """No other user's reservation holds the device, so a restore proceeds."""
    with patch(
        "app.routers.device_configs.find_blocking_reservations_for_device",
        new=AsyncMock(return_value=[]),
    ):
        yield


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    app.dependency_overrides.clear()


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


async def _seed_device(ac: AsyncClient) -> str:
    """As admin: a device under a Management driver that supports dry runs."""
    drv = await ac.post(
        "/drivers",
        data={"name": f"RuleDrv-{uuid.uuid4().hex[:6]}", "connection_type": "Management"},
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
    return dev.json()["id"]


async def _add_version(ac: AsyncClient, device_id: str, vlan: int, description: str) -> dict:
    resp = await ac.post(
        f"/devices/{device_id}/config-versions",
        json={"config": {"vlan": vlan}, "description": description},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _successful_dry_run(device_id: str, version_id: str, reservation_id=None) -> str:
    """A dry-run job that already ran successfully, written straight to the table."""
    async with TestSessionLocal() as session:
        job = DeviceConfigApplyJob(
            device_id=uuid.UUID(device_id),
            version_id=uuid.UUID(version_id),
            scheduled_for=datetime.now(timezone.utc) - timedelta(minutes=1),
            fired_at=datetime.now(timezone.utc),
            reservation_id=reservation_id,
            dry_run=True,
            status="success",
            created_by=uuid.UUID(ADMIN_PAYLOAD["sub"]),
            author_name="admin",
        )
        session.add(job)
        await session.commit()
        return str(job.id)


def _future(seconds: int = 120) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


async def _count(model) -> int:
    async with TestSessionLocal() as session:
        return (await session.execute(select(func.count()).select_from(model))).scalar_one()


class _ExecutionAnswersSuccess:
    """Stand-in for the immediate apply's execution call: a SUCCESS run."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, json, headers=None):
        class _Resp:
            status_code = 200
            text = ""

            def json(self):
                return {"id": "22222222-2222-2222-2222-222222222222", "status": "SUCCESS"}

        return _Resp()


# --- CFG-AUTH-6 ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_routes_do_not_check_device_visibility():
    """A non-admin outside the device's groups (the version list answers 404) but
    with the authority each write route asks for can still create, restore, apply,
    schedule, and confirm. Only the read consults visibility."""
    async with _client_as(ADMIN_PAYLOAD) as ac:
        device_id = await _seed_device(ac)
        version = await _add_version(ac, device_id, 10, "first")
    job_id = await _successful_dry_run(device_id, version["id"])

    visibility = AsyncMock(return_value=set())
    with (
        patch("app.services.device_visibility._resolve_visible_device_ids", new=visibility),
        patch(
            "app.routers.device_configs._user_can_manage_device",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "app.routers.device_configs._user_has_explicit_manage",
            new=AsyncMock(return_value=True),
        ),
        patch("app.routers.apply_jobs._user_can_manage_device", new=AsyncMock(return_value=True)),
    ):
        async with _client_as(USER_PAYLOAD) as ac:
            hidden = await ac.get(f"/devices/{device_id}/config-versions")
            assert visibility.await_count == 1
            created = await ac.post(
                f"/devices/{device_id}/config-versions", json={"config": {"vlan": 20}}
            )
            restored = await ac.post(
                f"/devices/{device_id}/config-versions/{version['id']}/restore", json={}
            )
            with patch(
                "app.routers.device_configs.httpx.AsyncClient",
                lambda **kw: _ExecutionAnswersSuccess(),
            ):
                applied = await ac.post(
                    f"/devices/{device_id}/config-versions/{version['id']}/apply",
                    headers={"Authorization": "Bearer t"},
                )
            scheduled = await ac.post(
                f"/devices/{device_id}/config-versions/{version['id']}/schedule",
                json={"scheduled_for": _future()},
            )
            confirmed = await ac.post(f"/apply-jobs/{job_id}/confirm")

    assert hidden.status_code == 404
    assert hidden.json() == {"detail": "Device not found"}
    assert created.status_code == 201, created.text
    assert restored.status_code == 201, restored.text
    assert applied.status_code == 200, applied.text
    assert applied.json()["status"] == "success"
    assert scheduled.status_code == 201, scheduled.text
    assert confirmed.status_code == 201, confirmed.text
    # The five writes never asked the visibility question the read asked.
    assert visibility.await_count == 1


# --- CFG-VER-10 ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_restore_without_description_is_labelled_with_the_source_number():
    async with _client_as(ADMIN_PAYLOAD) as ac:
        device_id = await _seed_device(ac)
        v1 = await _add_version(ac, device_id, 10, "first")
        v2 = await _add_version(ac, device_id, 20, "second")

        default = await ac.post(f"/devices/{device_id}/config-versions/{v1['id']}/restore", json={})
        given = await ac.post(
            f"/devices/{device_id}/config-versions/{v2['id']}/restore",
            json={"description": "back to the second one"},
        )

    assert default.status_code == 201, default.text
    assert default.json()["version_number"] == 3
    assert default.json()["description"] == "Restored from v1"
    assert given.status_code == 201, given.text
    assert given.json()["description"] == "back to the second one"


# --- CFG-VER-15 ----------------------------------------------------------------


def test_no_route_deletes_or_edits_a_config_version():
    """Every config-version route reads or appends; none deletes, replaces, or edits."""
    version_routes = [r for r in app.routes if "config-versions" in getattr(r, "path", "")]
    assert version_routes, "the config-version routes were not found on the app"
    for route in version_routes:
        assert route.methods <= {"GET", "HEAD", "POST"}, (route.path, route.methods)
    posts = {r.path for r in version_routes if "POST" in r.methods}
    assert posts == {
        "/devices/{device_id}/config-versions",
        "/devices/{device_id}/config-versions/{version_id}/restore",
        "/devices/{device_id}/config-versions/{version_id}/apply",
        "/devices/{device_id}/config-versions/{version_id}/schedule",
    }


@pytest.mark.asyncio
async def test_version_cannot_be_deleted_or_edited_over_http():
    async with _client_as(ADMIN_PAYLOAD) as ac:
        device_id = await _seed_device(ac)
        version = await _add_version(ac, device_id, 10, "first")
        url = f"/devices/{device_id}/config-versions/{version['id']}"
        deleted = await ac.delete(url)
        replaced = await ac.put(url, json={"config": {"vlan": 99}})
        edited = await ac.patch(url, json={"description": "changed"})
        after = await ac.get(url)

    assert [deleted.status_code, replaced.status_code, edited.status_code] == [405, 405, 405]
    assert after.status_code == 200
    assert after.json()["config"] == {"vlan": 10}
    assert after.json()["description"] == "first"


@pytest.mark.asyncio
async def test_deleting_a_device_deletes_its_versions_and_apply_jobs():
    """The device delete removes only that device's versions and jobs, through the
    ON DELETE CASCADE foreign keys; another device's history is untouched."""
    async with _client_as(ADMIN_PAYLOAD) as ac:
        doomed = await _seed_device(ac)
        kept = await _seed_device(ac)
        doomed_v1 = await _add_version(ac, doomed, 10, "first")
        await _add_version(ac, doomed, 20, "second")
        kept_v1 = await _add_version(ac, kept, 30, "kept")
        for device_id, version in ((doomed, doomed_v1), (kept, kept_v1)):
            sched = await ac.post(
                f"/devices/{device_id}/config-versions/{version['id']}/schedule",
                json={"scheduled_for": _future()},
            )
            assert sched.status_code == 201, sched.text
        await _successful_dry_run(doomed, doomed_v1["id"])
        assert await _count(DeviceConfigVersion) == 3
        assert await _count(DeviceConfigApplyJob) == 3

        with patch("app.routers.devices.assert_device_deletable", new=AsyncMock()):
            resp = await ac.delete(f"/devices/{doomed}")

    assert resp.status_code == 204, resp.text
    async with TestSessionLocal() as session:
        versions = (await session.execute(select(DeviceConfigVersion))).scalars().all()
        jobs = (await session.execute(select(DeviceConfigApplyJob))).scalars().all()
    assert [str(v.device_id) for v in versions] == [kept]
    assert [str(j.device_id) for j in jobs] == [kept]


# --- CFG-JOB-3 -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_schedule_time_checks_run_before_the_device_and_version_lookups():
    """A bad time is 422 even for an unknown device or version, and is answered
    before the authorization check asks anyone."""
    async with _client_as(ADMIN_PAYLOAD) as ac:
        device_id = await _seed_device(ac)
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    too_far = _future(settings.apply_job_max_horizon_days * 86400 + 60)
    horizon_detail = (
        f"scheduled_for must be within {settings.apply_job_max_horizon_days} days from now"
    )

    authority = AsyncMock(return_value=True)
    with patch("app.routers.apply_jobs._user_can_manage_device", new=authority):
        async with _client_as(USER_PAYLOAD) as ac:
            answers = [
                await ac.post(
                    f"/devices/{uuid.uuid4()}/config-versions/{uuid.uuid4()}/schedule",
                    json={"scheduled_for": past},
                ),
                await ac.post(
                    f"/devices/{uuid.uuid4()}/config-versions/{uuid.uuid4()}/schedule",
                    json={"scheduled_for": too_far},
                ),
                await ac.post(
                    f"/devices/{device_id}/config-versions/{uuid.uuid4()}/schedule",
                    json={"scheduled_for": past},
                ),
            ]

    assert [a.status_code for a in answers] == [422, 422, 422]
    assert answers[0].json() == {"detail": "scheduled_for must be in the future"}
    assert answers[1].json() == {"detail": horizon_detail}
    assert answers[2].json() == {"detail": "scheduled_for must be in the future"}
    authority.assert_not_awaited()
    assert await _count(DeviceConfigApplyJob) == 0


# --- CFG-JOB-13 ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_confirm_repeats_no_schedule_time_check():
    """After the source dry run succeeded, the device's driver was swapped to one a
    schedule would refuse (no `configure`, no dry-run support), and the source names
    a reservation reservations would not confirm. A non-admin with authority but
    outside the device's groups still gets the promoted job: the confirm runs no
    reservation, driver, dry-run, horizon, or visibility check. The schedule route,
    asked the same thing, refuses."""
    async with _client_as(ADMIN_PAYLOAD) as ac:
        device_id = await _seed_device(ac)
        version = await _add_version(ac, device_id, 10, "first")
    reservation_id = uuid.uuid4()
    job_id = await _successful_dry_run(device_id, version["id"], reservation_id=reservation_id)
    async with TestSessionLocal() as session:
        device = await session.get(Device, uuid.UUID(device_id))
        template = await session.get(DeviceTemplate, device.template_id)
        driver = await session.get(DriverPackage, template.driver_id)
        driver.connection_type = "Layer 2 Switch"
        driver.supports_dry_run = False
        await session.commit()

    reservations = AsyncMock(side_effect=AssertionError("reservations must not be asked"))
    visibility = AsyncMock(return_value=set())
    before = datetime.now(timezone.utc)
    with (
        patch.object(apply_jobs, "_validate_reservation_for_job", new=reservations),
        patch("app.services.device_visibility._resolve_visible_device_ids", new=visibility),
        patch("app.routers.apply_jobs._user_can_manage_device", new=AsyncMock(return_value=True)),
    ):
        async with _client_as(USER_PAYLOAD) as ac:
            confirmed = await ac.post(f"/apply-jobs/{job_id}/confirm")
            refused = await ac.post(
                f"/devices/{device_id}/config-versions/{version['id']}/schedule",
                json={"scheduled_for": _future(), "dry_run": True},
            )

    assert confirmed.status_code == 201, confirmed.text
    body = confirmed.json()
    assert body["status"] == "pending"
    assert body["dry_run"] is False
    assert body["reservation_id"] == str(reservation_id)
    assert body["created_by"] == USER_PAYLOAD["sub"]
    due = datetime.fromisoformat(body["scheduled_for"])
    if due.tzinfo is None:
        due = due.replace(tzinfo=timezone.utc)
    assert before + timedelta(seconds=9) <= due <= before + timedelta(seconds=15)
    reservations.assert_not_awaited()
    visibility.assert_not_awaited()
    assert refused.status_code == 409
    assert refused.json()["detail"]["error"] == "driver_cannot_configure"
