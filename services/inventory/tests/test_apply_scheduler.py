"""Unit tests for app.services.apply_scheduler.fire_job + _due_jobs.

The full polling loop is integration-shaped; these tests exercise the
deterministic per-job pipeline against an in-memory DB and a fake httpx
client so we can assert the state transitions without timing.
"""

import asyncio
import contextlib
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from app.database import Base
from app.models.device import Device, DeviceStatus
from app.models.device_config_apply_job import DeviceConfigApplyJob
from app.models.device_config_version import DeviceConfigVersion
from app.models.driver_package import DriverPackage
from app.models.template import DeviceTemplate
from app.services import apply_scheduler
from app.services.apply_scheduler import (
    CREATOR_UNAUTHORIZED_ERROR,
    DRIVER_CANNOT_CONFIGURE_ERROR,
    _due_jobs,
    _mark_failed_in_fresh_session,
    _post_internal_execute,
    _reservation_active,
    _resweep_stale_running,
    fire_job,
)
from herd_common.enums import TopologyType
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
TestSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


def _patch_creator_authorized(monkeypatch, allowed: bool = True):
    """Stub the issue #704 fire-time authority re-check.

    Every fire_job test that exercises the pipeline PAST the reservation
    branch needs this: the real check calls out to acl/reservations over
    HTTP, which is not reachable from a unit test. Tests that specifically
    exercise the re-check's own skip/closed-failure behavior patch it
    differently (or not at all, to prove the real function fails closed).
    """

    async def _fake(job):
        return allowed

    monkeypatch.setattr(apply_scheduler, "_creator_still_authorized", _fake)


@pytest.fixture(autouse=True)
async def setup_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


class FakeResponse:
    def __init__(self, status_code: int, body: dict | None = None):
        self.status_code = status_code
        self._body = body or {}
        self.text = ""

    def json(self):
        return self._body


class FakeClient:
    """Async httpx.AsyncClient stand-in driven by a route table."""

    def __init__(
        self,
        post_responses: dict[str, FakeResponse] | None = None,
        get_responses: dict[str, FakeResponse] | None = None,
    ):
        self._post = post_responses or {}
        self._get = get_responses or {}
        self.posts: list[tuple[str, dict, dict]] = []
        self.gets: list[tuple[str, dict]] = []

    async def post(self, url, json=None, headers=None, timeout=None):
        self.posts.append((url, json, headers or {}))
        for key, resp in self._post.items():
            if key in url:
                return resp
        return FakeResponse(500, {"detail": "no route"})

    async def get(self, url, headers=None, timeout=None):
        self.gets.append((url, headers or {}))
        for key, resp in self._get.items():
            if key in url:
                return resp
        return FakeResponse(404, {})


async def _seed_version_and_job(
    db,
    *,
    scheduled_for: datetime,
    reservation_id: uuid.UUID | None = None,
) -> tuple[DeviceConfigVersion, DeviceConfigApplyJob]:
    device_id = uuid.uuid4()
    version = DeviceConfigVersion(
        device_id=device_id,
        version_number=1,
        connection_type="Management",
        config={"vlan": 100},
        created_by=uuid.uuid4(),
        author_name="alice",
    )
    db.add(version)
    await db.flush()
    job = DeviceConfigApplyJob(
        device_id=device_id,
        version_id=version.id,
        scheduled_for=scheduled_for,
        reservation_id=reservation_id,
        status="pending",
        created_by=uuid.uuid4(),
        author_name="alice",
    )
    db.add(job)
    await db.commit()
    return version, job


_driver_counter = 0


async def _seed_device_version_and_job(
    db,
    *,
    connection_type: str,
    scheduled_for: datetime,
) -> tuple[Device, DeviceConfigVersion, DeviceConfigApplyJob]:
    """Like _seed_version_and_job, but with a REAL Device/Template/Driver row
    (issue #839/#840 fire-time gate needs the device's current driver, not
    the version's frozen connection_type)."""
    global _driver_counter
    _driver_counter += 1
    driver = DriverPackage(
        name=f"SchedDrv{_driver_counter}",
        connection_type=connection_type,
        filename="d.zip",
        storage_key=f"drivers/sched-{_driver_counter}",
        size_bytes=10,
        sha256=f"sha-{_driver_counter}",
        uploaded_by="admin",
    )
    db.add(driver)
    await db.flush()
    template = DeviceTemplate(
        name=f"SchedTpl{_driver_counter}",
        driver_id=driver.id,
        sections=[],
    )
    db.add(template)
    await db.flush()
    device = Device(
        name=f"sched-dev-{_driver_counter}",
        template_id=template.id,
        topology_type=TopologyType.PHYSICAL,
        status=DeviceStatus.AVAILABLE,
        field_data={},
    )
    db.add(device)
    await db.flush()
    version = DeviceConfigVersion(
        device_id=device.id,
        version_number=1,
        connection_type=connection_type,
        config={"vlan": 100} if connection_type == "Management" else {"routes": []},
        created_by=uuid.uuid4(),
        author_name="alice",
    )
    db.add(version)
    await db.flush()
    job = DeviceConfigApplyJob(
        device_id=device.id,
        version_id=version.id,
        scheduled_for=scheduled_for,
        status="pending",
        created_by=uuid.uuid4(),
        author_name="alice",
    )
    db.add(job)
    await db.commit()
    return device, version, job


@pytest.mark.asyncio
async def test_due_jobs_returns_only_pending_and_due():
    async with TestSessionLocal() as db:
        now = datetime.now(timezone.utc)
        # Pending due
        await _seed_version_and_job(db, scheduled_for=now - timedelta(seconds=10))
        # Pending future
        await _seed_version_and_job(db, scheduled_for=now + timedelta(minutes=5))

        due = await _due_jobs(db, now)
        assert len(due) == 1
        # SQLite returns naïve timestamps; compare on the unix epoch instead.
        assert due[0].scheduled_for.replace(tzinfo=timezone.utc) < now


@pytest.mark.asyncio
async def test_fire_job_success(monkeypatch):
    async with TestSessionLocal() as db:
        now = datetime.now(timezone.utc)
        _, job = await _seed_version_and_job(db, scheduled_for=now)

        monkeypatch.setattr(
            "app.services.apply_scheduler.settings.internal_api_token", "token", raising=False
        )
        _patch_creator_authorized(monkeypatch)
        run_id = "11111111-1111-1111-1111-111111111111"
        client = FakeClient(
            post_responses={
                "/execute/internal": FakeResponse(201, {"id": run_id, "status": "SUCCESS"}),
            }
        )
        await fire_job(db, job, client)
        await db.refresh(job)
        assert job.status == "success"
        assert str(job.run_id) == run_id
        assert job.fired_at is not None


@pytest.mark.asyncio
async def test_fire_job_failed_when_execution_returns_500(monkeypatch):
    async with TestSessionLocal() as db:
        now = datetime.now(timezone.utc)
        _, job = await _seed_version_and_job(db, scheduled_for=now)

        monkeypatch.setattr(
            "app.services.apply_scheduler.settings.internal_api_token", "token", raising=False
        )
        _patch_creator_authorized(monkeypatch)
        client = FakeClient(
            post_responses={
                "/execute/internal": FakeResponse(500, {"detail": "boom"}),
            }
        )
        await fire_job(db, job, client)
        await db.refresh(job)
        assert job.status == "failed"
        assert "500" in (job.error or "")


@pytest.mark.asyncio
async def test_fire_job_skipped_when_reservation_not_active(monkeypatch):
    async with TestSessionLocal() as db:
        now = datetime.now(timezone.utc)
        reservation_id = uuid.uuid4()
        _, job = await _seed_version_and_job(db, scheduled_for=now, reservation_id=reservation_id)

        monkeypatch.setattr(
            "app.services.apply_scheduler.settings.internal_api_token", "token", raising=False
        )
        client = FakeClient(
            get_responses={
                str(reservation_id): FakeResponse(
                    200, {"id": str(reservation_id), "status": "COMPLETED", "is_active": False}
                )
            },
        )
        await fire_job(db, job, client)
        await db.refresh(job)
        assert job.status == "skipped"
        assert "reservation" in (job.error or "")


@pytest.mark.asyncio
async def test_fire_job_proceeds_when_reservation_active(monkeypatch):
    async with TestSessionLocal() as db:
        now = datetime.now(timezone.utc)
        reservation_id = uuid.uuid4()
        _, job = await _seed_version_and_job(db, scheduled_for=now, reservation_id=reservation_id)

        monkeypatch.setattr(
            "app.services.apply_scheduler.settings.internal_api_token", "token", raising=False
        )
        _patch_creator_authorized(monkeypatch)
        client = FakeClient(
            get_responses={
                str(reservation_id): FakeResponse(
                    200, {"id": str(reservation_id), "status": "ACTIVE", "is_active": True}
                )
            },
            post_responses={
                "/execute/internal": FakeResponse(
                    201,
                    {"id": "22222222-2222-2222-2222-222222222222", "status": "SUCCESS"},
                ),
            },
        )
        await fire_job(db, job, client)
        await db.refresh(job)
        assert job.status == "success"


# --- Fire-time creator authority re-check (issue #704) ----------------------


@pytest.mark.asyncio
async def test_fire_job_skips_when_creator_no_longer_authorized(monkeypatch):
    """Spec #704 test (1): null reservation_id, creator no longer qualifies
    -> skipped with the pinned error and no execution call attempted."""
    async with TestSessionLocal() as db:
        now = datetime.now(timezone.utc)
        _, job = await _seed_version_and_job(db, scheduled_for=now)

        monkeypatch.setattr(
            "app.services.apply_scheduler.settings.internal_api_token", "token", raising=False
        )
        _patch_creator_authorized(monkeypatch, allowed=False)
        client = FakeClient(
            post_responses={
                "/execute/internal": FakeResponse(201, {"id": "x", "status": "SUCCESS"}),
            }
        )
        await fire_job(db, job, client)
        await db.refresh(job)
        assert job.status == "skipped"
        assert job.error == CREATOR_UNAUTHORIZED_ERROR
        assert client.posts == []


@pytest.mark.asyncio
async def test_fire_job_skips_when_reservation_active_but_creator_unauthorized(monkeypatch):
    """Spec #704 test (2): the reservation_id branch passing (reservation is
    active) does not substitute for the creator's own standing; the job
    still skips on the pinned authority error, not the reservation error."""
    async with TestSessionLocal() as db:
        now = datetime.now(timezone.utc)
        reservation_id = uuid.uuid4()
        _, job = await _seed_version_and_job(db, scheduled_for=now, reservation_id=reservation_id)

        monkeypatch.setattr(
            "app.services.apply_scheduler.settings.internal_api_token", "token", raising=False
        )
        _patch_creator_authorized(monkeypatch, allowed=False)
        client = FakeClient(
            get_responses={
                str(reservation_id): FakeResponse(
                    200, {"id": str(reservation_id), "status": "ACTIVE", "is_active": True}
                )
            },
            post_responses={
                "/execute/internal": FakeResponse(201, {"id": "x", "status": "SUCCESS"}),
            },
        )
        await fire_job(db, job, client)
        await db.refresh(job)
        assert job.status == "skipped"
        assert job.error == CREATOR_UNAUTHORIZED_ERROR
        assert client.posts == []


@pytest.mark.asyncio
async def test_fire_job_positive_control_authorized_creator_fires(monkeypatch):
    """Spec #704 test (3): positive control, an authorized creator's job
    still fires and resolves success once the new gate is satisfied."""
    async with TestSessionLocal() as db:
        now = datetime.now(timezone.utc)
        _, job = await _seed_version_and_job(db, scheduled_for=now)

        monkeypatch.setattr(
            "app.services.apply_scheduler.settings.internal_api_token", "token", raising=False
        )
        _patch_creator_authorized(monkeypatch, allowed=True)
        run_id = "44444444-4444-4444-4444-444444444444"
        client = FakeClient(
            post_responses={
                "/execute/internal": FakeResponse(201, {"id": run_id, "status": "SUCCESS"}),
            }
        )
        await fire_job(db, job, client)
        await db.refresh(job)
        assert job.status == "success"
        assert str(job.run_id) == run_id


@pytest.mark.asyncio
async def test_fire_job_skips_when_authority_check_unreachable(monkeypatch):
    """Spec #704 test (4): the authority check itself failing closed (auth
    or acl unreachable) skips the job, exercised end to end through the
    real herd_common helper rather than a stubbed _creator_still_authorized,
    by making the underlying httpx.AsyncClient raise a connection error for
    every call the helper makes.
    """

    class _RaisingAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def request(self, method, url, **kwargs):
            raise httpx.ConnectError("acl/auth unreachable")

    async with TestSessionLocal() as db:
        now = datetime.now(timezone.utc)
        _, job = await _seed_version_and_job(db, scheduled_for=now)

        monkeypatch.setattr(
            "app.services.apply_scheduler.settings.internal_api_token", "token", raising=False
        )
        monkeypatch.setattr("herd_common.internal_client.httpx.AsyncClient", _RaisingAsyncClient)
        client = FakeClient(
            post_responses={
                "/execute/internal": FakeResponse(201, {"id": "x", "status": "SUCCESS"}),
            }
        )
        await fire_job(db, job, client)
        await db.refresh(job)
        assert job.status == "skipped"
        assert job.error == CREATOR_UNAUTHORIZED_ERROR
        assert client.posts == []


@pytest.mark.asyncio
async def test_reservation_gate_hits_internal_url(monkeypatch):
    """Regression: the gate must call /internal/{id}, not the JWT-protected /{id}.

    The pre-fix code was sending X-Internal-Token to the JWT-protected detail
    endpoint and getting a silent 401 every tick, which made the gate dead.
    """
    async with TestSessionLocal() as db:
        now = datetime.now(timezone.utc)
        reservation_id = uuid.uuid4()
        _, job = await _seed_version_and_job(db, scheduled_for=now, reservation_id=reservation_id)

        monkeypatch.setattr(
            "app.services.apply_scheduler.settings.internal_api_token", "token", raising=False
        )
        client = FakeClient(
            get_responses={
                str(reservation_id): FakeResponse(200, {"is_active": False}),
            },
        )
        await fire_job(db, job, client)

        assert len(client.gets) == 1
        url, headers = client.gets[0]
        assert "/internal/" in url
        assert url.endswith(f"/internal/{reservation_id}")
        assert headers.get("X-Internal-Token") == "token"


@pytest.mark.asyncio
async def test_reservation_gate_closed_default_when_token_missing(monkeypatch):
    """When internal_api_token is unset, gate returns False (do not fire).

    Closed-default behavior is intentional: an unreachable gate must not let a
    job through.
    """
    async with TestSessionLocal() as db:
        now = datetime.now(timezone.utc)
        reservation_id = uuid.uuid4()
        _, job = await _seed_version_and_job(db, scheduled_for=now, reservation_id=reservation_id)

        monkeypatch.setattr(
            "app.services.apply_scheduler.settings.internal_api_token", "", raising=False
        )
        client = FakeClient()
        await fire_job(db, job, client)
        await db.refresh(job)
        assert job.status == "skipped"
        # No GET should even be attempted when token is unset.
        assert client.gets == []


@pytest.mark.asyncio
async def test_reservation_gate_closed_default_on_403(monkeypatch):
    """A 403 from the reservations service must close the gate, not let through."""
    async with TestSessionLocal() as db:
        now = datetime.now(timezone.utc)
        reservation_id = uuid.uuid4()
        _, job = await _seed_version_and_job(db, scheduled_for=now, reservation_id=reservation_id)

        monkeypatch.setattr(
            "app.services.apply_scheduler.settings.internal_api_token", "token", raising=False
        )
        client = FakeClient(
            get_responses={
                str(reservation_id): FakeResponse(403, {"detail": "Invalid internal token"})
            },
        )
        await fire_job(db, job, client)
        await db.refresh(job)
        assert job.status == "skipped"


@pytest.mark.asyncio
async def test_fire_job_bails_when_already_claimed(monkeypatch):
    """Lost-race: if another replica flipped status to running first, bail without firing.

    Simulates the race by externally flipping the job to running before fire_job
    runs. fire_job's conditional UPDATE finds rowcount=0 and returns without
    calling execution.
    """
    async with TestSessionLocal() as db:
        now = datetime.now(timezone.utc)
        _, job = await _seed_version_and_job(db, scheduled_for=now)

        # Race winner: another replica claimed it.
        job.status = "running"
        await db.commit()
        original_status = job.status

        monkeypatch.setattr(
            "app.services.apply_scheduler.settings.internal_api_token", "token", raising=False
        )
        client = FakeClient(
            post_responses={
                "/execute/internal": FakeResponse(201, {"id": "deadbeef", "status": "SUCCESS"}),
            }
        )
        await fire_job(db, job, client)

        # No POST to execution; status unchanged from what the race winner set.
        assert client.posts == []
        await db.refresh(job)
        assert job.status == original_status


@pytest.mark.asyncio
async def test_fire_job_fails_when_version_was_deleted(monkeypatch):
    async with TestSessionLocal() as db:
        now = datetime.now(timezone.utc)
        version, job = await _seed_version_and_job(db, scheduled_for=now)
        # Simulate version deletion.
        await db.delete(version)
        await db.commit()

        monkeypatch.setattr(
            "app.services.apply_scheduler.settings.internal_api_token", "token", raising=False
        )
        _patch_creator_authorized(monkeypatch)
        client = FakeClient()
        await fire_job(db, job, client)
        await db.refresh(job)
        assert job.status == "failed"
        assert "no longer exists" in (job.error or "")


@pytest.mark.asyncio
async def test_resweep_stale_running_requeues():
    """A row in `running` past the stale threshold with fired_at=None is re-queued."""
    async with TestSessionLocal() as db:
        # Seed stale: scheduled 10 minutes ago, never finished.
        stale_at = datetime.now(timezone.utc) - timedelta(minutes=10)
        version, job = await _seed_version_and_job(db, scheduled_for=stale_at)
        job.status = "running"
        await db.commit()

        affected = await _resweep_stale_running(db, datetime.now(timezone.utc))
        assert affected == 1
        await db.refresh(job)
        assert job.status == "pending"


@pytest.mark.asyncio
async def test_resweep_leaves_fresh_running_alone():
    """A row that just claimed `running` (scheduled_for recent) is not requeued."""
    async with TestSessionLocal() as db:
        # Seed fresh: scheduled 1 minute ago, running normally.
        fresh_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        _, job = await _seed_version_and_job(db, scheduled_for=fresh_at)
        job.status = "running"
        await db.commit()

        affected = await _resweep_stale_running(db, datetime.now(timezone.utc))
        assert affected == 0
        await db.refresh(job)
        assert job.status == "running"


@pytest.mark.asyncio
async def test_resweep_leaves_terminal_jobs_alone():
    """Terminal rows (fired_at set) must not be touched, even if scheduled_for is old."""
    async with TestSessionLocal() as db:
        stale_at = datetime.now(timezone.utc) - timedelta(minutes=20)
        _, job = await _seed_version_and_job(db, scheduled_for=stale_at)
        # Simulate a terminal flip on an old row.
        job.status = "success"
        job.fired_at = datetime.now(timezone.utc) - timedelta(minutes=15)
        await db.commit()

        affected = await _resweep_stale_running(db, datetime.now(timezone.utc))
        assert affected == 0
        await db.refresh(job)
        assert job.status == "success"


@pytest.mark.asyncio
async def test_mark_failed_in_fresh_session_records_failure():
    """Verify the fresh-session crash recorder writes a terminal `failed` row."""
    async with TestSessionLocal() as db:
        now = datetime.now(timezone.utc)
        _, job = await _seed_version_and_job(db, scheduled_for=now)
        job.status = "running"
        await db.commit()
        job_id = job.id

    await _mark_failed_in_fresh_session(TestSessionLocal, job_id, "scheduler crash")

    async with TestSessionLocal() as db:
        refreshed = await db.get(DeviceConfigApplyJob, job_id)
        assert refreshed is not None
        assert refreshed.status == "failed"
        assert refreshed.error == "scheduler crash"
        assert refreshed.fired_at is not None


# --- _reservation_active direct branches ------------------------------------


class _RaisingGetClient:
    def __init__(self, exc):
        self._exc = exc

    async def get(self, url, headers=None, timeout=None):
        raise self._exc

    async def post(self, url, json=None, headers=None, timeout=None):
        raise self._exc


class _MalformedJSONResponse:
    def __init__(self, status_code=200):
        self.status_code = status_code
        self.text = "<<not json>>"

    def json(self):
        raise ValueError("no json here")


@pytest.mark.asyncio
async def test_reservation_active_http_error_returns_false(monkeypatch):
    import httpx

    monkeypatch.setattr(
        "app.services.apply_scheduler.settings.internal_api_token", "token", raising=False
    )
    client = _RaisingGetClient(httpx.ConnectError("down"))
    assert await _reservation_active(client, uuid.uuid4()) is False


@pytest.mark.asyncio
async def test_reservation_active_malformed_json_returns_false(monkeypatch):
    monkeypatch.setattr(
        "app.services.apply_scheduler.settings.internal_api_token", "token", raising=False
    )

    class _Client:
        async def get(self, url, headers=None, timeout=None):
            return _MalformedJSONResponse(200)

    assert await _reservation_active(_Client(), uuid.uuid4()) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [[{"is_active": True}], "true", 1, None])
async def test_reservation_active_answer_not_an_object_returns_false(monkeypatch, body):
    """A 200 whose JSON is not an object means "do not fire", like any other
    unusable answer, and never raises (issue #1096)."""
    monkeypatch.setattr(
        "app.services.apply_scheduler.settings.internal_api_token", "token", raising=False
    )

    class _Resp:
        status_code = 200

        def json(self):
            return body

    class _Client:
        async def get(self, url, headers=None, timeout=None):
            return _Resp()

    assert await _reservation_active(_Client(), uuid.uuid4()) is False


# --- _post_internal_execute direct branches ---------------------------------


def _make_job():
    return DeviceConfigApplyJob(
        device_id=uuid.uuid4(),
        version_id=uuid.uuid4(),
        scheduled_for=datetime.now(timezone.utc),
        status="running",
        created_by=uuid.uuid4(),
        author_name="alice",
        dry_run=False,
    )


@pytest.mark.asyncio
async def test_post_internal_execute_http_error(monkeypatch):
    import httpx

    monkeypatch.setattr(
        "app.services.apply_scheduler.settings.internal_api_token", "token", raising=False
    )
    client = _RaisingGetClient(httpx.ConnectError("execution down"))
    status, run_id, error = await _post_internal_execute(client, _make_job(), {"vlan": 1})
    assert status == "failed"
    assert run_id is None
    # Class name only (issue #1093): the exception text never reaches the row.
    assert error == "execution service unreachable (ConnectError)"


@pytest.mark.asyncio
async def test_post_internal_execute_error_body_not_json(monkeypatch):
    monkeypatch.setattr(
        "app.services.apply_scheduler.settings.internal_api_token", "token", raising=False
    )

    class _Client:
        async def post(self, url, json=None, headers=None, timeout=None):
            return _MalformedJSONResponse(503)

    status, run_id, error = await _post_internal_execute(_Client(), _make_job(), {})
    assert status == "failed"
    assert error == "execution answered HTTP 503"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body, expected",
    [
        (
            {"detail": {"error": "driver_cannot_configure", "message": "No configure method."}},
            "execution answered HTTP 409: No configure method.",
        ),
        ({"detail": "Failed to fetch device: http://inventory:8000 refused"}, None),
    ],
)
async def test_post_internal_execute_refusal_stores_herd_text_only(monkeypatch, body, expected):
    """The job row's error is the status plus a structured detail's message,
    never a plain upstream string (issue #1093)."""
    import httpx

    monkeypatch.setattr(
        "app.services.apply_scheduler.settings.internal_api_token", "token", raising=False
    )

    class _Client:
        async def post(self, url, json=None, headers=None, timeout=None):
            return httpx.Response(409, json=body)

    status, run_id, error = await _post_internal_execute(_Client(), _make_job(), {})
    assert status == "failed"
    assert run_id is None
    assert error == (expected or "execution answered HTTP 409")


@pytest.mark.asyncio
async def test_post_internal_execute_success_body_not_json(monkeypatch):
    monkeypatch.setattr(
        "app.services.apply_scheduler.settings.internal_api_token", "token", raising=False
    )

    class _Client:
        async def post(self, url, json=None, headers=None, timeout=None):
            return _MalformedJSONResponse(200)

    status, run_id, error = await _post_internal_execute(_Client(), _make_job(), {})
    assert status == "failed"
    assert "malformed JSON" in error


@pytest.mark.asyncio
async def test_post_internal_execute_malformed_run_id_degrades_to_none(monkeypatch):
    monkeypatch.setattr(
        "app.services.apply_scheduler.settings.internal_api_token", "token", raising=False
    )
    client = FakeClient(
        post_responses={
            "/execute/internal": FakeResponse(200, {"id": "not-a-uuid", "status": "SUCCESS"}),
        }
    )
    status, run_id, error = await _post_internal_execute(client, _make_job(), {})
    assert status == "success"
    assert run_id is None


@pytest.mark.asyncio
async def test_post_internal_execute_non_success_status(monkeypatch):
    monkeypatch.setattr(
        "app.services.apply_scheduler.settings.internal_api_token", "token", raising=False
    )
    client = FakeClient(
        post_responses={
            "/execute/internal": FakeResponse(
                200, {"id": None, "status": "FAILED", "error": "device unreachable"}
            ),
        }
    )
    status, run_id, error = await _post_internal_execute(client, _make_job(), {})
    assert status == "failed"
    assert error == "device unreachable"


@pytest.mark.asyncio
async def test_post_internal_execute_missing_status_records_failed(monkeypatch):
    """Issue #720: a response body with no status key is never a success. The
    contract makes status required today, so this pins the safe default for
    the day it loosens."""
    monkeypatch.setattr(
        "app.services.apply_scheduler.settings.internal_api_token", "token", raising=False
    )
    run_id = "33333333-3333-3333-3333-333333333333"
    client = FakeClient(post_responses={"/execute/internal": FakeResponse(200, {"id": run_id})})
    status, got_run_id, error = await _post_internal_execute(client, _make_job(), {})
    assert status == "failed"
    assert got_run_id == uuid.UUID(run_id)
    assert error == "execution returned non-success status"


@pytest.mark.asyncio
async def test_post_internal_execute_null_status_records_failed(monkeypatch):
    """An explicit null status takes the same path: str(None) is not SUCCESS."""
    monkeypatch.setattr(
        "app.services.apply_scheduler.settings.internal_api_token", "token", raising=False
    )
    client = FakeClient(
        post_responses={"/execute/internal": FakeResponse(200, {"id": None, "status": None})}
    )
    status, got_run_id, error = await _post_internal_execute(client, _make_job(), {})
    assert status == "failed"
    assert got_run_id is None
    assert error == "execution returned non-success status"


# --- _mark_failed_in_fresh_session swallows secondary errors ----------------


@pytest.mark.asyncio
async def test_mark_failed_in_fresh_session_swallows_session_error():
    """If the fresh session itself raises, the recorder must not propagate."""

    class _BoomSession:
        async def __aenter__(self):
            raise RuntimeError("cannot open session")

        async def __aexit__(self, *a):
            return False

    def _factory():
        return _BoomSession()

    # Must not raise.
    await _mark_failed_in_fresh_session(_factory, uuid.uuid4(), "crash")


# --- run_scheduler_loop fires a due job -------------------------------------


@pytest.mark.asyncio
async def test_run_scheduler_loop_fires_due_jobs(monkeypatch):
    """One healthy tick: a due job is fetched and fire_job is invoked for it,
    then the loop is cancelled. Covers the due-job firing branch of the loop."""
    real_sleep = asyncio.sleep
    now = datetime.now(timezone.utc)
    async with TestSessionLocal() as db:
        _, job = await _seed_version_and_job(db, scheduled_for=now - timedelta(seconds=5))
        job_id = job.id

    fired: list[uuid.UUID] = []

    async def fake_resweep(db, now):
        return 0

    async def fake_fire(db, job, client):
        fired.append(job.id)

    async def fake_sleep(seconds):
        await real_sleep(0)

    monkeypatch.setattr(apply_scheduler, "_resweep_stale_running", fake_resweep)
    monkeypatch.setattr(apply_scheduler, "fire_job", fake_fire)
    monkeypatch.setattr(apply_scheduler.asyncio, "sleep", fake_sleep)

    task = asyncio.create_task(apply_scheduler.run_scheduler_loop(TestSessionLocal))
    for _ in range(500):
        await real_sleep(0.001)
        if fired:
            break
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert job_id in fired


@pytest.mark.asyncio
async def test_run_scheduler_loop_recovers_when_fire_job_crashes(monkeypatch):
    """If fire_job raises for a due job, the loop logs it and records the failure
    via the fresh-session recorder, then continues (covers the crash branch)."""
    real_sleep = asyncio.sleep
    now = datetime.now(timezone.utc)
    async with TestSessionLocal() as db:
        _, job = await _seed_version_and_job(db, scheduled_for=now - timedelta(seconds=5))
        job_id = job.id

    recorded: list[uuid.UUID] = []

    async def fake_resweep(db, now):
        return 0

    async def crashing_fire(db, job, client):
        raise RuntimeError("driver blew up")

    async def fake_mark_failed(session_factory, jid, error):
        recorded.append(jid)

    async def fake_sleep(seconds):
        await real_sleep(0)

    monkeypatch.setattr(apply_scheduler, "_resweep_stale_running", fake_resweep)
    monkeypatch.setattr(apply_scheduler, "fire_job", crashing_fire)
    monkeypatch.setattr(apply_scheduler, "_mark_failed_in_fresh_session", fake_mark_failed)
    monkeypatch.setattr(apply_scheduler.asyncio, "sleep", fake_sleep)

    task = asyncio.create_task(apply_scheduler.run_scheduler_loop(TestSessionLocal))
    for _ in range(500):
        await real_sleep(0.001)
        if recorded:
            break
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert job_id in recorded


@pytest.mark.asyncio
async def test_run_scheduler_loop_backs_off_on_db_failure_then_recovers(monkeypatch):
    """A failing tick (simulated DB outage) is caught by the outer except,
    backoff fires, and the next tick proceeds normally. Validates that the
    loop does not crash or busy-loop on transient DB errors."""
    real_sleep = (
        asyncio.sleep
    )  # capture before monkeypatching, used inside fake_sleep + polling loop
    call_count = {"n": 0}
    sleeps: list[float] = []

    async def fake_due_jobs(db, now):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise ConnectionError("simulated DB outage")
        return []

    async def fake_resweep(db, now):
        return None

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        await real_sleep(0)

    monkeypatch.setattr(apply_scheduler, "_due_jobs", fake_due_jobs)
    monkeypatch.setattr(apply_scheduler, "_resweep_stale_running", fake_resweep)
    monkeypatch.setattr(apply_scheduler.asyncio, "sleep", fake_sleep)

    task = asyncio.create_task(apply_scheduler.run_scheduler_loop(TestSessionLocal))

    # Wait for both sleeps to be recorded (one per tick). Polling on call_count
    # would cancel between the second _due_jobs return and the second sleep.
    for _ in range(100):
        await real_sleep(0)
        if len(sleeps) >= 2:
            break

    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert call_count["n"] >= 2, (
        f"expected loop to survive failure and run a 2nd tick, got {call_count['n']}"
    )
    assert len(sleeps) >= 2, "expected at least 2 sleeps (one per tick)"
    assert sleeps[0] > sleeps[1], (
        "expected backoff to double after failed tick and reset on successful tick: "
        f"sleeps[0]={sleeps[0]} should exceed sleeps[1]={sleeps[1]}"
    )


# --- Fire-time driver-capability gate (issues #839/#840) --------------------


@pytest.mark.asyncio
async def test_fire_job_fails_when_driver_cannot_configure(monkeypatch):
    """A job whose device's CURRENT driver has no configure contract (e.g.
    queued before the schedule-time gate existed, or the driver was swapped
    after scheduling) fails at fire time with the pinned error and never
    calls execution."""
    async with TestSessionLocal() as db:
        now = datetime.now(timezone.utc)
        device, _version, job = await _seed_device_version_and_job(
            db, connection_type="Layer 3 Switch", scheduled_for=now
        )
        # Force fire_job's db.get(Device, ...) to issue a fresh, eager-joined
        # SELECT rather than returning the just-created, relationship-unloaded
        # object straight out of this session's identity map (this session
        # doubles as both the seeder and fire_job's caller, unlike production,
        # where the scheduler loop always uses its own fresh session). Expire
        # only `device`, not the whole session: expiring `job` too would make
        # fire_job's own `job.id` access hit the same sync-lazy-load trap.
        db.expire(device)

        monkeypatch.setattr(
            "app.services.apply_scheduler.settings.internal_api_token", "token", raising=False
        )
        _patch_creator_authorized(monkeypatch)
        client = FakeClient(
            post_responses={
                "/execute/internal": FakeResponse(201, {"id": "x", "status": "SUCCESS"}),
            }
        )
        await fire_job(db, job, client)
        await db.refresh(job)
        assert job.status == "failed"
        assert job.error == DRIVER_CANNOT_CONFIGURE_ERROR
        assert client.posts == []


@pytest.mark.asyncio
async def test_fire_job_proceeds_when_driver_can_configure(monkeypatch):
    """Control case: a Management-driver device is unaffected and still fires
    normally through to success."""
    async with TestSessionLocal() as db:
        now = datetime.now(timezone.utc)
        device, _version, job = await _seed_device_version_and_job(
            db, connection_type="Management", scheduled_for=now
        )
        db.expire(device)

        monkeypatch.setattr(
            "app.services.apply_scheduler.settings.internal_api_token", "token", raising=False
        )
        _patch_creator_authorized(monkeypatch)
        client = FakeClient(
            post_responses={
                "/execute/internal": FakeResponse(
                    201, {"id": "11111111-1111-1111-1111-111111111111", "status": "SUCCESS"}
                ),
            }
        )
        await fire_job(db, job, client)
        await db.refresh(job)
        assert job.status == "success"
        assert len(client.posts) == 1


@pytest.mark.asyncio
async def test_fire_job_unresolvable_device_is_left_to_existing_behavior(monkeypatch):
    """Pin today's behavior for a job whose device row cannot be resolved at
    all (issue #839's 'no driver resolvable' rule, extended: no Device row is
    an even more extreme case). The gate is a no-op and firing proceeds
    exactly as it did before this change, calling execution."""
    async with TestSessionLocal() as db:
        now = datetime.now(timezone.utc)
        # _seed_version_and_job uses a synthetic device_id with NO Device row.
        _, job = await _seed_version_and_job(db, scheduled_for=now)

        monkeypatch.setattr(
            "app.services.apply_scheduler.settings.internal_api_token", "token", raising=False
        )
        _patch_creator_authorized(monkeypatch)
        run_id = "22222222-2222-2222-2222-222222222222"
        client = FakeClient(
            post_responses={
                "/execute/internal": FakeResponse(201, {"id": run_id, "status": "SUCCESS"}),
            }
        )
        await fire_job(db, job, client)
        await db.refresh(job)
        assert job.status == "success"
        assert str(job.run_id) == run_id
        assert len(client.posts) == 1


# ---- stale sweep measured from the claim (issue #1089) ----


@pytest.mark.asyncio
async def test_claim_records_claimed_at(monkeypatch):
    async with TestSessionLocal() as db:
        now = datetime.now(timezone.utc)
        _, job = await _seed_version_and_job(db, scheduled_for=now - timedelta(hours=1))
        assert job.claimed_at is None
        _patch_creator_authorized(monkeypatch, allowed=False)
        await fire_job(db, job, FakeClient())
        await db.refresh(job)
        assert job.status == "skipped"
        claimed = job.claimed_at.replace(tzinfo=timezone.utc)
        assert now - timedelta(seconds=5) <= claimed <= datetime.now(timezone.utc)


@pytest.mark.asyncio
async def test_sweep_does_not_requeue_a_late_claimed_job_still_firing(monkeypatch):
    """A job scheduled an hour ago is claimed now (a backlog or an outage). While
    its execute call is in flight another scheduler's tick runs the sweep and then
    looks for due jobs: the job must stay running and be fired exactly once."""
    async with TestSessionLocal() as db:
        now = datetime.now(timezone.utc)
        _, job = await _seed_version_and_job(db, scheduled_for=now - timedelta(hours=1))
        _patch_creator_authorized(monkeypatch)
        seen: dict = {}
        posts: list = []

        async def execute_while_peer_ticks(client, fired_job, config):
            posts.append(fired_job.id)
            async with TestSessionLocal() as peer:
                seen["requeued"] = await _resweep_stale_running(peer, datetime.now(timezone.utc))
                seen["due"] = await _due_jobs(peer, datetime.now(timezone.utc))
                row = await peer.get(DeviceConfigApplyJob, fired_job.id)
                await peer.refresh(row)
                seen["status"] = row.status
            return "success", None, None

        monkeypatch.setattr(apply_scheduler, "_post_internal_execute", execute_while_peer_ticks)
        await fire_job(db, job, FakeClient())
        await db.refresh(job)

    assert seen == {"requeued": 0, "due": [], "status": "running"}
    assert posts == [job.id]
    assert job.status == "success"


@pytest.mark.asyncio
async def test_sweep_requeues_a_job_claimed_past_the_threshold_and_clears_the_claim():
    async with TestSessionLocal() as db:
        now = datetime.now(timezone.utc)
        _, job = await _seed_version_and_job(db, scheduled_for=now - timedelta(minutes=1))
        job.status = "running"
        job.claimed_at = now - timedelta(minutes=6)
        await db.commit()

        affected = await _resweep_stale_running(db, now)
        assert affected == 1
        await db.refresh(job)
        assert job.status == "pending"
        assert job.claimed_at is None


@pytest.mark.asyncio
async def test_sweep_leaves_a_recent_claim_alone_whatever_its_scheduled_time():
    async with TestSessionLocal() as db:
        now = datetime.now(timezone.utc)
        _, job = await _seed_version_and_job(db, scheduled_for=now - timedelta(days=2))
        job.status = "running"
        job.claimed_at = now - timedelta(minutes=4)
        await db.commit()

        assert await _resweep_stale_running(db, now) == 0
        await db.refresh(job)
        assert job.status == "running"


# ---- a scheduled success moves the current config pointer (issue #1094) ----


async def _pointer(device_id):
    async with TestSessionLocal() as s:
        device = await s.get(Device, device_id)
        return device.current_config_version_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("dry_run", "answer", "moves"),
    [
        (False, FakeResponse(201, {"id": str(uuid.uuid4()), "status": "SUCCESS"}), True),
        (True, FakeResponse(201, {"id": str(uuid.uuid4()), "status": "SUCCESS"}), False),
        (False, FakeResponse(201, {"id": str(uuid.uuid4()), "status": "FAILED"}), False),
        (False, FakeResponse(201, {"id": str(uuid.uuid4())}), False),
    ],
)
async def test_scheduled_apply_moves_pointer_only_on_a_real_success(
    monkeypatch, dry_run, answer, moves
):
    async with TestSessionLocal() as db:
        device, version, job = await _seed_device_version_and_job(
            db, connection_type="Management", scheduled_for=datetime.now(timezone.utc)
        )
        device_id, version_id = device.id, version.id
        job.dry_run = dry_run
        await db.commit()
        db.expire(device)
        _patch_creator_authorized(monkeypatch)
        await fire_job(db, job, FakeClient(post_responses={"/execute/internal": answer}))
        await db.refresh(job)
        status = job.status
    assert status == ("success" if answer._body.get("status") == "SUCCESS" else "failed")
    assert await _pointer(device_id) == (version_id if moves else None)


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [["SUCCESS"], "SUCCESS", 7])
async def test_post_internal_execute_non_object_json_records_failed(monkeypatch, body):
    async with TestSessionLocal() as db:
        _, job = await _seed_version_and_job(db, scheduled_for=datetime.now(timezone.utc))
    client = FakeClient(post_responses={"/execute/internal": FakeResponse(201, None)})
    client._post["/execute/internal"]._body = body
    assert await _post_internal_execute(client, job, {}) == (
        "failed",
        None,
        "execution returned malformed JSON",
    )


# ---- the fire request carries the job's reservation (issue #1090) ----


@pytest.mark.asyncio
@pytest.mark.parametrize("dry_run", [True, False])
@pytest.mark.parametrize("tied", [True, False])
async def test_post_internal_execute_sends_the_job_reservation_id(monkeypatch, tied, dry_run):
    monkeypatch.setattr(
        "app.services.apply_scheduler.settings.internal_api_token", "token", raising=False
    )
    job = _make_job()
    job.dry_run = dry_run
    reservation_id = uuid.uuid4()
    job.reservation_id = reservation_id if tied else None
    run_id = uuid.uuid4()
    client = FakeClient(
        post_responses={
            "/execute/internal": FakeResponse(201, {"id": str(run_id), "status": "SUCCESS"})
        }
    )
    await _post_internal_execute(client, job, {"vlan": 7})
    [(url, body, headers)] = client.posts
    assert url.endswith("/execute/internal")
    assert headers == {"X-Internal-Token": "token"}
    assert body == {
        "device_id": str(job.device_id),
        "action": "configure",
        "user_id": str(job.created_by),
        "method_kwargs": {"vlan": 7},
        "dry_run": dry_run,
        "reservation_id": str(reservation_id) if tied else None,
    }
