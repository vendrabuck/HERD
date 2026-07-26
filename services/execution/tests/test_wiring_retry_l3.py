"""Tests for L3 route-pin rows in the wiring retry channels (ADR 0009 phase 5, issue #416).

Covers direction-aware reattempt (an ACTIVE-intended FAILED pin re-provisions the pinned
set via configure_route, a RELEASED-intended one re-removes it via remove_route), the
verbatim-pinned-set discipline (issue #20: the retry drives the STORED routes, never a
re-derived config), the direction-scoped freeze, per-layer labeling in a mixed batch, and
the absence of an L3 supersession guard (a per-reservation route set is non-exclusive, so a
stale release cannot harm another reservation).
"""

import uuid
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

import pytest
from app.database import Base
from app.models.l1_connection_assignment import L1ConnectionAssignment
from app.models.reservation_wiring_state import ReservationWiringState
from app.models.route_assignment import RouteAssignment
from app.services.wiring_retry_service import (
    WiringReservationFrozen,
    reattempt_reservation,
    run_wiring_retry_tick,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

test_engine = create_async_engine(
    "sqlite+aiosqlite:///:memory:",
    echo=False,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestSessionLocal = async_sessionmaker(test_engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
async def setup_db():
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


def _db_session_factory():
    class _Ctx:
        async def __aenter__(self):
            self._session = TestSessionLocal()
            return self._session

        async def __aexit__(self, *args):
            await self._session.close()

    return lambda: _Ctx()


SW_L3 = str(uuid.uuid4())
SW_L3_B = str(uuid.uuid4())
DRIVER_ID = str(uuid.uuid4())
RES_ID = str(uuid.uuid4())


def _switch_data(device_id):
    return {
        "id": device_id,
        "name": "L3",
        "template_id": "tmpl-1",
        "driver_id": DRIVER_ID,
        "driver_sha256": "sha",
        "driver_filename": "driver.zip",
        "connection_type": "Layer 3 Switch",
        "field_data": {"ip": "10.0.0.1"},
    }


SWITCH_DATA = _switch_data(SW_L3)
SWITCHES = {SW_L3: SWITCH_DATA, SW_L3_B: _switch_data(SW_L3_B)}
TEMPLATE_DATA = {"id": "tmpl-1", "name": "L3 Template", "sections": []}
SUCCESS_RESULT = {"success": True, "output": {"result": True}, "error": None, "duration_ms": 5}

PINNED = [
    {"destination": "10.0.0.0/24", "next_hop": "192.168.1.1", "interface": "eth0"},
    {"destination": "10.1.0.0/24", "next_hop": None, "interface": "eth1"},
]
CURRENT_CONFIG = [{"destination": "203.0.113.0/24", "next_hop": None, "interface": "eth9"}]


def _recorder(fail=None):
    fail = fail or set()
    calls = []

    def execute_fn(driver_path, action, context, **kwargs):
        mk = kwargs.get("method_kwargs") or {}
        calls.append((action, mk.get("destination")))
        if action in fail or (action, mk.get("destination")) in fail:
            return {"success": False, "output": None, "error": "boom", "duration_ms": 1}
        return SUCCESS_RESULT

    return execute_fn, calls


def _patches(execute_fn):
    async def _device(device_id, client=None):
        return SWITCHES.get(str(device_id))

    # The switch config is EDITED out from under the pin: any retry that re-derives from
    # config (a bug) would drive CURRENT_CONFIG, which the assertions catch.
    async def _config(device_id, client=None):
        return {"config": {"routes": CURRENT_CONFIG}}

    stack = ExitStack()
    for p in [
        patch("app.services.nats_consumer._fetch_device", new=AsyncMock(side_effect=_device)),
        patch(
            "app.services.nats_consumer._fetch_template", new=AsyncMock(return_value=TEMPLATE_DATA)
        ),
        patch(
            "app.services.nats_consumer._fetch_latest_config", new=AsyncMock(side_effect=_config)
        ),
        patch("app.services.driver_loader.load_driver", new=AsyncMock(return_value="/tmp/driver")),
        patch("app.services.driver_sandbox.execute_driver_method", side_effect=execute_fn),
    ]:
        stack.enter_context(p)
    return stack


async def _seed_l3_failed(
    intended, *, device_id=SW_L3, routes=None, attempts=3, err="boom", reservation_id=RES_ID
):
    async with TestSessionLocal() as s:
        row = RouteAssignment(
            reservation_id=uuid.UUID(reservation_id),
            device_id=uuid.UUID(device_id),
            routes=routes if routes is not None else PINNED,
            status="FAILED",
            intended=intended,
            attempts=attempts,
            last_error=err,
        )
        s.add(row)
        await s.commit()
        await s.refresh(row)
        return row.id


async def _seed_state(frozen=False):
    async with TestSessionLocal() as s:
        s.add(ReservationWiringState(reservation_id=uuid.UUID(RES_ID), frozen=frozen))
        await s.commit()


async def _l3_row(row_id):
    async with TestSessionLocal() as s:
        return (
            await s.execute(select(RouteAssignment).where(RouteAssignment.id == row_id))
        ).scalar_one()


# --- direction-aware reattempt ---


async def test_build_direction_retry_reconfigures_pinned_set_and_ends_active():
    rid = await _seed_l3_failed("ACTIVE")
    execute_fn, calls = _recorder()
    with _patches(execute_fn):
        result = await reattempt_reservation(RES_ID, _db_session_factory())
    dests = {d for a, d in calls if a == "configure_route"}
    assert dests == {"10.0.0.0/24", "10.1.0.0/24"}, "the PINNED set, not the edited config"
    assert not any(d == "203.0.113.0/24" for _a, d in calls), "never re-derives from config"
    row = await _l3_row(rid)
    assert row.status == "ACTIVE"
    out = result["results"][0]
    assert out["outcome"] == "reconnected"
    assert out["layer"] == "l3"
    assert out["route_count"] == 2
    assert out["switch_device_id"] == SW_L3


async def test_release_direction_retry_removes_pinned_set_and_ends_released():
    rid = await _seed_l3_failed("RELEASED")
    execute_fn, calls = _recorder()
    with _patches(execute_fn):
        result = await reattempt_reservation(RES_ID, _db_session_factory())
    dests = {d for a, d in calls if a == "remove_route"}
    assert dests == {"10.0.0.0/24", "10.1.0.0/24"}
    row = await _l3_row(rid)
    assert row.status == "RELEASED"
    assert result["results"][0]["outcome"] == "released"


async def test_build_retry_repeat_failure_accumulates_attempts():
    rid = await _seed_l3_failed("ACTIVE", attempts=3)
    execute_fn, _calls = _recorder(fail={"configure_route"})
    with _patches(execute_fn):
        result = await reattempt_reservation(RES_ID, _db_session_factory())
    row = await _l3_row(rid)
    assert row.status == "FAILED"
    assert row.intended == "ACTIVE"
    assert row.attempts > 3, "attempts accumulate on repeat failure"
    assert result["results"][0]["outcome"] == "still_failed"


# --- frozen direction scoping ---


async def test_frozen_pure_build_l3_raises():
    await _seed_l3_failed("ACTIVE")
    await _seed_state(frozen=True)
    execute_fn, calls = _recorder()
    with _patches(execute_fn):
        with pytest.raises(WiringReservationFrozen):
            await reattempt_reservation(RES_ID, _db_session_factory())
    assert calls == [], "no driver call for a frozen pure-build L3 retry"


async def test_frozen_processes_release_reports_build_frozen():
    build_id = await _seed_l3_failed("ACTIVE", device_id=SW_L3)
    # A second SWITCH's release row on the same reservation (the ledger keys pins on
    # (reservation, device), so a build and a release live on distinct switches).
    release_id = await _seed_l3_failed("RELEASED", device_id=SW_L3_B)
    await _seed_state(frozen=True)
    execute_fn, calls = _recorder()
    with _patches(execute_fn):
        result = await reattempt_reservation(RES_ID, _db_session_factory())
    outcomes = {r["id"]: r["outcome"] for r in result["results"]}
    assert outcomes[str(build_id)] == "frozen"
    assert outcomes[str(release_id)] == "released"
    assert not any(a == "configure_route" for a, _d in calls), "build not driven while frozen"
    assert any(a == "remove_route" for a, _d in calls)


# --- no supersession for L3 (documented-away): a release still fires its driver removal ---


async def test_l3_release_is_not_superseded_by_another_reservation_on_same_switch():
    """Route pins are per-reservation and non-exclusive, so another reservation holding
    ACTIVE routes on the same switch does NOT supersede this reservation's stuck release:
    the removal must still run (removing THIS reservation's routes never touches theirs)."""
    other_res = str(uuid.uuid4())
    async with TestSessionLocal() as s:
        s.add(
            RouteAssignment(
                reservation_id=uuid.UUID(other_res),
                device_id=uuid.UUID(SW_L3),
                routes=PINNED,
                status="ACTIVE",
                intended="ACTIVE",
            )
        )
        await s.commit()
    rid = await _seed_l3_failed("RELEASED")
    execute_fn, calls = _recorder()
    with _patches(execute_fn):
        result = await reattempt_reservation(RES_ID, _db_session_factory())
    assert any(a == "remove_route" for a, _d in calls), "the release must still fire its driver"
    assert result["results"][0]["outcome"] == "released"
    row = await _l3_row(rid)
    assert row.status == "RELEASED"


# --- mixed L1 + L3 batch, per-layer labeling ---


async def test_tick_labels_layers_and_counts_l3(monkeypatch):
    monkeypatch.setattr("app.config.settings.wiring_retry_batch_size", 10)
    monkeypatch.setattr("app.config.settings.wiring_retry_max_attempts", 100)
    await _seed_l3_failed("ACTIVE", attempts=1)
    async with TestSessionLocal() as s:
        s.add(
            L1ConnectionAssignment(
                reservation_id=uuid.UUID(RES_ID),
                switch_device_id=uuid.UUID(SW_L3),
                port_a="a1",
                port_b="a2",
                status="FAILED",
                intended="ACTIVE",
                attempts=1,
                last_error="boom",
            )
        )
        await s.commit()

    execute_fn, _calls = _recorder()
    with _patches(execute_fn):
        stats = await run_wiring_retry_tick(_db_session_factory())

    assert stats["rows_due"] == 2
    assert stats["l1_rows_retried"] == 1
    assert stats["l3_rows_retried"] == 1
    assert stats["rows_retried"] == 2
    assert stats["reconnected"] == 2
