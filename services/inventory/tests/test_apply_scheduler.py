"""Unit tests for app.services.apply_scheduler.fire_job + _due_jobs.

The full polling loop is integration-shaped; these tests exercise the
deterministic per-job pipeline against an in-memory DB and a fake httpx
client so we can assert the state transitions without timing.
"""

import asyncio
import contextlib
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from app.database import Base
from app.models.device_config_apply_job import DeviceConfigApplyJob
from app.models.device_config_version import DeviceConfigVersion
from app.services import apply_scheduler
from app.services.apply_scheduler import (
    _due_jobs,
    _mark_failed_in_fresh_session,
    _post_internal_execute,
    _reservation_active,
    _resweep_stale_running,
    fire_job,
)
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
TestSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


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
    assert "unreachable" in error


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
    assert error.startswith("503")


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
