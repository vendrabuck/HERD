"""Unit tests for port-cable validation logic.

The frontend ConnectionModal calls `GET /connections?device_id=X` and determines
which ports on device X have physical L1 cables. A port is "cabled" iff it
appears as port_a on a connection where device_a_id = X, or as port_b on a
connection where device_b_id = X.

These tests exercise the backend data that drives that UI decision.
"""

import uuid

import pytest
from app.database import Base
from app.schemas.connection import ConnectionCreate
from app.services.connection_service import create_connection, list_connections
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
TestSession = async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
async def setup_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


def _cabled_ports_for(device_id: uuid.UUID, connections) -> set[str]:
    """Replicate the frontend rule for deciding which ports are cabled."""
    cabled = set()
    for c in connections:
        if c.device_a_id == device_id:
            cabled.add(c.port_a)
        if c.device_b_id == device_id:
            cabled.add(c.port_b)
    return cabled


@pytest.mark.asyncio
async def test_device_with_no_connections_has_no_cabled_ports():
    device_id = uuid.uuid4()
    async with TestSession() as db:
        conns, total = await list_connections(db, device_id=device_id)
        assert conns == []
        assert total == 0
        assert _cabled_ports_for(device_id, conns) == set()


@pytest.mark.asyncio
async def test_port_on_device_a_side_is_identified_as_cabled():
    dut = uuid.uuid4()
    switch = uuid.uuid4()
    async with TestSession() as db:
        await create_connection(
            db,
            ConnectionCreate(
                device_a_id=dut,
                port_a="eth1",
                device_b_id=switch,
                port_b="0/0/1",
                connection_type="L1",
            ),
            created_by="admin",
        )
        conns, _ = await list_connections(db, device_id=dut)
        assert _cabled_ports_for(dut, conns) == {"eth1"}
        assert "eth2" not in _cabled_ports_for(dut, conns)


@pytest.mark.asyncio
async def test_port_on_device_b_side_is_identified_as_cabled():
    dut = uuid.uuid4()
    switch = uuid.uuid4()
    async with TestSession() as db:
        # Connection stored with DUT on the B side.
        await create_connection(
            db,
            ConnectionCreate(
                device_a_id=switch,
                port_a="0/0/1",
                device_b_id=dut,
                port_b="eth1",
                connection_type="L1",
            ),
            created_by="admin",
        )
        conns, _ = await list_connections(db, device_id=dut)
        assert _cabled_ports_for(dut, conns) == {"eth1"}


@pytest.mark.asyncio
async def test_multiple_connections_aggregate_cabled_ports():
    dut = uuid.uuid4()
    switch_a = uuid.uuid4()
    switch_b = uuid.uuid4()
    async with TestSession() as db:
        for port, peer, peer_port in [
            ("eth1", switch_a, "0/0/1"),
            ("eth2", switch_a, "0/0/2"),
            ("eth3", switch_b, "0/0/1"),
        ]:
            await create_connection(
                db,
                ConnectionCreate(
                    device_a_id=dut,
                    port_a=port,
                    device_b_id=peer,
                    port_b=peer_port,
                    connection_type="L1",
                ),
                created_by="admin",
            )
        conns, total = await list_connections(db, device_id=dut)
        assert total == 3
        assert _cabled_ports_for(dut, conns) == {"eth1", "eth2", "eth3"}


@pytest.mark.asyncio
async def test_list_connections_filters_by_device_id():
    """Connections on other devices must not affect the cabled-port set."""
    dut = uuid.uuid4()
    other = uuid.uuid4()
    switch = uuid.uuid4()
    async with TestSession() as db:
        await create_connection(
            db,
            ConnectionCreate(
                device_a_id=dut,
                port_a="eth1",
                device_b_id=switch,
                port_b="0/0/1",
                connection_type="L1",
            ),
            created_by="admin",
        )
        await create_connection(
            db,
            ConnectionCreate(
                device_a_id=other,
                port_a="eth9",
                device_b_id=switch,
                port_b="0/0/2",
                connection_type="L1",
            ),
            created_by="admin",
        )
        conns, total = await list_connections(db, device_id=dut)
        assert total == 1
        assert _cabled_ports_for(dut, conns) == {"eth1"}
        # 'eth9' belongs to the other device, must not leak in.
        assert "eth9" not in _cabled_ports_for(dut, conns)


@pytest.mark.asyncio
async def test_uncabled_port_detection():
    """A port not in the returned connections is 'uncabled' from the UI's POV."""
    dut = uuid.uuid4()
    switch = uuid.uuid4()
    all_ports = {f"eth{i}" for i in range(1, 9)}
    async with TestSession() as db:
        await create_connection(
            db,
            ConnectionCreate(
                device_a_id=dut,
                port_a="eth1",
                device_b_id=switch,
                port_b="0/0/1",
                connection_type="L1",
            ),
            created_by="admin",
        )
        conns, _ = await list_connections(db, device_id=dut)
        cabled = _cabled_ports_for(dut, conns)
        uncabled = all_ports - cabled
        assert cabled == {"eth1"}
        assert uncabled == {"eth2", "eth3", "eth4", "eth5", "eth6", "eth7", "eth8"}
