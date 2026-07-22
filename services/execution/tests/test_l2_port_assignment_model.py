"""Schema pins for L2PortAssignment (ADR 0009 Decision 2, issue #369/#416).

Schema-only in this phase: no service code reads or writes this table yet
(that is the layered L2 reconcile, ADR 0009 phase 4). These tests pin the
metadata contract migration 0017 must reproduce in Postgres: the partial-unique
index on (switch_device_id, port, vlan_assignment_id) restricted to ACTIVE, and
the FAILED-partial index on created_at, mirroring
test_l1_assignment_service.py's test_failed_created_at_index_covers_the_retry_sweep_query
and the l1_connection_assignments/vlan_assignments partial-index precedent.
"""

import uuid

import pytest
from app.database import Base
from app.models.l2_port_assignment import L2PortAssignment
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

test_engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
TestSessionLocal = async_sessionmaker(test_engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
async def setup_db():
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture
async def db():
    async with TestSessionLocal() as session:
        yield session


def test_active_unique_index_covers_switch_port_vlan():
    indexes = {ix.name: ix for ix in L2PortAssignment.__table__.indexes}
    index = indexes["uq_l2_active_per_switch_port_vlan"]

    assert index.unique is True
    assert [c.name for c in index.columns] == ["switch_device_id", "port", "vlan_assignment_id"]
    assert str(index.dialect_options["sqlite"]["where"]) == "status = 'ACTIVE'"
    assert str(index.dialect_options["postgresql"]["where"]) == "status = 'ACTIVE'"


def test_failed_created_at_index_covers_a_future_retry_sweep_query():
    indexes = {ix.name: ix for ix in L2PortAssignment.__table__.indexes}
    index = indexes["ix_l2_port_assignments_failed_created_at"]

    assert [c.name for c in index.columns] == ["created_at"]
    assert str(index.dialect_options["sqlite"]["where"]) == "status = 'FAILED'"
    assert str(index.dialect_options["postgresql"]["where"]) == "status = 'FAILED'"


@pytest.mark.asyncio
async def test_row_defaults(db):
    row = L2PortAssignment(
        reservation_id=uuid.uuid4(),
        vlan_assignment_id=uuid.uuid4(),
        switch_device_id=uuid.uuid4(),
        port="0/0/1",
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)

    assert row.intended == "ACTIVE"
    assert row.status == "ACTIVE"
    assert row.attempts == 0
    assert row.last_error is None
    assert row.released_at is None


@pytest.mark.asyncio
async def test_partial_unique_index_blocks_duplicate_active_insert(db):
    """The DB is the final arbiter: two ACTIVE rows for one (switch, port, vlan)
    trip the partial-unique index, mirroring the L1/VLAN precedent."""
    switch = uuid.uuid4()
    vlan_assignment_id = uuid.uuid4()
    db.add(
        L2PortAssignment(
            reservation_id=uuid.uuid4(),
            vlan_assignment_id=vlan_assignment_id,
            switch_device_id=switch,
            port="0/0/1",
            status="ACTIVE",
        )
    )
    await db.commit()

    db.add(
        L2PortAssignment(
            reservation_id=uuid.uuid4(),
            vlan_assignment_id=vlan_assignment_id,
            switch_device_id=switch,
            port="0/0/1",
            status="ACTIVE",
        )
    )
    with pytest.raises(IntegrityError):
        await db.commit()
    await db.rollback()


@pytest.mark.asyncio
async def test_failed_row_does_not_block_new_active_claim(db):
    """The unique predicate is ACTIVE-only, so a FAILED row never blocks a claim."""
    switch = uuid.uuid4()
    vlan_assignment_id = uuid.uuid4()
    db.add(
        L2PortAssignment(
            reservation_id=uuid.uuid4(),
            vlan_assignment_id=vlan_assignment_id,
            switch_device_id=switch,
            port="0/0/1",
            status="FAILED",
        )
    )
    await db.commit()

    db.add(
        L2PortAssignment(
            reservation_id=uuid.uuid4(),
            vlan_assignment_id=vlan_assignment_id,
            switch_device_id=switch,
            port="0/0/1",
            status="ACTIVE",
        )
    )
    await db.commit()

    actives = (
        (
            await db.execute(
                select(L2PortAssignment).where(
                    L2PortAssignment.switch_device_id == switch,
                    L2PortAssignment.status == "ACTIVE",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(actives) == 1
