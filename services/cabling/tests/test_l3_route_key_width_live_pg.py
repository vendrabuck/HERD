"""Postgres-live coverage for the route_key column-width fix (issue #758).

``fork_l3_routes.route_key`` was ``String(400)``: a route whose fields hit
their 64-character cap (in particular a non-ASCII ``virtual_router``, since
the default ``json.dumps`` escaping inflates each non-ASCII character to a
six-byte ``\\uXXXX`` sequence) could pack past 400 characters, and Postgres
rejects the row inside the locked reconcile transaction with
``StringDataRightTruncation``. SQLite (this suite's usual dialect) never
enforces VARCHAR width, so no test in ``test_forks.py`` or
``test_l3_intent.py`` can prove the column itself holds real Postgres input.
This file is that missing proof, run against a real Postgres, using the same
env contract as ``test_fork_port_claim_race_live_pg.py`` and
``test_fork_restore_save_race_live_pg.py``:

    HERD_TEST_PG_DSN        SQLAlchemy asyncpg DSN of the database to use. Unset or
                            empty, the suite is skipped and connects to nothing.
    HERD_TEST_PG_REQUIRED   "1" (or any value not in ("", "0")) turns that skip into
                            a hard failure. A named database that does not answer
                            fails every test either way (herd_common.pg_live_suite).

It saves a fork whose canvas carries one L3 route with a 64-character
non-ASCII ``virtual_router`` (the fix's own worst-case shape, chosen to match
``test_l3_intent.py``'s ``test_route_key_worst_case_field_widths_round_trip``
unit coverage) directly through ``fork_save_service.save_fork``, then reads
it back through the real ``get_fork_internal`` route handler (called
directly with a live session rather than over HTTP, the same "call the
handler function" idiom the rest of this live-PG suite uses) to prove both
the write and the ``ForkL3RouteResponse`` read-back round-trip the field
byte-for-byte.

This file creates exactly one throwaway ``reservation_fork`` row (random UUID
reservation_id, no cross-schema FK) and deletes it in a ``finally``;
``fork_versions`` and ``fork_l3_routes`` rows cascade-delete with their fork
(``ON DELETE CASCADE`` on both ``fork_id`` columns).
"""

from __future__ import annotations

import uuid

import pytest
from app.models.fork import ForkL3Route, ForkStatus_ACTIVE, ForkVersion, ReservationFork
from app.routes.forks import get_fork_internal
from app.services.fork_save_service import resolve_canvas_wiring
from app.services.fork_save_service import save_fork as _real_save_fork
from app.services.l3_intent import parse_l3_intent
from herd_common.pg_live_suite import pg_live_target
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

_PG = pg_live_target()
PG_DSN = _PG.dsn


pytestmark = pytest.mark.skipif(_PG.skip_reason is not None, reason=_PG.skip_reason or "")


@pytest.fixture(autouse=True)
def _fail_when_pg_live_cannot_run():
    if _PG.failure is not None:
        pytest.fail(_PG.failure)


@pytest.fixture(autouse=True)
def _use_cabling_schema():
    """Point the ORM Table objects at the real "cabling" schema for this file only.

    Mirrors the sibling live-PG files' fixture of the same name:
    services/cabling/conftest.py forces DB_SCHEMA="" for the SQLite unit suite, so
    every cabling model's mapped Table carries schema=None by the time this module's
    tests run. The live database's migrations created these tables under the real
    "cabling" schema.
    """
    tables = (
        ReservationFork.__table__,
        ForkVersion.__table__,
        ForkL3Route.__table__,
    )
    original = tuple(t.schema for t in tables)
    for t in tables:
        t.schema = "cabling"
    try:
        yield
    finally:
        for t, schema in zip(tables, original, strict=True):
            t.schema = schema


@pytest.fixture
async def pg_engine():
    engine = create_async_engine(PG_DSN)
    yield engine
    await engine.dispose()


@pytest.fixture
def session_factory(pg_engine):
    # expire_on_commit=False mirrors herd_common.database.make_database, the
    # production session factory shape every service actually runs with.
    return async_sessionmaker(pg_engine, expire_on_commit=False)


async def _make_empty_active_fork(session_factory) -> tuple[uuid.UUID, uuid.UUID]:
    """Persist a throwaway ACTIVE fork with an empty v1 snapshot (no wiring yet).

    Returns (reservation_id, fork_id).
    """
    reservation_id = uuid.uuid4()
    async with session_factory() as db:
        fork = ReservationFork(reservation_id=reservation_id, status=ForkStatus_ACTIVE)
        db.add(fork)
        await db.flush()
        db.add(ForkVersion(fork_id=fork.id, version_number=1))
        await db.commit()
        return reservation_id, fork.id


async def _delete_fork(session_factory, fork_id: uuid.UUID) -> None:
    async with session_factory() as db:
        await db.execute(delete(ReservationFork).where(ReservationFork.id == fork_id))
        await db.commit()


@pytest.mark.asyncio
async def test_worst_case_virtual_router_round_trips_through_save_and_internal_get(
    session_factory,
):
    """The #758 proof: a 64-character non-ASCII virtual_router (this fix's own
    worst case) survives save_fork's Postgres INSERT and reads back byte-for
    -byte through the real GET /internal/forks/{reservation_id} handler.

    Before the fix (route_key ``String(400)``, ``json.dumps`` with the default
    ``ensure_ascii=True``), this save raises ``StringDataRightTruncation``
    inside the reconcile transaction.
    """
    device_id = uuid.uuid4()
    virtual_router = "é" * 64  # non-ASCII; \\uXXXX-escapes to 6 bytes each under
    # the old ensure_ascii=True packing, well past the old 400-char column cap
    canvas = {
        "nodes": [
            {
                "id": "n1",
                "data": {
                    "device": {"id": str(device_id)},
                    "l3": {
                        "routes": [
                            {
                                "destination": "10.0.0.0/24",
                                "interface": "eth0",
                                "next_hop": None,
                                "virtual_router": virtual_router,
                            }
                        ]
                    },
                },
            }
        ],
        "edges": [],
    }

    reservation_id, fork_id = await _make_empty_active_fork(session_factory)
    try:
        async with session_factory() as db:
            fork = await db.get(ReservationFork, fork_id)
            wiring_resolution = await resolve_canvas_wiring(db, canvas)
            intended_routes = parse_l3_intent(canvas)
            await _real_save_fork(
                db,
                fork,
                canvas_data=canvas,
                member_device_ids={device_id},
                wiring_resolution=wiring_resolution,
                intended_routes=intended_routes,
                created_by="route-key-width-test",
            )

        # Read back through the real internal fork GET route handler, called
        # directly with a live session (this suite's convention for exercising
        # route logic without a running HTTP server).
        with pytest.MonkeyPatch.context() as mp:
            import app.routes.forks as forks_module

            mp.setattr(forks_module.settings, "internal_api_token", "route-key-width-test-token")
            async with session_factory() as db:
                detail = await get_fork_internal(
                    reservation_id=reservation_id,
                    x_internal_token="route-key-width-test-token",
                    authorization=None,
                    db=db,
                )

        assert len(detail.l3_routes) == 1
        route = detail.l3_routes[0]
        assert route.device_id == device_id
        assert route.destination == "10.0.0.0/24"
        assert route.interface == "eth0"
        assert route.next_hop is None
        assert route.virtual_router == virtual_router
        assert len(route.virtual_router) == 64
    finally:
        await _delete_fork(session_factory, fork_id)
