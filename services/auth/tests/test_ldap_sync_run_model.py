"""Unit test for the LdapSyncRun model (ADR 0011 phase 3 foundations).

Pins server defaults on SQLite (create_all path, no Alembic): the counters
land at 0, detail lands at {}, started_at is populated, and the caller-set
trigger/status persist round-trip. The reconciler that populates non-default
runs is a separate component and is not exercised here.
"""

import uuid

import pytest
from app.database import Base
from app.models.ldap_sync_run import LdapSyncRun
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"

engine = create_async_engine(TEST_DATABASE_URL, echo=False)
TestSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
async def setup_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.mark.asyncio
async def test_insert_run_applies_server_defaults():
    run_id = uuid.uuid4()
    async with TestSessionLocal() as session:
        run = LdapSyncRun(id=run_id, trigger="manual", status="running")
        session.add(run)
        await session.commit()

        assert run.id == run_id
        assert run.trigger == "manual"
        assert run.status == "running"
        assert run.started_at is not None
        assert run.finished_at is None
        assert run.users_provisioned == 0
        assert run.members_added == 0
        assert run.members_removed == 0
        assert run.members_skipped == 0
        assert run.detail == {}
        assert run.error is None
