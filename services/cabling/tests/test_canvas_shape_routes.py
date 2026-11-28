"""Functional tests: the canvas shape check through the real routes.

Write side: every route that stores a canvas from a request body refuses one that
does not have the shape with the one 422 item, and stores nothing. Read side: a
stored canvas written before the check (seeded straight into the database here)
leaves a collection read answering for every other row, and every route that reads
it answers a fixed 409 or, for the fork read, a null canvas; its owner can still
replace it or delete it.

The ASGI transport is built with `raise_app_exceptions=False`, so a status the app
answers is what these tests see.
"""

import copy
import csv
import io
import json
import math
import threading
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from app.config import settings
from app.database import Base, get_db
from app.dependencies import get_current_user_payload
from app.main import app
from app.models.connection import Connection
from app.models.fork import (
    ForkConnection,
    ForkL3Route,
    ForkStatus_ACTIVE,
    ForkVersion,
    ReservationFork,
)
from app.models.template import TopologyTemplate
from app.models.topology import Topology, TopologyVersion
from app.services import canvas_shape
from app.services.canvas_shape import INLINE_CHECK_VALUES, STORED_CANVAS_UNREADABLE_DETAIL
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

INTERNAL_TOKEN = "test-internal-token-canvas-shape"

USER_A = str(uuid.uuid4())
USER_B = str(uuid.uuid4())
ADMIN = str(uuid.uuid4())
PAYLOADS = {
    USER_A: {"sub": USER_A, "username": "alice", "role": "user"},
    USER_B: {"sub": USER_B, "username": "bob", "role": "user"},
    ADMIN: {"sub": ADMIN, "username": "admin", "role": "admin"},
}

DEV_A = str(uuid.uuid4())
DEV_B = str(uuid.uuid4())

test_engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
TestSessionLocal = async_sessionmaker(test_engine, expire_on_commit=False)

UNREADABLE_409 = {"detail": STORED_CANVAS_UNREADABLE_DETAIL}


def _nested(levels: int) -> dict:
    value: dict = {}
    for _ in range(levels - 1):
        value = {"k": value}
    return value


# Stored canvases that fail the check, one of each kind of problem ("deep" is nested
# far past the bound, which is far below any reader's nesting limit).
BAD_STORED = {
    "node_not_an_object": {"nodes": [1], "edges": []},
    "nodes_a_string": {"nodes": "abc", "edges": []},
    "node_data_a_string": {"nodes": [{"id": "n1", "data": "x"}], "edges": []},
    "device_id_a_number": {"nodes": [{"id": "n1", "data": {"device": {"id": 5}}}]},
    "edge_source_a_list": {"nodes": [], "edges": [{"id": "e1", "source": ["a"], "target": "b"}]},
    "edge_layer_a_number": {
        "edges": [{"id": "e1", "source": "a", "target": "b", "data": {"layer": 5}}]
    },
    "deep": {"nodes": [{"id": "n1", "data": _nested(300)}], "edges": []},
    "nan": {"nodes": [{"id": "n1", "position": {"x": math.nan, "y": 0}}], "edges": []},
    "lone_surrogate": {"nodes": [{"id": "n1", "data": {"label": "a\ud800"}}], "edges": []},
}


def _good_canvas(name: str = "sw-a") -> dict:
    return {
        "nodes": [
            {"id": "n1", "type": "deviceNode", "data": {"device": {"id": DEV_A, "name": name}}},
            {"id": "n2", "type": "deviceNode", "data": {"device": {"id": DEV_B, "name": "sw-b"}}},
        ],
        "edges": [{"id": "e1", "source": "n1", "target": "n2", "data": {"layer": "L1"}}],
    }


@pytest.fixture(autouse=True)
async def setup_db():
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture(autouse=True)
def _internal_token(monkeypatch):
    monkeypatch.setattr(settings, "internal_api_token", INTERNAL_TOKEN)


@pytest.fixture(autouse=True)
def _no_reservations():
    """No reservation holds any topology: the edit lock and the delete guard both
    answer "nothing blocks" without a reservations service."""
    with (
        patch("app.routes.topologies.find_blocking_reservations", new=AsyncMock(return_value=[])),
        patch("app.routes.versions.find_blocking_reservations", new=AsyncMock(return_value=[])),
        patch("app.routes.topologies.assert_topology_deletable", new=AsyncMock(return_value=None)),
    ):
        yield


async def _override_get_db() -> AsyncSession:
    async with TestSessionLocal() as session:
        yield session


class _Acting:
    user = USER_A


@pytest.fixture
async def client():
    app.dependency_overrides[get_current_user_payload] = lambda: PAYLOADS[_Acting.user]
    app.dependency_overrides[get_db] = _override_get_db
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()
    _Acting.user = USER_A


def _as(user: str) -> None:
    _Acting.user = user


def _internal() -> dict:
    return {"X-Internal-Token": INTERNAL_TOKEN}


async def _seed_topology(name: str, owner: str, canvas) -> uuid.UUID:
    """A topology row and its version 1, written straight to the database."""
    topology_id = uuid.uuid4()
    async with TestSessionLocal() as db:
        db.add(
            Topology(
                id=topology_id,
                name=name,
                created_by=uuid.UUID(owner),
                owner_name=PAYLOADS[owner]["username"],
                canvas_data=canvas,
            )
        )
        await db.flush()
        db.add(
            TopologyVersion(
                topology_id=topology_id,
                version_number=1,
                canvas_data=canvas,
                name=name,
                created_by=uuid.UUID(owner),
                author_name=PAYLOADS[owner]["username"],
            )
        )
        await db.commit()
    return topology_id


async def _version_ids(topology_id: uuid.UUID) -> list[uuid.UUID]:
    async with TestSessionLocal() as db:
        rows = (
            await db.execute(
                select(TopologyVersion.id)
                .where(TopologyVersion.topology_id == topology_id)
                .order_by(TopologyVersion.version_number)
            )
        ).all()
    return [r for (r,) in rows]


async def _stored_canvas(model, row_id: uuid.UUID):
    async with TestSessionLocal() as db:
        return (await db.get(model, row_id)).canvas_data


async def _seed_fork(draft, saved) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """An ACTIVE fork with draft `draft`, one saved version holding `saved`, and one
    wiring row. Returns (reservation_id, fork_id, version_id)."""
    reservation_id, fork_id, version_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with TestSessionLocal() as db:
        db.add(
            ReservationFork(
                id=fork_id,
                reservation_id=reservation_id,
                canvas_data=draft,
                status=ForkStatus_ACTIVE,
            )
        )
        await db.flush()
        db.add(ForkVersion(id=version_id, fork_id=fork_id, version_number=1, canvas_data=saved))
        db.add(
            ForkConnection(
                fork_id=fork_id,
                device_a_id=uuid.UUID(DEV_A),
                port_a="eth0",
                device_b_id=uuid.UUID(DEV_B),
                port_b="eth0",
                layer="L1",
                edge_key="e1",
                created_by="seed",
            )
        )
        await db.commit()
    return reservation_id, fork_id, version_id


# Write side ------------------------------------------------------------------------------

# Bodies the check refuses, each sent as raw JSON (`NaN` is a token Python's JSON
# reader accepts), with the one 422 message each answers.
BAD_BODIES = {
    "node_not_an_object": ('{"nodes": [1], "edges": []}', "canvas_data.nodes.0: must be an object"),
    "nodes_a_string": ('{"nodes": "abc"}', "canvas_data.nodes: must be a list"),
    "node_data_a_string": (
        '{"nodes": [{"id": "n1", "data": "x"}]}',
        "canvas_data.nodes.0.data: must be an object",
    ),
    "edge_target_an_object": (
        '{"edges": [{"id": "e1", "source": "n1", "target": {}}]}',
        "canvas_data.edges.0.target: must be a string",
    ),
    "deep": (
        json.dumps({"nodes": [{"id": "n1", "data": _nested(70)}]}),
        "canvas_data.nodes.0: is nested deeper than 64 levels",
    ),
    "past_the_recursion_limit": (
        '{"nodes": [{"id": "n1", "data": {"k": ' + "[" * 5000 + "]" * 5000 + "}}]}",
        "canvas_data.nodes.0: is nested deeper than 64 levels",
    ),
    "nan": (
        '{"nodes": [{"id": "n1", "position": {"x": NaN, "y": 0}}]}',
        "canvas_data.nodes.0: holds a number that is not finite",
    ),
    "infinity_literal": (
        '{"viewport": {"zoom": 1e400}}',
        "canvas_data: holds a number that is not finite",
    ),
    "edge_id_past_its_column": (
        json.dumps({"edges": [{"id": "e" * 256, "source": "n1", "target": "n2"}]}),
        "canvas_data.edges.0.id: is longer than 255 characters",
    ),
    "edge_id_with_a_nul": (
        '{"edges": [{"id": "e\\u0000x", "source": "n1", "target": "n2"}]}',
        "canvas_data.edges.0.id: holds a NUL character, which no database column takes",
    ),
}


def _expected_422(message: str) -> dict:
    return {"detail": [{"type": "canvas_shape", "loc": ["body", "canvas_data"], "msg": message}]}


async def _put_raw(client, url: str, canvas_json: str, extra: str = "", headers=None):
    body = '{"canvas_data": ' + canvas_json + extra + "}"
    return await client.put(
        url, content=body, headers={"content-type": "application/json", **(headers or {})}
    )


async def _post_raw(client, url: str, canvas_json: str, extra: str = "", headers=None):
    body = '{"canvas_data": ' + canvas_json + extra + "}"
    return await client.post(
        url, content=body, headers={"content-type": "application/json", **(headers or {})}
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", list(BAD_BODIES))
async def test_a_topology_put_refuses_a_bad_canvas_and_stores_nothing(client, kind):
    canvas_json, message = BAD_BODIES[kind]
    topology_id = await _seed_topology("lab", USER_A, _good_canvas())
    resp = await _put_raw(client, f"/topologies/{topology_id}", canvas_json)
    assert resp.status_code == 422, resp.text
    assert resp.json() == _expected_422(message)
    assert await _stored_canvas(Topology, topology_id) == _good_canvas()
    assert len(await _version_ids(topology_id)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", list(BAD_BODIES))
async def test_a_template_create_and_update_refuse_a_bad_canvas(client, kind):
    canvas_json, message = BAD_BODIES[kind]
    resp = await _post_raw(client, "/templates", canvas_json, ', "name": "tmpl"')
    assert resp.status_code == 422, resp.text
    assert resp.json() == _expected_422(message)
    async with TestSessionLocal() as db:
        assert (await db.execute(select(TopologyTemplate))).scalars().all() == []

    created = await client.post("/templates", json={"name": "tmpl", "canvas_data": _good_canvas()})
    assert created.status_code == 201, created.text
    template_id = uuid.UUID(created.json()["id"])
    resp = await _put_raw(client, f"/templates/{template_id}", canvas_json)
    assert resp.status_code == 422, resp.text
    assert resp.json() == _expected_422(message)
    assert await _stored_canvas(TopologyTemplate, template_id) == _good_canvas()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", list(BAD_BODIES))
async def test_a_fork_canvas_put_and_save_refuse_a_bad_canvas(client, kind):
    canvas_json, message = BAD_BODIES[kind]
    reservation_id, fork_id, _ = await _seed_fork(_good_canvas(), _good_canvas())
    resp = await _put_raw(
        client, f"/internal/forks/{reservation_id}/canvas", canvas_json, headers=_internal()
    )
    assert resp.status_code == 422, resp.text
    assert resp.json() == _expected_422(message)
    members = json.dumps([DEV_A, DEV_B])
    resp = await _post_raw(
        client,
        f"/internal/forks/{reservation_id}/save",
        canvas_json,
        f', "member_device_ids": {members}',
        headers=_internal(),
    )
    assert resp.status_code == 422, resp.text
    assert resp.json() == _expected_422(message)
    assert await _stored_canvas(ReservationFork, fork_id) == _good_canvas()
    async with TestSessionLocal() as db:
        versions = (
            (await db.execute(select(ForkVersion).where(ForkVersion.fork_id == fork_id)))
            .scalars()
            .all()
        )
    assert len(versions) == 1


@pytest.mark.asyncio
async def test_a_good_canvas_is_still_stored_by_every_body_route(client):
    """The check refuses nothing the editor sends: the same canvas through each
    route stores as sent (stripped)."""
    topology_id = await _seed_topology("lab", USER_A, None)
    resp = await client.put(f"/topologies/{topology_id}", json={"canvas_data": _good_canvas()})
    assert resp.status_code == 200, resp.text
    assert resp.json()["canvas_data"] == _good_canvas()
    resp = await client.post("/templates", json={"name": "t", "canvas_data": _good_canvas()})
    assert resp.status_code == 201, resp.text
    reservation_id, _, _ = await _seed_fork(None, None)
    resp = await client.put(
        f"/internal/forks/{reservation_id}/canvas",
        json={"canvas_data": _good_canvas()},
        headers=_internal(),
    )
    assert resp.status_code == 200, resp.text


# Import: the row is refused by the same check -------------------------------------------


async def _import(client, raw: bytes):
    return await client.post(
        "/topologies/import",
        params={"format": "json"},
        files={"file": ("t.json", io.BytesIO(raw), "application/json")},
    )


@pytest.fixture
def _import_mocks():
    with (
        patch(
            "app.services.bulk_service.resolve_caller_visibility",
            new=AsyncMock(return_value=None),
        ),
        patch("app.services.bulk_service.resolve_device_names", new=AsyncMock(return_value={})),
    ):
        yield


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("canvas_json", "reason"),
    [
        (
            json.dumps({"nodes": [{"id": "n1", "data": _nested(70)}]}),
            "canvas.nodes.0: is nested deeper than 64 levels",
        ),
        (
            '{"nodes": [], "edges": [{"id": "e1", "data": {"layer": 2}}]}',
            "canvas.edges.0.data.layer: must be a string",
        ),
        (
            '{"nodes": [{"id": "n1", "position": {"x": Infinity}}]}',
            "canvas.nodes.0: holds a number that is not finite",
        ),
    ],
    ids=["deep", "edge_layer_a_number", "infinity"],
)
async def test_an_import_row_with_a_bad_canvas_is_rejected_and_the_rest_imports(
    client, _import_mocks, canvas_json, reason
):
    raw = ('[{"name": "bad", "canvas": ' + canvas_json + '}, {"name": "good"}]').encode()
    resp = await _import(client, raw)
    assert resp.status_code == 200, resp.text
    assert [(r["action"], r["identity"], r["reason"]) for r in resp.json()["rows"]] == [
        ("reject", "bad", reason),
        ("create", "good", None),
    ]
    async with TestSessionLocal() as db:
        names = (await db.execute(select(Topology.name))).scalars().all()
    assert names == ["good"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "device_id", [5, True, ["x"], {"id": "x"}], ids=["int", "true", "list", "object"]
)
async def test_a_resolved_device_id_that_is_not_a_string_refuses_the_import(
    client, caplog, device_id
):
    """Name resolution writes inventory's answer into `data.device.id`, which the
    shape check requires to be a string: an answer holding anything else is the
    503 of a resolution that could not be used, and nothing is stored."""
    raw = json.dumps(
        [
            {
                "name": "lab",
                "canvas": {"nodes": [{"id": "n1", "data": {"device": {"name": "sw"}}}]},
            },
            {"name": "other"},
        ]
    ).encode()
    with (
        patch(
            "app.services.bulk_service.resolve_caller_visibility",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "app.services.bulk_service.resolve_device_names",
            new=AsyncMock(return_value={"sw-ok": str(uuid.uuid4()), "sw": device_id}),
        ),
    ):
        resp = await _import(client, raw)
    assert resp.status_code == 503
    assert resp.json() == {
        "detail": "Could not resolve device names; the import was not processed. Retry the request."
    }
    assert [r.getMessage() for r in caplog.records if r.name == "app.services.bulk_service"] == [
        "import_device_name_resolve_unusable reason=device_id_not_a_string"
    ]
    async with TestSessionLocal() as db:
        assert (await db.execute(select(Topology.name))).scalars().all() == []


# Read side: a collection walk ----------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("fmt", ["json", "csv"])
@pytest.mark.parametrize("kind", list(BAD_STORED))
async def test_the_export_leaves_out_a_bad_stored_canvas_and_exports_the_rest(client, fmt, kind):
    """User A's row, stored without the shape, is in the table; user B's export
    answers 200 with B's topologies, leaves A's out, and says how many it left out."""
    await _seed_topology("a-broken", USER_A, BAD_STORED[kind])
    await _seed_topology("b-lab-1", USER_B, _good_canvas("sw-b1"))
    await _seed_topology("b-lab-2", USER_B, None)
    _as(USER_B)
    resp = await client.get("/topologies/export", params={"format": fmt})
    assert resp.status_code == 200, resp.text
    assert resp.headers["x-export-skipped"] == "1"
    if fmt == "json":
        names = [item["name"] for item in resp.json()["items"]]
        assert names == ["b-lab-1", "b-lab-2"]
    else:
        rows = list(csv.DictReader(io.StringIO(resp.text)))
        assert {r["topology_name"] for r in rows} == {"b-lab-1"}
        assert {r["source_device"] for r in rows} == {"sw-b1"}


@pytest.mark.asyncio
async def test_an_export_with_nothing_bad_skips_nothing(client):
    await _seed_topology("lab", USER_B, _good_canvas())
    resp = await client.get("/topologies/export", params={"format": "json"})
    assert resp.status_code == 200
    assert resp.headers["x-export-skipped"] == "0"
    assert [i["name"] for i in resp.json()["items"]] == ["lab"]


@pytest.mark.asyncio
async def test_a_skipped_topology_is_logged_by_id_and_problem_only(client, caplog):
    marker = "marker-9f3c"
    topology_id = await _seed_topology("bad", USER_A, {"nodes": [{"id": [marker]}]})
    resp = await client.get("/topologies/export", params={"format": "json"})
    assert resp.status_code == 200
    lines = [r.getMessage() for r in caplog.records if "stored_canvas_unreadable" in r.getMessage()]
    assert lines == [
        f"stored_canvas_unreadable kind=topology id={topology_id} "
        "problem=canvas_data.nodes.0.id: must be a string"
    ]
    assert marker not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", list(BAD_STORED))
async def test_the_topology_list_still_lists_a_bad_row(client, kind):
    await _seed_topology("bad", USER_A, BAD_STORED[kind])
    await _seed_topology("good", USER_B, _good_canvas())
    _as(USER_B)
    resp = await client.get("/topologies", params={"sort_by": "name", "sort_dir": "asc"})
    assert resp.status_code == 200, resp.text
    assert [t["name"] for t in resp.json()["items"]] == ["bad", "good"]


# Read side: every topology reader ------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", list(BAD_STORED))
async def test_every_reader_of_a_bad_topology_answers_409(client, kind):
    topology_id = await _seed_topology("bad", USER_A, BAD_STORED[kind])
    (version_id,) = await _version_ids(topology_id)
    for user in (USER_B, ADMIN):
        _as(user)
        assert (await client.get(f"/topologies/{topology_id}")).json() == UNREADABLE_409
        resp = await client.get(f"/topologies/{topology_id}")
        assert resp.status_code == 409
        resp = await client.post(f"/topologies/{topology_id}/clone", json={"name": "copy"})
        assert (resp.status_code, resp.json()) == (409, UNREADABLE_409)
        resp = await client.post(
            f"/templates/from-topology/{topology_id}", json={"name": f"t-{user[:8]}"}
        )
        assert (resp.status_code, resp.json()) == (409, UNREADABLE_409)
        resp = await client.get(f"/topologies/{topology_id}/versions/{version_id}")
        assert (resp.status_code, resp.json()) == (409, UNREADABLE_409)
        resp = await client.get(f"/topologies/{topology_id}/versions")
        assert resp.status_code == 200
    _as(ADMIN)
    resp = await client.post(f"/topologies/{topology_id}/validate")
    assert (resp.status_code, resp.json()) == (409, UNREADABLE_409)
    resp = await client.post(f"/topologies/{topology_id}/validate/internal", headers=_internal())
    assert (resp.status_code, resp.json()) == (409, UNREADABLE_409)
    async with TestSessionLocal() as db:
        names = (await db.execute(select(Topology.name))).scalars().all()
        templates = (await db.execute(select(TopologyTemplate))).scalars().all()
    assert names == ["bad"]
    assert templates == []


@pytest.mark.asyncio
async def test_a_stored_canvas_past_the_inline_budget_is_judged_on_a_worker_thread(
    client, monkeypatch
):
    """A topology read whose canvas holds more than `INLINE_CHECK_VALUES` values is
    judged on a bulk worker thread, good or bad, with the answer a small one gets."""
    seen: list[int] = []
    real = canvas_shape.stored_shape_problem

    def recording(canvas):
        seen.append(threading.get_ident())
        return real(canvas)

    monkeypatch.setattr(canvas_shape, "stored_shape_problem", recording)
    wide = [{} for _ in range(INLINE_CHECK_VALUES)]
    good_id = await _seed_topology("wide-good", USER_A, {"nodes": [], "edges": [*wide, {}]})
    bad_id = await _seed_topology("wide-bad", USER_A, {"nodes": [], "edges": [*wide, 1]})
    small_id = await _seed_topology("small", USER_A, _good_canvas())
    loop_thread = threading.get_ident()
    assert (await client.get(f"/topologies/{good_id}")).status_code == 200
    resp = await client.get(f"/topologies/{bad_id}")
    assert (resp.status_code, resp.json()) == (409, UNREADABLE_409)
    assert len(seen) == 2 and loop_thread not in seen
    assert (await client.get(f"/topologies/{small_id}")).status_code == 200
    assert len(seen) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_side", ["a", "b"])
@pytest.mark.parametrize("kind", ["node_not_an_object", "deep", "lone_surrogate"])
async def test_a_diff_or_restore_of_a_bad_version_answers_409(client, kind, bad_side):
    topology_id = await _seed_topology("lab", USER_A, BAD_STORED[kind])
    resp = await client.put(f"/topologies/{topology_id}", json={"canvas_data": _good_canvas()})
    assert resp.status_code == 200, resp.text
    bad_version, good_version = await _version_ids(topology_id)
    sides = {"a": bad_version, "b": good_version}
    if bad_side == "b":
        sides = {"a": good_version, "b": bad_version}
    resp = await client.get(f"/topologies/{topology_id}/versions/diff", params=sides)
    assert (resp.status_code, resp.json()) == (409, UNREADABLE_409)
    resp = await client.post(f"/topologies/{topology_id}/versions/{bad_version}/restore", json={})
    assert (resp.status_code, resp.json()) == (409, UNREADABLE_409)
    assert await _stored_canvas(Topology, topology_id) == _good_canvas()
    assert len(await _version_ids(topology_id)) == 2
    resp = await client.get(f"/topologies/{topology_id}/versions/{good_version}")
    assert resp.status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", list(BAD_STORED))
async def test_the_owner_repairs_a_bad_topology_with_a_put_or_deletes_it(client, kind):
    topology_id = await _seed_topology("bad", USER_A, BAD_STORED[kind])
    # A name-only update would answer the stored canvas: refused, nothing written.
    resp = await client.put(f"/topologies/{topology_id}", json={"name": "renamed"})
    assert (resp.status_code, resp.json()) == (409, UNREADABLE_409)
    async with TestSessionLocal() as db:
        assert (await db.get(Topology, topology_id)).name == "bad"
    # Another user still cannot touch it: the 403 comes first.
    _as(USER_B)
    resp = await client.put(f"/topologies/{topology_id}", json={"canvas_data": _good_canvas()})
    assert resp.status_code == 403
    _as(USER_A)
    resp = await client.put(
        f"/topologies/{topology_id}", json={"name": "fixed", "canvas_data": _good_canvas()}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["canvas_data"] == _good_canvas()
    resp = await client.get(f"/topologies/{topology_id}")
    assert resp.status_code == 200
    assert resp.json()["name"] == "fixed"
    assert len(await _version_ids(topology_id)) == 2

    other_id = await _seed_topology("bad-2", USER_A, BAD_STORED[kind])
    resp = await client.delete(f"/topologies/{other_id}")
    assert resp.status_code == 204
    async with TestSessionLocal() as db:
        assert await db.get(Topology, other_id) is None


# Read side: templates ------------------------------------------------------------------------


async def _seed_template(canvas, owner: str = USER_A) -> uuid.UUID:
    template_id = uuid.uuid4()
    async with TestSessionLocal() as db:
        db.add(
            TopologyTemplate(
                id=template_id,
                name=f"tmpl-{template_id.hex[:8]}",
                canvas_data=canvas,
                created_by=uuid.UUID(owner),
                owner_name=PAYLOADS[owner]["username"],
            )
        )
        await db.commit()
    return template_id


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", list(BAD_STORED))
async def test_a_bad_template_answers_409_and_its_owner_can_replace_it(client, kind):
    template_id = await _seed_template(BAD_STORED[kind])
    good_id = await _seed_template({"nodes": [], "edges": []}, owner=USER_B)
    _as(USER_B)
    resp = await client.get(f"/templates/{template_id}")
    assert (resp.status_code, resp.json()) == (409, UNREADABLE_409)
    resp = await client.post(
        f"/templates/{template_id}/instantiate", json={"name": "lab", "role_assignments": {}}
    )
    assert (resp.status_code, resp.json()) == (409, UNREADABLE_409)
    resp = await client.get("/templates")
    assert resp.status_code == 200
    assert {t["id"] for t in resp.json()["items"]} == {str(template_id), str(good_id)}
    _as(USER_A)
    resp = await client.put(f"/templates/{template_id}", json={"description": "x"})
    assert (resp.status_code, resp.json()) == (409, UNREADABLE_409)
    resp = await client.put(
        f"/templates/{template_id}", json={"canvas_data": {"nodes": [], "edges": []}}
    )
    assert resp.status_code == 200, resp.text
    resp = await client.get(f"/templates/{template_id}")
    assert resp.status_code == 200
    async with TestSessionLocal() as db:
        assert (await db.execute(select(Topology))).scalars().all() == []


# Read side: forks ----------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("saved", ["good", "bad"])
@pytest.mark.parametrize("kind", list(BAD_STORED))
async def test_the_fork_read_answers_its_wiring_over_a_bad_draft(client, kind, saved):
    """Execution reads the wiring from this route: a bad draft is answered by the
    latest saved canvas, or as null when that one is bad too, and every other part
    as stored."""
    saved_canvas = _good_canvas() if saved == "good" else copy.deepcopy(BAD_STORED[kind])
    reservation_id, _, version_id = await _seed_fork(BAD_STORED[kind], saved_canvas)
    resp = await client.get(f"/internal/forks/{reservation_id}", headers=_internal())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["canvas_data"] == (_good_canvas() if saved == "good" else None)
    assert [(c["device_a_id"], c["device_b_id"], c["edge_key"]) for c in body["connections"]] == [
        (DEV_A, DEV_B, "e1")
    ]
    assert [v["id"] for v in body["versions"]] == [str(version_id)]


async def _wired_fork() -> tuple[uuid.UUID, uuid.UUID, dict]:
    """A fork built through the real routes: created empty, then saved with one
    cable between DEV_A and DEV_B over a physical connection, so it holds one
    connection row and version 2. Returns (reservation_id, fork_id, saved canvas)."""
    async with TestSessionLocal() as db:
        db.add(
            Connection(
                device_a_id=uuid.UUID(DEV_A),
                port_a="eth0",
                device_b_id=uuid.UUID(DEV_B),
                port_b="eth0",
                created_by="seed",
            )
        )
        await db.commit()
    reservation_id = uuid.uuid4()
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as raw:
        created = await raw.post(
            "/internal/forks",
            json={"reservation_id": str(reservation_id), "member_device_ids": [DEV_A, DEV_B]},
            headers=_internal(),
        )
        assert created.status_code == 201, created.text
        saved = await raw.post(
            f"/internal/forks/{reservation_id}/save",
            json={"canvas_data": _good_canvas(), "member_device_ids": [DEV_A, DEV_B]},
            headers=_internal(),
        )
        assert saved.status_code == 200, saved.text
        assert len(saved.json()["built"]) == 1
    return reservation_id, uuid.UUID(created.json()["fork_id"]), _good_canvas()


async def _connection_rows(fork_id: uuid.UUID) -> list[tuple]:
    async with TestSessionLocal() as db:
        rows = (
            (await db.execute(select(ForkConnection).where(ForkConnection.fork_id == fork_id)))
            .scalars()
            .all()
        )
    return sorted((str(r.id), r.port_a, r.port_b, r.edge_key) for r in rows)


async def _store(model, row_id: uuid.UUID, canvas) -> None:
    async with TestSessionLocal() as db:
        (await db.get(model, row_id)).canvas_data = canvas
        await db.commit()


async def _latest_fork_version(fork_id: uuid.UUID) -> ForkVersion:
    async with TestSessionLocal() as db:
        return (
            (
                await db.execute(
                    select(ForkVersion)
                    .where(ForkVersion.fork_id == fork_id)
                    .order_by(ForkVersion.version_number.desc())
                )
            )
            .scalars()
            .first()
        )


EMPTY_OVER_UNREADABLE_409 = {
    "detail": "The stored draft cannot be read and this fork holds wiring; restore a saved "
    "version, or rebuild the canvas, before saving a canvas with no nodes"
}


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", list(BAD_STORED))
async def test_a_fork_read_over_a_bad_draft_answers_the_latest_saved_canvas(client, kind):
    """Open, then Save: the read answers the canvas the wiring was saved from, and
    saving exactly what was read leaves every connection row as it was."""
    reservation_id, fork_id, saved_canvas = await _wired_fork()
    before = await _connection_rows(fork_id)
    assert len(before) == 1
    await _store(ReservationFork, fork_id, copy.deepcopy(BAD_STORED[kind]))
    read = await client.get(f"/internal/forks/{reservation_id}", headers=_internal())
    assert read.status_code == 200, read.text
    assert read.json()["canvas_data"] == saved_canvas
    resp = await client.post(
        f"/internal/forks/{reservation_id}/save",
        json={"canvas_data": read.json()["canvas_data"], "member_device_ids": [DEV_A, DEV_B]},
        headers=_internal(),
    )
    assert resp.status_code == 200, resp.text
    assert (resp.json()["released"], resp.json()["built"]) == ([], [])
    assert resp.json()["unchanged_count"] == 1
    assert await _connection_rows(fork_id) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["node_not_an_object", "deep", "lone_surrogate"])
@pytest.mark.parametrize("saved", ["bad", "null"])
async def test_an_empty_canvas_over_a_bad_draft_is_refused_while_the_fork_holds_wiring(
    client, kind, saved
):
    """With neither the draft nor the latest saved canvas readable, the read
    answers no canvas, and saving or putting the empty canvas an editor opened on
    it would send is refused: no connection row is released."""
    reservation_id, fork_id, saved_canvas = await _wired_fork()
    before = await _connection_rows(fork_id)
    await _store(ReservationFork, fork_id, copy.deepcopy(BAD_STORED[kind]))
    latest = await _latest_fork_version(fork_id)
    await _store(
        ForkVersion, latest.id, copy.deepcopy(BAD_STORED[kind]) if saved == "bad" else None
    )
    # An older version that is readable is not the state the wiring was saved
    # from, so it is never answered in the draft's place.
    async with TestSessionLocal() as db:
        first = (
            await db.execute(
                select(ForkVersion).where(
                    ForkVersion.fork_id == fork_id, ForkVersion.version_number == 1
                )
            )
        ).scalar_one()
        first.canvas_data = {"nodes": [{"id": "older"}], "edges": []}
        await db.commit()
    read = await client.get(f"/internal/forks/{reservation_id}", headers=_internal())
    assert read.status_code == 200, read.text
    assert read.json()["canvas_data"] is None
    for empty in ({"nodes": [], "edges": []}, {}, {"nodes": None, "edges": []}):
        resp = await client.post(
            f"/internal/forks/{reservation_id}/save",
            json={"canvas_data": empty, "member_device_ids": [DEV_A, DEV_B]},
            headers=_internal(),
        )
        assert (resp.status_code, resp.json()) == (409, EMPTY_OVER_UNREADABLE_409)
        resp = await client.put(
            f"/internal/forks/{reservation_id}/canvas",
            json={"canvas_data": empty},
            headers=_internal(),
        )
        assert (resp.status_code, resp.json()) == (409, EMPTY_OVER_UNREADABLE_409)
    assert await _connection_rows(fork_id) == before
    assert await _stored_canvas(ReservationFork, fork_id) == BAD_STORED[kind]
    assert (await _latest_fork_version(fork_id)).version_number == 2
    # A canvas with nodes is a deliberate rebuild, and saves as any other.
    resp = await client.post(
        f"/internal/forks/{reservation_id}/save",
        json={"canvas_data": saved_canvas, "member_device_ids": [DEV_A, DEV_B]},
        headers=_internal(),
    )
    assert resp.status_code == 200, resp.text
    assert await _connection_rows(fork_id) == before


@pytest.mark.asyncio
async def test_an_empty_canvas_saves_when_the_draft_is_readable_or_nothing_is_wired(client):
    """The refusal needs all four: no nodes, a draft without the shape, a read that
    answers no canvas in its place, and wiring."""
    reservation_id, fork_id, _ = await _wired_fork()
    # A readable draft is answered as it is, whatever the saved versions hold.
    await _store(ForkVersion, (await _latest_fork_version(fork_id)).id, None)
    empty = {"nodes": [], "edges": []}
    resp = await client.put(
        f"/internal/forks/{reservation_id}/canvas", json={"canvas_data": empty}, headers=_internal()
    )
    assert resp.status_code == 200, resp.text
    resp = await client.post(
        f"/internal/forks/{reservation_id}/save",
        json={"canvas_data": empty, "member_device_ids": [DEV_A, DEV_B]},
        headers=_internal(),
    )
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["released"]) == 1
    assert await _connection_rows(fork_id) == []
    await _store(ReservationFork, fork_id, copy.deepcopy(BAD_STORED["node_not_an_object"]))
    await _store(ForkVersion, (await _latest_fork_version(fork_id)).id, None)
    resp = await client.post(
        f"/internal/forks/{reservation_id}/save",
        json={"canvas_data": empty, "member_device_ids": [DEV_A, DEV_B]},
        headers=_internal(),
    )
    assert resp.status_code == 200, resp.text
    # An L3 route row is wiring too.
    async with TestSessionLocal() as db:
        db.add(
            ForkL3Route(
                fork_id=fork_id,
                device_id=uuid.UUID(DEV_A),
                destination="10.0.0.0/24",
                interface="eth0",
                route_key="seed-route",
                created_by="seed",
            )
        )
        await db.commit()
    await _store(ReservationFork, fork_id, copy.deepcopy(BAD_STORED["node_not_an_object"]))
    await _store(ForkVersion, (await _latest_fork_version(fork_id)).id, None)
    resp = await client.post(
        f"/internal/forks/{reservation_id}/save",
        json={"canvas_data": empty, "member_device_ids": [DEV_A, DEV_B]},
        headers=_internal(),
    )
    assert (resp.status_code, resp.json()) == (409, EMPTY_OVER_UNREADABLE_409)
    resp = await client.put(
        f"/internal/forks/{reservation_id}/canvas", json={"canvas_data": empty}, headers=_internal()
    )
    assert (resp.status_code, resp.json()) == (409, EMPTY_OVER_UNREADABLE_409)


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["put", "save"])
@pytest.mark.parametrize("kind", ["node_not_an_object", "deep", "lone_surrogate"])
async def test_an_empty_canvas_over_a_bad_draft_saves_when_the_read_answers_the_saved_canvas(
    client, kind, route
):
    """The read answers the latest saved canvas in the bad draft's place, so the
    editor showed the real drawing: clearing it is deliberate, and an empty canvas
    saves as over any draft, the save releasing the wiring."""
    reservation_id, fork_id, saved_canvas = await _wired_fork()
    before = await _connection_rows(fork_id)
    assert len(before) == 1
    await _store(ReservationFork, fork_id, copy.deepcopy(BAD_STORED[kind]))
    read = await client.get(f"/internal/forks/{reservation_id}", headers=_internal())
    assert read.json()["canvas_data"] == saved_canvas
    empty = {"nodes": [], "edges": []}
    if route == "put":
        resp = await client.put(
            f"/internal/forks/{reservation_id}/canvas",
            json={"canvas_data": empty},
            headers=_internal(),
        )
        assert resp.status_code == 200, resp.text
        assert await _stored_canvas(ReservationFork, fork_id) == empty
        assert await _connection_rows(fork_id) == before
    else:
        resp = await client.post(
            f"/internal/forks/{reservation_id}/save",
            json={"canvas_data": empty, "member_device_ids": [DEV_A, DEV_B]},
            headers=_internal(),
        )
        assert resp.status_code == 200, resp.text
        assert len(resp.json()["released"]) == 1
        assert await _connection_rows(fork_id) == []
        assert (await _latest_fork_version(fork_id)).version_number == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", list(BAD_STORED))
async def test_a_bad_fork_version_answers_409_on_read_and_restore(client, kind):
    reservation_id, fork_id, version_id = await _seed_fork(_good_canvas(), BAD_STORED[kind])
    resp = await client.get(
        f"/internal/forks/{reservation_id}/versions/{version_id}", headers=_internal()
    )
    assert (resp.status_code, resp.json()) == (409, UNREADABLE_409)
    resp = await client.post(
        f"/internal/forks/{reservation_id}/versions/{version_id}/restore", headers=_internal()
    )
    assert (resp.status_code, resp.json()) == (409, UNREADABLE_409)
    assert await _stored_canvas(ReservationFork, fork_id) == _good_canvas()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", list(BAD_STORED))
async def test_a_fork_is_not_created_from_a_bad_parent(client, kind):
    topology_id = await _seed_topology("parent", USER_A, BAD_STORED[kind])
    resp = await client.post(
        "/internal/forks",
        json={
            "reservation_id": str(uuid.uuid4()),
            "parent_topology_id": str(topology_id),
            "member_device_ids": [DEV_A, DEV_B],
        },
        headers=_internal(),
    )
    assert (resp.status_code, resp.json()) == (409, UNREADABLE_409)
    async with TestSessionLocal() as db:
        assert (await db.execute(select(ReservationFork))).scalars().all() == []


def _long_edge_id_canvas() -> dict:
    canvas = _good_canvas()
    canvas["edges"][0]["id"] = "e" * 300
    return canvas


@pytest.mark.asyncio
async def test_a_stored_long_edge_id_is_read_and_refused_only_where_it_is_written(client):
    """The edge id bound is the width of the fork's wiring column: a stored
    topology or template canvas with a longer id is read, exported, and replaced as
    any other, a body carrying it is refused, and a fork is not created from it."""
    topology_id = await _seed_topology("long-id", USER_A, _long_edge_id_canvas())
    template_id = await _seed_template(_long_edge_id_canvas())
    _as(USER_A)
    resp = await client.get(f"/topologies/{topology_id}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["canvas_data"]["edges"][0]["id"] == "e" * 300
    resp = await client.get(f"/templates/{template_id}")
    assert resp.status_code == 200, resp.text
    resp = await client.get("/topologies/export", params={"format": "json"})
    assert resp.status_code == 200, resp.text
    assert resp.headers["x-export-skipped"] == "0"
    assert [i["name"] for i in resp.json()["items"]] == ["long-id"]
    resp = await client.put(
        f"/topologies/{topology_id}", json={"canvas_data": _long_edge_id_canvas()}
    )
    assert (resp.status_code, resp.json()) == (
        422,
        _expected_422("canvas_data.edges.0.id: is longer than 255 characters"),
    )
    resp = await client.post(
        "/internal/forks",
        json={
            "reservation_id": str(uuid.uuid4()),
            "parent_topology_id": str(topology_id),
            "member_device_ids": [DEV_A, DEV_B],
        },
        headers=_internal(),
    )
    assert (resp.status_code, resp.json()) == (409, UNREADABLE_409)
    async with TestSessionLocal() as db:
        assert (await db.execute(select(ReservationFork))).scalars().all() == []
        assert (await db.execute(select(ForkConnection))).scalars().all() == []
    # The owner repairs it with a canvas whose edge id fits.
    resp = await client.put(f"/topologies/{topology_id}", json={"canvas_data": _good_canvas()})
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_a_fork_draft_with_a_long_edge_id_is_read_and_its_save_refused(client):
    reservation_id, fork_id, _ = await _seed_fork(_long_edge_id_canvas(), _good_canvas())
    before = await _connection_rows(fork_id)
    assert [row[3] for row in before] == ["e1"]
    resp = await client.get(f"/internal/forks/{reservation_id}", headers=_internal())
    assert resp.status_code == 200, resp.text
    assert resp.json()["canvas_data"]["edges"][0]["id"] == "e" * 300
    resp = await client.post(
        f"/internal/forks/{reservation_id}/save",
        json={"canvas_data": resp.json()["canvas_data"], "member_device_ids": [DEV_A, DEV_B]},
        headers=_internal(),
    )
    assert (resp.status_code, resp.json()) == (
        422,
        _expected_422("canvas_data.edges.0.id: is longer than 255 characters"),
    )
    assert await _connection_rows(fork_id) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["node_not_an_object", "edge_source_a_list", "deep"])
async def test_a_prune_over_a_bad_draft_and_saved_canvas_still_releases(client, kind):
    """The saved canvas is read as none, so the row touching the removed device
    releases; the bad draft is left as it is; the new version's canvas is null."""
    reservation_id, fork_id, _ = await _seed_fork(
        copy.deepcopy(BAD_STORED[kind]), copy.deepcopy(BAD_STORED[kind])
    )
    resp = await client.post(
        f"/internal/forks/{reservation_id}/prune-devices",
        json={"device_ids": [DEV_B]},
        headers=_internal(),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["changed"] is True
    assert [(d["device_a_id"], d["device_b_id"]) for d in body["released"]] == [(DEV_A, DEV_B)]
    async with TestSessionLocal() as db:
        fork = await db.get(ReservationFork, fork_id)
        assert fork.canvas_data == BAD_STORED[kind]
        latest = (
            (
                await db.execute(
                    select(ForkVersion)
                    .where(ForkVersion.fork_id == fork_id)
                    .order_by(ForkVersion.version_number.desc())
                )
            )
            .scalars()
            .first()
        )
        assert latest.version_number == 2
        assert latest.canvas_data is None
        assert (await db.execute(select(ForkConnection))).scalars().all() == []


# Text written to a column ---------------------------------------------------------------

NUL_422_ITEM = {
    "type": "text_holds_nul",
    "msg": "Value holds a NUL character, which no database column takes",
}


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["a\u0000b", "\u0000", "ab\u0000"], ids=["middle", "only", "end"])
async def test_every_name_and_description_holding_a_nul_is_a_422(client, text):
    """Each request field cabling writes to a name or description column refuses a
    NUL character with one 422 item naming the field, and writes nothing."""
    topology_id = await _seed_topology("lab", USER_A, _good_canvas())
    (version_id,) = await _version_ids(topology_id)
    template = await client.post("/templates", json={"name": "tmpl"})
    assert template.status_code == 201, template.text
    template_id = template.json()["id"]
    sends = [
        (client.post, "/topologies", {"name": text}, ["body", "name"]),
        (client.put, f"/topologies/{topology_id}", {"name": text}, ["body", "name"]),
        (client.put, f"/topologies/{topology_id}", {"description": text}, ["body", "description"]),
        (client.post, f"/topologies/{topology_id}/clone", {"name": text}, ["body", "name"]),
        (
            client.post,
            f"/topologies/{topology_id}/versions/{version_id}/restore",
            {"description": text},
            ["body", "description"],
        ),
        (client.post, "/templates", {"name": text}, ["body", "name"]),
        (client.post, "/templates", {"name": "t2", "description": text}, ["body", "description"]),
        (client.put, f"/templates/{template_id}", {"name": text}, ["body", "name"]),
        (
            client.put,
            f"/templates/{template_id}",
            {"description": text},
            ["body", "description"],
        ),
        (
            client.post,
            f"/templates/{template_id}/instantiate",
            {"name": text, "role_assignments": {}},
            ["body", "name"],
        ),
        (
            client.post,
            f"/templates/from-topology/{topology_id}",
            {"name": text},
            ["body", "name"],
        ),
        (
            client.post,
            f"/templates/from-topology/{topology_id}",
            {"name": "t3", "description": text},
            ["body", "description"],
        ),
    ]
    for send, url, body, loc in sends:
        resp = await send(url, json=body)
        assert resp.status_code == 422, (url, resp.text)
        assert resp.json() == {"detail": [{**NUL_422_ITEM, "loc": loc}]}, url
    async with TestSessionLocal() as db:
        assert (await db.execute(select(Topology.name))).scalars().all() == ["lab"]
        templates = (await db.execute(select(TopologyTemplate.name))).scalars().all()
    assert templates == ["tmpl"]
    assert len(await _version_ids(topology_id)) == 1
