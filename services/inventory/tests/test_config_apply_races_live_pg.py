"""Postgres-live proof of the device-configuration write races (issues #1095, #1088).

SQLite serializes every writer on one shared connection, so neither race below can
happen there. Both run as REAL concurrent asyncio tasks over SEPARATE sessions against
a real Postgres, through the production functions:

  - #1095, rule CFG-VER-5: two creates for one device both read the same maximum
    version number. The unique index ix_device_config_versions_device_version makes
    the second insert wait for the first commit and then fail with a unique violation;
    `_commit_new_version` classifies it (asyncpg's SQLSTATE 23505), rolls back,
    recomputes, and stores the next number. Both writes succeed with distinct numbers.
  - #1088, rule CFG-STATE-4: the cancel's compare-and-swap and the scheduler's claim
    (`fire_job`) race for one pending job, in both orders. Exactly one wins: a 204
    cancel means nothing reached execution; a claim that won means the cancel answered
    409 and the job fired.

Env contract identical to the other live suites (HERD_TEST_PG_DSN,
HERD_TEST_PG_REQUIRED). Gate-ledger scoping (issue #819): the gate runs this against
its ALREADY-USED database, so every row this suite writes hangs off one device it
creates (template and device named with a random suffix), every job is scheduled an
hour ahead so the gate's live apply scheduler never finds it due (the test drives
`fire_job` directly), foreign config versions and foreign terminal jobs are
snapshotted before and after and must be unchanged, and the device and template are
deleted in a finally (versions and jobs cascade with the device).
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from app.models.device import Device, DeviceStatus
from app.models.device_config_apply_job import DeviceConfigApplyJob
from app.models.device_config_version import DeviceConfigVersion
from app.models.template import DeviceTemplate
from app.routers import device_configs
from app.routers.apply_jobs import cancel_apply_job
from app.services.apply_scheduler import fire_job
from fastapi import HTTPException
from herd_common.enums import TopologyType
from herd_common.pg_live_suite import pg_live_target
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

_PG = pg_live_target()
PG_DSN = _PG.dsn

# The unit-suite conftest sets DB_SCHEMA="", so the mapped tables carry no schema;
# the live database keeps them under "inventory".
_CONNECT_ARGS = {"server_settings": {"search_path": "inventory"}}

TERMINAL_JOB_STATUSES = ("success", "failed", "skipped", "cancelled")
# How long a blocked task is given to prove it is blocked.
BLOCK_SECONDS = 0.5
# How far ahead each job is scheduled, so the gate's live scheduler never claims it.
LEAD = timedelta(hours=1)
ADMIN = {"sub": "00000000-0000-0000-0000-00000000a001", "role": "admin", "username": "pg"}


def _make_engine():
    return create_async_engine(PG_DSN, connect_args=_CONNECT_ARGS)


pytestmark = pytest.mark.skipif(_PG.skip_reason is not None, reason=_PG.skip_reason or "")


@pytest.fixture(autouse=True)
def _fail_when_pg_live_cannot_run():
    if _PG.failure is not None:
        pytest.fail(_PG.failure)


@pytest.fixture
async def session_factory():
    engine = _make_engine()
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _foreign_snapshot(session_factory, device_id: uuid.UUID) -> dict:
    async with session_factory() as s:
        versions = await s.execute(
            select(DeviceConfigVersion.id, DeviceConfigVersion.version_number).where(
                DeviceConfigVersion.device_id != device_id
            )
        )
        jobs = await s.execute(
            select(DeviceConfigApplyJob.id, DeviceConfigApplyJob.status).where(
                DeviceConfigApplyJob.device_id != device_id,
                DeviceConfigApplyJob.status.in_(TERMINAL_JOB_STATUSES),
            )
        )
        return {
            "versions": {r.id: r.version_number for r in versions},
            "jobs": {r.id: r.status for r in jobs},
        }


@pytest.fixture
async def device_id(session_factory):
    """One device of this test's own; foreign rows snapshotted; own rows deleted."""
    suffix = uuid.uuid4().hex[:10]
    template = DeviceTemplate(name=f"pg-live-cfg-{suffix}", sections=[])
    async with session_factory() as s:
        s.add(template)
        await s.flush()
        device = Device(
            name=f"pg-live-cfg-{suffix}",
            template_id=template.id,
            topology_type=TopologyType.PHYSICAL,
            status=DeviceStatus.AVAILABLE,
            field_data={},
        )
        s.add(device)
        await s.commit()
    before = await _foreign_snapshot(session_factory, device.id)
    try:
        yield device.id
    finally:
        async with session_factory() as s:
            await s.execute(delete(Device).where(Device.id == device.id))
            await s.execute(delete(DeviceTemplate).where(DeviceTemplate.id == template.id))
            await s.commit()
        assert await _foreign_snapshot(session_factory, device.id) == before


def _new_version(device_id: uuid.UUID, tag: int) -> DeviceConfigVersion:
    return DeviceConfigVersion(
        device_id=device_id,
        connection_type="Management",
        config={"tag": tag},
        created_by=uuid.uuid4(),
    )


async def test_concurrent_creates_get_distinct_numbers(session_factory, device_id):
    both_read = asyncio.Barrier(2)
    real_next = device_configs._next_version_number
    reads: list[int] = []

    async def next_after_both_read(db, dev):
        number = await real_next(db, dev)
        reads.append(number)
        if len(reads) <= 2:
            # Both writers hold the same max+1 before either inserts: the race.
            await both_read.wait()
        return number

    async def create(tag: int) -> DeviceConfigVersion:
        async with session_factory() as db:
            version = _new_version(device_id, tag)
            await device_configs._commit_new_version(db, device_id, version)
            return version

    with patch.object(device_configs, "_next_version_number", new=next_after_both_read):
        a, b = await asyncio.wait_for(asyncio.gather(create(1), create(2)), timeout=20)

    assert reads[:2] == [1, 1]
    assert sorted([a.version_number, b.version_number]) == [1, 2]
    async with session_factory() as s:
        rows = (
            await s.execute(
                select(DeviceConfigVersion.version_number, DeviceConfigVersion.config).where(
                    DeviceConfigVersion.device_id == device_id
                )
            )
        ).all()
    assert sorted(r.version_number for r in rows) == [1, 2]
    assert sorted(r.config["tag"] for r in rows) == [1, 2]


async def _pending_job(session_factory, device_id) -> uuid.UUID:
    async with session_factory() as s:
        version = _new_version(device_id, 0)
        version.version_number = 1
        s.add(version)
        await s.flush()
        job = DeviceConfigApplyJob(
            device_id=device_id,
            version_id=version.id,
            scheduled_for=datetime.now(timezone.utc) + LEAD,
            status="pending",
            created_by=uuid.UUID(ADMIN["sub"]),
        )
        s.add(job)
        await s.commit()
        return job.id


def _gate_first_commit(db, reached: asyncio.Event, release: asyncio.Event) -> None:
    """Hold this session's FIRST commit until `release`; later commits pass through.

    The write before that commit holds the row lock, so the other writer blocks on
    it for as long as the commit is held.
    """
    real_commit = db.commit
    state = {"held": False}

    async def gated():
        if not state["held"]:
            state["held"] = True
            reached.set()
            await release.wait()
        await real_commit()

    db.commit = gated


async def _status(session_factory, job_id) -> str:
    async with session_factory() as s:
        return (
            await s.execute(
                select(DeviceConfigApplyJob.status).where(DeviceConfigApplyJob.id == job_id)
            )
        ).scalar_one()


@pytest.fixture
def execute():
    post = AsyncMock(return_value=("success", None, None))
    with (
        patch("app.services.apply_scheduler._post_internal_execute", new=post),
        patch(
            "app.services.apply_scheduler._creator_still_authorized",
            new=AsyncMock(return_value=True),
        ),
    ):
        yield post


async def test_cancel_holds_the_row_first_claim_fires_nothing(session_factory, device_id, execute):
    job_id = await _pending_job(session_factory, device_id)
    reached, release = asyncio.Event(), asyncio.Event()

    async def cancel():
        async with session_factory() as db:
            _gate_first_commit(db, reached, release)
            await cancel_apply_job(job_id, payload=ADMIN, db=db)

    async def claim():
        async with session_factory() as db:
            job = await db.get(DeviceConfigApplyJob, job_id)
            assert job.status == "pending"
            await fire_job(db, job, client=None)

    cancel_task = asyncio.create_task(cancel())
    await asyncio.wait_for(reached.wait(), timeout=10)
    claim_task = asyncio.create_task(claim())
    await asyncio.sleep(BLOCK_SECONDS)
    assert not claim_task.done(), "the claim did not wait for the cancel's row lock"
    release.set()
    await asyncio.wait_for(asyncio.gather(cancel_task, claim_task), timeout=20)

    execute.assert_not_awaited()
    assert await _status(session_factory, job_id) == "cancelled"


async def test_claim_holds_the_row_first_cancel_answers_409(session_factory, device_id, execute):
    job_id = await _pending_job(session_factory, device_id)
    reached, release = asyncio.Event(), asyncio.Event()

    async def claim():
        async with session_factory() as db:
            job = await db.get(DeviceConfigApplyJob, job_id)
            _gate_first_commit(db, reached, release)
            await fire_job(db, job, client=None)

    async def cancel():
        async with session_factory() as db:
            await cancel_apply_job(job_id, payload=ADMIN, db=db)

    claim_task = asyncio.create_task(claim())
    await asyncio.wait_for(reached.wait(), timeout=10)
    cancel_task = asyncio.create_task(cancel())
    await asyncio.sleep(BLOCK_SECONDS)
    assert not cancel_task.done(), "the cancel did not wait for the claim's row lock"
    release.set()
    results = await asyncio.wait_for(
        asyncio.gather(claim_task, cancel_task, return_exceptions=True), timeout=20
    )

    assert results[0] is None
    refusal = results[1]
    assert isinstance(refusal, HTTPException)
    assert refusal.status_code == 409
    # The claim's fire_job carries on to its terminal write once released, so the
    # cancel's re-read sees running or already success; never a 204.
    assert refusal.detail in (
        "Job is 'running', not cancellable",
        "Job is 'success', not cancellable",
    )
    execute.assert_awaited_once()
    assert await _status(session_factory, job_id) == "success"
