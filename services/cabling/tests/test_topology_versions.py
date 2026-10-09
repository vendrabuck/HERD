import uuid
from unittest.mock import AsyncMock, patch

import pytest
from app.database import Base, get_db
from app.dependencies import get_current_user_payload, require_admin
from app.main import app
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

ADMIN_ID = str(uuid.uuid4())
USER_ID = str(uuid.uuid4())
OTHER_USER_ID = str(uuid.uuid4())

ADMIN_PAYLOAD = {"sub": ADMIN_ID, "username": "admin", "role": "admin"}
USER_PAYLOAD = {"sub": USER_ID, "username": "viewer", "role": "user"}
OTHER_USER_PAYLOAD = {"sub": OTHER_USER_ID, "username": "other", "role": "user"}


def _override_admin():
    return ADMIN_PAYLOAD


def _override_user():
    return USER_PAYLOAD


def _override_other_user():
    return OTHER_USER_PAYLOAD


test_engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
TestSessionLocal = async_sessionmaker(test_engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
async def setup_db():
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


async def _override_get_db() -> AsyncSession:
    async with TestSessionLocal() as session:
        yield session


@pytest.fixture(autouse=True)
def _noop_reservation_guard():
    """Default: no active reservations, so restore proceeds."""
    with patch("app.routes.versions.find_blocking_reservations", return_value=[]) as m:
        yield m


@pytest.fixture
def _no_live_reservations_for_delete():
    """Issue #977: the topology DELETE asks reservations first; answer none."""
    with patch(
        "app.services.reservation_guard.find_blocking_reservations_strict",
        AsyncMock(return_value=[]),
    ) as m:
        yield m


@pytest.fixture
async def user_client():
    app.dependency_overrides[get_current_user_payload] = _override_user
    app.dependency_overrides[require_admin] = _override_user
    app.dependency_overrides[get_db] = _override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


async def _make_topology(client: AsyncClient, name: str = "My Lab") -> str:
    resp = await client.post("/topologies", json={"name": name})
    assert resp.status_code == 201
    return resp.json()["id"]


async def _save_canvas(client: AsyncClient, topology_id: str, canvas: dict, **extra) -> dict:
    body = {"canvas_data": canvas, **extra}
    resp = await client.put(f"/topologies/{topology_id}", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.mark.asyncio
async def test_first_save_creates_version_one(user_client):
    topology_id = await _make_topology(user_client)
    canvas = {"nodes": [{"id": "n1"}], "edges": []}
    await _save_canvas(user_client, topology_id, canvas, description="first")

    resp = await user_client.get(f"/topologies/{topology_id}/versions")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert data["items"][0]["version_number"] == 1
    assert data["items"][0]["description"] == "first"
    assert data["items"][0]["author_name"] == "viewer"
    assert data["items"][0]["created_by"] == USER_ID


@pytest.mark.asyncio
async def test_second_distinct_save_creates_version_two(user_client):
    topology_id = await _make_topology(user_client)
    await _save_canvas(user_client, topology_id, {"nodes": [{"id": "n1"}], "edges": []})
    await _save_canvas(
        user_client, topology_id, {"nodes": [{"id": "n1"}, {"id": "n2"}], "edges": []}
    )

    resp = await user_client.get(f"/topologies/{topology_id}/versions")
    data = resp.json()
    assert data["total"] == 2
    version_numbers = [v["version_number"] for v in data["items"]]
    assert version_numbers == [2, 1]


@pytest.mark.asyncio
async def test_identical_save_is_deduped(user_client):
    topology_id = await _make_topology(user_client)
    canvas = {"nodes": [{"id": "n1"}], "edges": []}
    await _save_canvas(user_client, topology_id, canvas)
    await _save_canvas(user_client, topology_id, canvas)

    resp = await user_client.get(f"/topologies/{topology_id}/versions")
    assert resp.json()["total"] == 1


@pytest.mark.asyncio
async def test_name_only_save_does_not_create_version(user_client):
    topology_id = await _make_topology(user_client)
    await _save_canvas(user_client, topology_id, {"nodes": [{"id": "n1"}], "edges": []})
    resp = await user_client.put(f"/topologies/{topology_id}", json={"name": "Renamed"})
    assert resp.status_code == 200

    versions = (await user_client.get(f"/topologies/{topology_id}/versions")).json()
    assert versions["total"] == 1


@pytest.mark.asyncio
async def test_list_omits_canvas_data_detail_includes_it(user_client):
    topology_id = await _make_topology(user_client)
    canvas = {"nodes": [{"id": "n1"}], "edges": []}
    await _save_canvas(user_client, topology_id, canvas)

    listing = (await user_client.get(f"/topologies/{topology_id}/versions")).json()
    assert "canvas_data" not in listing["items"][0]

    version_id = listing["items"][0]["id"]
    detail = (await user_client.get(f"/topologies/{topology_id}/versions/{version_id}")).json()
    assert detail["canvas_data"] == canvas


@pytest.mark.asyncio
async def test_diff_detects_add_remove_modify(user_client):
    topology_id = await _make_topology(user_client)
    canvas_a = {
        "nodes": [{"id": "n1", "position": {"x": 0, "y": 0}}, {"id": "n2"}],
        "edges": [{"id": "e1", "source": "n1", "target": "n2"}],
    }
    canvas_b = {
        "nodes": [{"id": "n1", "position": {"x": 50, "y": 0}}, {"id": "n3"}],
        "edges": [{"id": "e1", "source": "n1", "target": "n2"}],
    }
    await _save_canvas(user_client, topology_id, canvas_a)
    await _save_canvas(user_client, topology_id, canvas_b)

    versions = (await user_client.get(f"/topologies/{topology_id}/versions")).json()["items"]
    v_b = versions[0]["id"]
    v_a = versions[1]["id"]

    resp = await user_client.get(
        f"/topologies/{topology_id}/versions/diff", params={"a": v_a, "b": v_b}
    )
    assert resp.status_code == 200
    diff = resp.json()
    assert [n["id"] for n in diff["nodes_added"]] == ["n3"]
    assert [n["id"] for n in diff["nodes_removed"]] == ["n2"]
    assert [m["id"] for m in diff["nodes_modified"]] == ["n1"]
    assert diff["edges_added"] == []
    assert diff["edges_removed"] == []
    assert diff["edges_modified"] == []


@pytest.mark.asyncio
async def test_diff_identical_versions_empty(user_client):
    topology_id = await _make_topology(user_client)
    canvas = {"nodes": [{"id": "n1"}], "edges": []}
    await _save_canvas(user_client, topology_id, canvas)
    # Save something different then revert via save of same canvas - won't dedupe
    # because we check against current state. Force a second version by changing.
    await _save_canvas(user_client, topology_id, {"nodes": [{"id": "n2"}], "edges": []})
    await _save_canvas(user_client, topology_id, canvas)

    versions = (await user_client.get(f"/topologies/{topology_id}/versions")).json()["items"]
    # v1 and v3 both have the original canvas - diffing them should be empty.
    resp = await user_client.get(
        f"/topologies/{topology_id}/versions/diff",
        params={"a": versions[2]["id"], "b": versions[0]["id"]},
    )
    diff = resp.json()
    assert diff["nodes_added"] == []
    assert diff["nodes_removed"] == []
    assert diff["nodes_modified"] == []


@pytest.mark.asyncio
async def test_restore_applies_snapshot_and_creates_new_version(user_client):
    topology_id = await _make_topology(user_client)
    canvas_a = {"nodes": [{"id": "n1"}], "edges": []}
    canvas_b = {"nodes": [{"id": "n2"}], "edges": []}
    await _save_canvas(user_client, topology_id, canvas_a)
    await _save_canvas(user_client, topology_id, canvas_b)

    versions = (await user_client.get(f"/topologies/{topology_id}/versions")).json()["items"]
    v_one = versions[1]  # version_number 1, canvas_a

    resp = await user_client.post(
        f"/topologies/{topology_id}/versions/{v_one['id']}/restore", json={}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["canvas_data"] == canvas_a

    versions = (await user_client.get(f"/topologies/{topology_id}/versions")).json()["items"]
    newest = versions[0]
    assert newest["version_number"] == 3
    assert newest["restored_from_id"] == v_one["id"]
    assert newest["description"] == "Restored from v1"


@pytest.mark.asyncio
async def test_restore_blocked_by_active_reservation(user_client):
    topology_id = await _make_topology(user_client)
    await _save_canvas(user_client, topology_id, {"nodes": [{"id": "n1"}], "edges": []})
    version_id = (await user_client.get(f"/topologies/{topology_id}/versions")).json()["items"][0][
        "id"
    ]

    with patch(
        "app.routes.versions.find_blocking_reservations",
        return_value=[{"id": "r1", "status": "ACTIVE", "end_time": "2026-05-01T00:00:00Z"}],
    ):
        # Pass a bearer token to trigger the guard call path
        resp = await user_client.post(
            f"/topologies/{topology_id}/versions/{version_id}/restore",
            json={},
            headers={"Authorization": "Bearer faketoken"},
        )
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["reservations"][0]["id"] == "r1"


@pytest.mark.asyncio
async def test_non_creator_can_read_but_not_restore(user_client):
    topology_id = await _make_topology(user_client)
    await _save_canvas(user_client, topology_id, {"nodes": [{"id": "n1"}], "edges": []})
    version_id = (await user_client.get(f"/topologies/{topology_id}/versions")).json()["items"][0][
        "id"
    ]

    app.dependency_overrides.clear()
    app.dependency_overrides[get_current_user_payload] = _override_other_user
    app.dependency_overrides[get_db] = _override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        # Read OK
        list_resp = await ac.get(f"/topologies/{topology_id}/versions")
        assert list_resp.status_code == 200
        assert list_resp.json()["total"] == 1
        # Restore forbidden
        restore_resp = await ac.post(
            f"/topologies/{topology_id}/versions/{version_id}/restore", json={}
        )
        assert restore_resp.status_code == 403


@pytest.mark.asyncio
async def test_admin_can_restore(user_client):
    topology_id = await _make_topology(user_client)
    await _save_canvas(user_client, topology_id, {"nodes": [{"id": "n1"}], "edges": []})
    version_id = (await user_client.get(f"/topologies/{topology_id}/versions")).json()["items"][0][
        "id"
    ]

    app.dependency_overrides.clear()
    app.dependency_overrides[get_current_user_payload] = _override_admin
    app.dependency_overrides[require_admin] = _override_admin
    app.dependency_overrides[get_db] = _override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.post(f"/topologies/{topology_id}/versions/{version_id}/restore", json={})
    assert resp.status_code == 200


async def _count_versions(topology_id: str) -> int:
    from app.models.topology import TopologyVersion
    from sqlalchemy import func, select

    async with TestSessionLocal() as session:
        return (
            await session.execute(
                select(func.count())
                .select_from(TopologyVersion)
                .where(TopologyVersion.topology_id == uuid.UUID(topology_id))
            )
        ).scalar_one()


@pytest.mark.asyncio
async def test_delete_topology_cascades_versions(user_client, _no_live_reservations_for_delete):
    """The cascade is the database's (ondelete="CASCADE" on topology_versions),
    and SQLite enforces foreign keys only with PRAGMA foreign_keys=ON, so this
    test turns it on for its own duration and counts the version rows; the
    versions route's 404 alone comes from the parent lookup (issue #1139). The
    in-memory engine keeps one connection for the whole module, so the pragma
    is turned off again in the finally."""
    async with test_engine.connect() as conn:
        await conn.exec_driver_sql("PRAGMA foreign_keys=ON")
    try:
        topology_id = await _make_topology(user_client)
        await _save_canvas(user_client, topology_id, {"nodes": [{"id": "n1"}], "edges": []})
        await _save_canvas(user_client, topology_id, {"nodes": [{"id": "n2"}], "edges": []})
        assert await _count_versions(topology_id) == 2

        resp = await user_client.delete(f"/topologies/{topology_id}")
        assert resp.status_code == 204

        assert await _count_versions(topology_id) == 0
        resp = await user_client.get(f"/topologies/{topology_id}/versions")
        assert resp.status_code == 404
    finally:
        async with test_engine.connect() as conn:
            await conn.exec_driver_sql("PRAGMA foreign_keys=OFF")


@pytest.mark.asyncio
async def test_versions_list_pagination(user_client):
    topology_id = await _make_topology(user_client)
    for i in range(3):
        await _save_canvas(user_client, topology_id, {"nodes": [{"id": f"n{i}"}], "edges": []})

    resp = await user_client.get(
        f"/topologies/{topology_id}/versions", params={"skip": 0, "limit": 2}
    )
    data = resp.json()
    assert data["total"] == 3
    assert len(data["items"]) == 2
    assert [v["version_number"] for v in data["items"]] == [3, 2]


@pytest.mark.asyncio
async def test_restore_with_description_and_restore_name(user_client):
    topology_id = await _make_topology(user_client, name="Original")
    await _save_canvas(user_client, topology_id, {"nodes": [{"id": "n1"}], "edges": []})
    v_one_id = (await user_client.get(f"/topologies/{topology_id}/versions")).json()["items"][0][
        "id"
    ]
    # Rename topology, save again so names diverge
    await _save_canvas(
        user_client,
        topology_id,
        {"nodes": [{"id": "n2"}], "edges": []},
        name="Renamed",
    )

    resp = await user_client.post(
        f"/topologies/{topology_id}/versions/{v_one_id}/restore",
        json={"description": "Manual rollback", "restore_name": True},
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "Original"

    latest = (await user_client.get(f"/topologies/{topology_id}/versions")).json()["items"][0]
    assert latest["description"] == "Manual rollback"
    assert latest["name"] == "Original"


@pytest.mark.asyncio
async def test_version_on_missing_topology(user_client):
    fake = str(uuid.uuid4())
    resp = await user_client.get(f"/topologies/{fake}/versions")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_version_detail_wrong_topology(user_client):
    t1 = await _make_topology(user_client, "T1")
    t2 = await _make_topology(user_client, "T2")
    await _save_canvas(user_client, t1, {"nodes": [{"id": "n1"}], "edges": []})
    v_id = (await user_client.get(f"/topologies/{t1}/versions")).json()["items"][0]["id"]

    # Looking up t1's version under t2 returns 404
    resp = await user_client.get(f"/topologies/{t2}/versions/{v_id}")
    assert resp.status_code == 404


# --- Concurrent version_number allocation (regression for the max+1 race) ---


def _version_numbers(items: list[dict]) -> list[int]:
    return sorted(v["version_number"] for v in items)


async def _insert_competing_version(topology_id: str, version_number: int) -> None:
    """Insert a TopologyVersion at a specific number through an independent session,
    simulating a concurrent writer that committed first."""
    async with TestSessionLocal() as session:
        from app.models.topology import TopologyVersion

        session.add(
            TopologyVersion(
                topology_id=uuid.UUID(topology_id),
                version_number=version_number,
                canvas_data={"nodes": [{"id": "race"}], "edges": []},
                name="Race Winner",
                description="committed by a concurrent writer",
                created_by=uuid.UUID(USER_ID),
                author_name="racer",
            )
        )
        await session.commit()


def _lose_first_commit_to_a_racer(target: str, topology_id: str, state: dict):
    """Patch the route's commit_with_new_version so its FIRST commit loses a race.

    The real allocation reads max+1 first; only then, at the first commit, a
    concurrent writer commits that very number through an independent session and
    the database aborts our transaction with a unique-constraint IntegrityError
    (the pattern the fork retry tests use). A competitor inserted BEFORE the max
    read would never conflict at all (issue #1139). The rollback expires the
    topology's pending field changes, so the retry only keeps them through
    commit_with_new_version's reapply.
    """
    from app.services import version_service
    from sqlalchemy.exc import IntegrityError

    real_commit_with_new_version = version_service.commit_with_new_version

    async def racing_commit_with_new_version(db, topology, snapshot):
        real_commit = db.commit

        async def commit():
            state["commits"] += 1
            if state["commits"] == 1:
                state["first_number"] = snapshot.version_number
                await db.rollback()
                await _insert_competing_version(topology_id, snapshot.version_number)
                raise IntegrityError(
                    "INSERT", {}, Exception("uq_topology_versions_topology_version")
                )
            return await real_commit()

        with patch.object(db, "commit", side_effect=commit):
            return await real_commit_with_new_version(db, topology, snapshot)

    return patch(target, side_effect=racing_commit_with_new_version)


async def _stored_topology(topology_id: str):
    from app.models.topology import Topology

    async with TestSessionLocal() as session:
        return await session.get(Topology, uuid.UUID(topology_id))


@pytest.mark.asyncio
async def test_update_topology_retries_on_version_number_conflict(user_client):
    """A concurrent writer claims version_number=2 between our max-read and commit.

    The commit raises IntegrityError; the handler must roll back, re-apply the
    pending name, canvas_data, and modified_by (the rollback expired them),
    recompute max+1 (now 3), and retry rather than surfacing a 500.
    """
    topology_id = await _make_topology(user_client)
    # First save (by the creator) makes version 1.
    await _save_canvas(user_client, topology_id, {"nodes": [{"id": "n1"}], "edges": []})

    # The racing PUT comes from an admin, so modified_by changes too.
    app.dependency_overrides[get_current_user_payload] = _override_admin
    new_canvas = {"nodes": [{"id": "n2"}], "edges": []}
    state = {"commits": 0}
    with _lose_first_commit_to_a_racer(
        "app.routes.topologies.commit_with_new_version", topology_id, state
    ):
        resp = await user_client.put(
            f"/topologies/{topology_id}",
            json={"name": "Renamed Lab", "canvas_data": new_canvas},
        )

    assert resp.status_code == 200, resp.text
    # The first attempt read max=1 and lost number 2; the retry committed.
    assert state == {"commits": 2, "first_number": 2}

    items = (await user_client.get(f"/topologies/{topology_id}/versions")).json()["items"]
    numbers = _version_numbers(items)
    # 1 (first save), 2 (the racing writer), 3 (our retried insert).
    assert numbers == [1, 2, 3]
    ours = next(v for v in items if v["version_number"] == 3)
    assert ours["name"] == "Renamed Lab"

    # The stored row carries every pending change across the rollback.
    stored = await _stored_topology(topology_id)
    assert stored.name == "Renamed Lab"
    assert stored.canvas_data == new_canvas
    assert stored.modified_by == uuid.UUID(ADMIN_ID)


@pytest.mark.asyncio
async def test_restore_retries_on_version_number_conflict(user_client):
    """Same race on the restore path: a concurrent writer takes the next number
    after our max read, and restore must retry to the following number with the
    restored canvas, name, and restored_from marker intact."""
    topology_id = await _make_topology(user_client)
    canvas_a = {"nodes": [{"id": "n1"}], "edges": []}
    canvas_b = {"nodes": [{"id": "n2"}], "edges": []}
    await _save_canvas(user_client, topology_id, canvas_a)  # version 1, "My Lab"
    await _save_canvas(user_client, topology_id, canvas_b, name="Renamed Lab")  # version 2

    items = (await user_client.get(f"/topologies/{topology_id}/versions")).json()["items"]
    v_one_id = next(v["id"] for v in items if v["version_number"] == 1)

    app.dependency_overrides[get_current_user_payload] = _override_admin
    state = {"commits": 0}
    with _lose_first_commit_to_a_racer(
        "app.routes.versions.commit_with_new_version", topology_id, state
    ):
        resp = await user_client.post(
            f"/topologies/{topology_id}/versions/{v_one_id}/restore",
            json={"restore_name": True},
        )

    assert resp.status_code == 200, resp.text
    assert state == {"commits": 2, "first_number": 3}

    items = (await user_client.get(f"/topologies/{topology_id}/versions")).json()["items"]
    assert _version_numbers(items) == [1, 2, 3, 4]
    # The retried snapshot carries the restored_from marker and restored canvas.
    restored = [v for v in items if v["restored_from_id"] == v_one_id]
    assert len(restored) == 1
    assert restored[0]["version_number"] == 4
    detail = (
        await user_client.get(f"/topologies/{topology_id}/versions/{restored[0]['id']}")
    ).json()
    assert detail["canvas_data"] == canvas_a

    # The live topology row took the restore despite the rollback in between.
    stored = await _stored_topology(topology_id)
    assert stored.canvas_data == canvas_a
    assert stored.name == "My Lab"
    assert stored.modified_by == uuid.UUID(ADMIN_ID)


@pytest.mark.asyncio
async def test_commit_with_new_version_exhausts_retries_and_raises(user_client):
    """If contention never clears, the helper re-raises IntegrityError past the cap
    rather than looping forever or silently swallowing the conflict.

    Every commit attempt is forced to raise IntegrityError (a persistent concurrent
    writer), so the bounded loop should give up after _MAX_ALLOCATE_RETRIES and the
    exception should propagate. The number of commit attempts equals the cap.
    """
    import app.services.version_service as vs
    from app.models.topology import Topology, TopologyVersion
    from app.services.version_service import commit_with_new_version
    from sqlalchemy.exc import IntegrityError

    topology_id = await _make_topology(user_client)
    await _save_canvas(user_client, topology_id, {"nodes": [{"id": "n1"}], "edges": []})

    async with TestSessionLocal() as session:
        topology = await session.get(Topology, uuid.UUID(topology_id))
        topology.canvas_data = {"nodes": [{"id": "n2"}], "edges": []}
        snapshot = TopologyVersion(
            topology_id=topology.id,
            canvas_data=topology.canvas_data,
            name=topology.name,
            description="will collide forever",
            created_by=uuid.UUID(USER_ID),
            author_name="viewer",
        )

        attempts = {"n": 0}

        async def always_conflict():
            attempts["n"] += 1
            raise IntegrityError("INSERT", {}, Exception("uq conflict"))

        # rollback is a no-op here because no flush ever succeeded.
        async def noop_rollback():
            return None

        with (
            patch.object(session, "commit", side_effect=always_conflict),
            patch.object(session, "rollback", side_effect=noop_rollback),
        ):
            with pytest.raises(IntegrityError):
                await commit_with_new_version(session, topology, snapshot)

        assert attempts["n"] == vs._MAX_ALLOCATE_RETRIES
