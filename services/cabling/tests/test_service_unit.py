"""Direct unit tests for cabling service functions and route handlers.

Bypasses ASGITransport to ensure pytest-cov tracks coverage of handler bodies.
"""

import uuid

import pytest
from app.database import Base
from app.schemas.connection import ConnectionCreate
from app.services.connection_service import (
    create_connection,
    delete_connection,
    get_connection,
    list_connections,
)
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
TestSession = async_sessionmaker(engine, expire_on_commit=False)

USER_ID = uuid.uuid4()
DEVICE_A = uuid.uuid4()
DEVICE_B = uuid.uuid4()


class _FakeRequest:
    """Minimal stand-in for fastapi.Request when calling the route handler
    directly. The reservation-lock guard only reads the Authorization header;
    no header means no token, so the guard is skipped (these unit tests do
    name-only updates that never touch the live wiring anyway)."""

    def __init__(self, headers: dict | None = None):
        self.headers = headers or {}


@pytest.fixture(autouse=True)
async def setup_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


# --- connection_service ---


@pytest.mark.asyncio
async def test_create_connection_success():
    async with TestSession() as db:
        body = ConnectionCreate(
            device_a_id=DEVICE_A,
            port_a="eth0",
            device_b_id=DEVICE_B,
            port_b="eth0",
            connection_type="L1",
        )
        conn = await create_connection(db, body, str(USER_ID))
        assert conn.device_a_id == DEVICE_A
        assert conn.device_b_id == DEVICE_B
        assert conn.port_a == "eth0"


@pytest.mark.asyncio
async def test_create_connection_self_loop_rejected():
    from fastapi import HTTPException

    async with TestSession() as db:
        body = ConnectionCreate(
            device_a_id=DEVICE_A,
            port_a="eth0",
            device_b_id=DEVICE_A,
            port_b="eth0",
            connection_type="L1",
        )
        with pytest.raises(HTTPException) as exc_info:
            await create_connection(db, body, str(USER_ID))
        assert exc_info.value.status_code == 422


# --- device-group boundary enforcement ---


@pytest.fixture(autouse=True)
def _disable_group_enforcement(monkeypatch):
    """Default the boundary check off so create_connection tests stay hermetic
    (no real inventory call). The boundary tests re-enable it explicitly."""
    from app.config import settings

    monkeypatch.setattr(settings, "enforce_device_group_boundaries", False)


def _cross_device_body():
    return ConnectionCreate(
        device_a_id=DEVICE_A,
        port_a="eth0",
        device_b_id=DEVICE_B,
        port_b="eth1",
        connection_type="L1",
    )


@pytest.mark.asyncio
async def test_rejects_cross_group_connection(monkeypatch):
    from unittest.mock import AsyncMock, patch

    from app.config import settings
    from fastapi import HTTPException

    monkeypatch.setattr(settings, "enforce_device_group_boundaries", True)
    fetch = AsyncMock(side_effect=[{"G1"}, {"G2"}])  # disjoint
    with patch("app.services.connection_service.fetch_device_group_ids", fetch):
        async with TestSession() as db:
            with pytest.raises(HTTPException) as exc:
                await create_connection(db, _cross_device_body(), str(USER_ID), bearer_token="t")
    assert exc.value.status_code == 422
    assert "different device groups" in exc.value.detail


@pytest.mark.asyncio
async def test_allows_shared_group_connection(monkeypatch):
    from unittest.mock import AsyncMock, patch

    from app.config import settings

    monkeypatch.setattr(settings, "enforce_device_group_boundaries", True)
    fetch = AsyncMock(side_effect=[{"G1", "G2"}, {"G2"}])  # share G2
    with patch("app.services.connection_service.fetch_device_group_ids", fetch):
        async with TestSession() as db:
            conn = await create_connection(db, _cross_device_body(), str(USER_ID), bearer_token="t")
    assert conn.device_a_id == DEVICE_A


@pytest.mark.asyncio
async def test_allows_when_either_device_ungrouped(monkeypatch):
    from unittest.mock import AsyncMock, patch

    from app.config import settings

    monkeypatch.setattr(settings, "enforce_device_group_boundaries", True)
    fetch = AsyncMock(side_effect=[set(), {"G1"}])  # device_a in no group
    with patch("app.services.connection_service.fetch_device_group_ids", fetch):
        async with TestSession() as db:
            conn = await create_connection(db, _cross_device_body(), str(USER_ID), bearer_token="t")
    assert conn.id is not None


@pytest.mark.asyncio
async def test_allows_when_inventory_unavailable_fail_open(monkeypatch):
    from unittest.mock import AsyncMock, patch

    from app.config import settings

    monkeypatch.setattr(settings, "enforce_device_group_boundaries", True)
    fetch = AsyncMock(side_effect=[None, {"G1"}])  # membership could not be determined
    with patch("app.services.connection_service.fetch_device_group_ids", fetch):
        async with TestSession() as db:
            conn = await create_connection(db, _cross_device_body(), str(USER_ID), bearer_token="t")
    assert conn.id is not None


@pytest.mark.asyncio
async def test_disabled_enforcement_skips_membership_fetch(monkeypatch):
    from unittest.mock import AsyncMock, patch

    from app.config import settings

    monkeypatch.setattr(settings, "enforce_device_group_boundaries", False)
    fetch = AsyncMock(side_effect=AssertionError("should not be called when disabled"))
    with patch("app.services.connection_service.fetch_device_group_ids", fetch):
        async with TestSession() as db:
            conn = await create_connection(db, _cross_device_body(), str(USER_ID), bearer_token="t")
    assert conn.id is not None
    fetch.assert_not_called()


@pytest.mark.asyncio
async def test_list_connections_empty():
    async with TestSession() as db:
        conns, total = await list_connections(db)
        assert conns == []
        assert total == 0


@pytest.mark.asyncio
async def test_list_connections_with_device_filter():
    async with TestSession() as db:
        body = ConnectionCreate(
            device_a_id=DEVICE_A,
            port_a="eth0",
            device_b_id=DEVICE_B,
            port_b="eth1",
            connection_type="L1",
        )
        await create_connection(db, body, str(USER_ID))

        conns, total = await list_connections(db, device_id=DEVICE_A)
        assert total == 1

        other = uuid.uuid4()
        conns, total = await list_connections(db, device_id=other)
        assert total == 0


@pytest.mark.asyncio
async def test_list_connections_pagination():
    async with TestSession() as db:
        for i in range(3):
            body = ConnectionCreate(
                device_a_id=DEVICE_A,
                port_a=f"eth{i}",
                device_b_id=DEVICE_B,
                port_b=f"eth{i}",
                connection_type="L1",
            )
            await create_connection(db, body, str(USER_ID))

        conns, total = await list_connections(db, skip=0, limit=2)
        assert total == 3
        assert len(conns) == 2

        conns, total = await list_connections(db, skip=2, limit=2)
        assert len(conns) == 1


@pytest.mark.asyncio
async def test_get_connection_found():
    async with TestSession() as db:
        body = ConnectionCreate(
            device_a_id=DEVICE_A,
            port_a="eth0",
            device_b_id=DEVICE_B,
            port_b="eth0",
            connection_type="L1",
        )
        created = await create_connection(db, body, str(USER_ID))
        found = await get_connection(db, created.id)
        assert found is not None
        assert found.id == created.id


@pytest.mark.asyncio
async def test_get_connection_not_found():
    async with TestSession() as db:
        found = await get_connection(db, uuid.uuid4())
        assert found is None


@pytest.mark.asyncio
async def test_delete_connection_success():
    async with TestSession() as db:
        body = ConnectionCreate(
            device_a_id=DEVICE_A,
            port_a="eth0",
            device_b_id=DEVICE_B,
            port_b="eth0",
            connection_type="L1",
        )
        created = await create_connection(db, body, str(USER_ID))
        deleted = await delete_connection(db, created.id)
        assert deleted is True

        found = await get_connection(db, created.id)
        assert found is None


@pytest.mark.asyncio
async def test_delete_connection_not_found():
    async with TestSession() as db:
        deleted = await delete_connection(db, uuid.uuid4())
        assert deleted is False


# --- topology route handlers (direct invocation) ---


@pytest.mark.asyncio
async def test_topology_list_direct():
    """Test list_topologies route handler directly."""
    from app.routes.topologies import list_topologies

    async with TestSession() as db:
        result = await list_topologies(skip=0, limit=50, payload={"sub": str(USER_ID)}, db=db)
        assert result.total == 0
        assert result.items == []


@pytest.mark.asyncio
async def test_topology_create_direct():
    """Test create_topology route handler directly."""
    from app.routes.topologies import create_topology
    from app.schemas.topology import TopologyCreate

    async with TestSession() as db:
        body = TopologyCreate(name="Test Topology")
        result = await create_topology(
            body=body,
            payload={"sub": str(USER_ID), "username": "testuser"},
            db=db,
        )
        assert result.name == "Test Topology"
        assert result.created_by == USER_ID


@pytest.mark.asyncio
async def test_topology_get_direct():
    """Test get_topology route handler directly."""
    from app.routes.topologies import create_topology, get_topology
    from app.schemas.topology import TopologyCreate

    async with TestSession() as db:
        body = TopologyCreate(name="GetMe")
        created = await create_topology(
            body=body,
            payload={"sub": str(USER_ID), "username": "testuser"},
            db=db,
        )
        found = await get_topology(
            topology_id=created.id,
            payload={"sub": str(USER_ID)},
            db=db,
        )
        assert found.name == "GetMe"


@pytest.mark.asyncio
async def test_topology_get_not_found():
    from app.routes.topologies import get_topology
    from fastapi import HTTPException

    async with TestSession() as db:
        with pytest.raises(HTTPException) as exc_info:
            await get_topology(
                topology_id=uuid.uuid4(),
                payload={"sub": str(USER_ID)},
                db=db,
            )
        assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_topology_update_direct():
    """Test update_topology route handler directly."""
    from app.routes.topologies import create_topology, update_topology
    from app.schemas.topology import TopologyCreate, TopologyUpdate

    async with TestSession() as db:
        body = TopologyCreate(name="Original")
        created = await create_topology(
            body=body,
            payload={"sub": str(USER_ID), "username": "testuser"},
            db=db,
        )
        update_body = TopologyUpdate(name="Updated")
        updated = await update_topology(
            topology_id=created.id,
            body=update_body,
            request=_FakeRequest(),
            payload={"sub": str(USER_ID), "role": "admin"},
            db=db,
        )
        assert updated.name == "Updated"


@pytest.mark.asyncio
async def test_topology_update_not_found():
    from app.routes.topologies import update_topology
    from app.schemas.topology import TopologyUpdate
    from fastapi import HTTPException

    async with TestSession() as db:
        with pytest.raises(HTTPException) as exc_info:
            await update_topology(
                topology_id=uuid.uuid4(),
                body=TopologyUpdate(name="x"),
                request=_FakeRequest(),
                payload={"sub": str(USER_ID)},
                db=db,
            )
        assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_topology_update_not_authorized():
    from app.routes.topologies import create_topology, update_topology
    from app.schemas.topology import TopologyCreate, TopologyUpdate
    from fastapi import HTTPException

    async with TestSession() as db:
        body = TopologyCreate(name="Owned")
        created = await create_topology(
            body=body,
            payload={"sub": str(USER_ID), "username": "owner"},
            db=db,
        )
        other_user = str(uuid.uuid4())
        with pytest.raises(HTTPException) as exc_info:
            await update_topology(
                topology_id=created.id,
                body=TopologyUpdate(name="Nope"),
                request=_FakeRequest(),
                payload={"sub": other_user, "role": "user"},
                db=db,
            )
        assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_topology_delete_direct():
    from app.routes.topologies import create_topology, delete_topology
    from app.schemas.topology import TopologyCreate

    async with TestSession() as db:
        body = TopologyCreate(name="ToDelete")
        created = await create_topology(
            body=body,
            payload={"sub": str(USER_ID), "username": "testuser"},
            db=db,
        )
        await delete_topology(
            topology_id=created.id,
            payload={"sub": str(USER_ID), "role": "admin"},
            db=db,
        )


@pytest.mark.asyncio
async def test_topology_delete_not_found():
    from app.routes.topologies import delete_topology
    from fastapi import HTTPException

    async with TestSession() as db:
        with pytest.raises(HTTPException) as exc_info:
            await delete_topology(
                topology_id=uuid.uuid4(),
                payload={"sub": str(USER_ID)},
                db=db,
            )
        assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_topology_delete_not_authorized():
    from app.routes.topologies import create_topology, delete_topology
    from app.schemas.topology import TopologyCreate
    from fastapi import HTTPException

    async with TestSession() as db:
        body = TopologyCreate(name="Protected")
        created = await create_topology(
            body=body,
            payload={"sub": str(USER_ID), "username": "owner"},
            db=db,
        )
        other_user = str(uuid.uuid4())
        with pytest.raises(HTTPException) as exc_info:
            await delete_topology(
                topology_id=created.id,
                payload={"sub": other_user, "role": "user"},
                db=db,
            )
        assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_topology_list_with_data():
    from app.routes.topologies import create_topology, list_topologies
    from app.schemas.topology import TopologyCreate

    async with TestSession() as db:
        for i in range(3):
            await create_topology(
                body=TopologyCreate(name=f"Topo{i}"),
                payload={"sub": str(USER_ID), "username": "test"},
                db=db,
            )
        result = await list_topologies(skip=0, limit=2, payload={"sub": str(USER_ID)}, db=db)
        assert result.total == 3
        assert len(result.items) == 2


# --- connections route handlers (direct invocation) ---


@pytest.mark.asyncio
async def test_connections_route_create_direct():
    from app.routes.connections import create_connection_endpoint
    from app.schemas.connection import ConnectionCreate as CC

    async with TestSession() as db:
        body = CC(
            device_a_id=DEVICE_A,
            port_a="eth0",
            device_b_id=DEVICE_B,
            port_b="eth0",
            connection_type="L1",
        )
        result = await create_connection_endpoint(
            body=body,
            db=db,
            payload={"sub": str(USER_ID), "role": "admin"},
            authorization=None,
        )
        assert result.device_a_id == DEVICE_A


@pytest.mark.asyncio
async def test_connections_route_list_direct():
    from app.routes.connections import list_connections_endpoint

    async with TestSession() as db:
        result = await list_connections_endpoint(
            device_id=None, skip=0, limit=50, db=db, _={"sub": str(USER_ID)}
        )
        assert result.total == 0


@pytest.mark.asyncio
async def test_connections_route_delete_not_found():
    from app.routes.connections import delete_connection_endpoint
    from fastapi import HTTPException

    async with TestSession() as db:
        with pytest.raises(HTTPException) as exc_info:
            await delete_connection_endpoint(
                connection_id=uuid.uuid4(),
                db=db,
                payload={"sub": str(USER_ID), "role": "admin"},
            )
        assert exc_info.value.status_code == 404
