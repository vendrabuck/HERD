"""Consumer tests for reservation.wiring_changed (ADR 0007, issue #345 P3b phase 3).

Drives handle_wiring_changed / handle_reservation_event through the same in-memory
SQLite + mocked-sandbox pattern the phase-1 L1 tests use, and asserts the ordering
decision (Decision 4), release-before-build apply (Decision 4/6), verbatim-hop and
unresolvable handling (Decision 5), the frozen no-op (Decision 7), and the
upstream-vs-driver error split (Decision 7).
"""

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from app.database import Base
from app.models.l1_connection_assignment import L1ConnectionAssignment
from app.models.reservation_wiring_state import ReservationWiringState
from app.services.nats_consumer import (
    WIRING_NOT_SIMPLE_CHAIN_REASON,
    WIRING_UNRESOLVABLE_REASON,
    TransientUpstreamError,
    handle_reservation_event,
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

    def _get():
        return _Ctx()

    return _get


SWITCH_ID = str(uuid.uuid4())
SWITCH2_ID = str(uuid.uuid4())
DRIVER_ID = str(uuid.uuid4())
RES_ID = str(uuid.uuid4())
DUT1 = str(uuid.uuid4())
DUT2 = str(uuid.uuid4())
DUT3 = str(uuid.uuid4())
DUT4 = str(uuid.uuid4())
GONE_ID = str(uuid.uuid4())


def _switch_data(switch_id: str, name: str) -> dict:
    return {
        "id": switch_id,
        "name": name,
        "template_id": "tmpl-1",
        "driver_id": DRIVER_ID,
        "driver_sha256": "sha256abc",
        "driver_filename": "driver.zip",
        "connection_type": "Layer 1 Switch",
        "field_data": {"ip": "10.0.0.1"},
    }


SWITCH_DATA = _switch_data(SWITCH_ID, "L1-Switch")
SWITCH2_DATA = _switch_data(SWITCH2_ID, "L1-Switch-2")
DUT_DATA = {"id": DUT1, "name": "server", "connection_type": "Server", "field_data": {}}
TEMPLATE_DATA = {
    "id": "tmpl-1",
    "name": "L1 Template",
    "sections": [{"name": "Net", "fields": [{"key": "ip", "type": "string"}]}],
}
SUCCESS_RESULT = {"success": True, "output": {"result": True}, "error": None, "duration_ms": 5}


def _wire(device_a, port_a, device_b, port_b, phys=None, edge_key=None):
    return {
        "device_a_id": device_a,
        "port_a": port_a,
        "device_b_id": device_b,
        "port_b": port_b,
        "layer": "L1",
        "physical_connection_id": phys,
        "edge_key": edge_key,
    }


# A cross-connect between DUT1 and DUT2 through the switch is two recorded hops whose
# switch-side ports 0/0/1 and 0/0/2 form the pair.
PAIR_12 = [
    _wire(DUT1, "eth0", SWITCH_ID, "0/0/1", str(uuid.uuid4())),
    _wire(SWITCH_ID, "0/0/2", DUT2, "eth0", str(uuid.uuid4())),
]
# A second cross-connect (DUT1 to DUT3) on switch ports 0/0/1 and 0/0/3.
PAIR_13 = [
    _wire(DUT1, "eth1", SWITCH_ID, "0/0/1", str(uuid.uuid4())),
    _wire(SWITCH_ID, "0/0/3", DUT3, "eth0", str(uuid.uuid4())),
]
# A distinct cross-connect on ports 0/0/3 and 0/0/4 of the SAME switch: combined with
# PAIR_12 this is two edges through one switch. Ambiguous when the hops carry NO
# edge_key (legacy rows); exactly pairable when each hop carries its edge_key.
PAIR_34 = [
    _wire(DUT3, "eth0", SWITCH_ID, "0/0/3", str(uuid.uuid4())),
    _wire(SWITCH_ID, "0/0/4", DUT2, "eth1", str(uuid.uuid4())),
]
# The same two same-switch cross-connects, each hop tagged with its canvas edge_key
# (issue #345, PR #367). Grouping by edge_key makes the pairing exact regardless of
# wire order, even though both edges traverse SWITCH.
PAIR_12_E1 = [
    _wire(DUT1, "eth0", SWITCH_ID, "0/0/1", str(uuid.uuid4()), edge_key="edge-1"),
    _wire(SWITCH_ID, "0/0/2", DUT2, "eth0", str(uuid.uuid4()), edge_key="edge-1"),
]
PAIR_34_E2 = [
    _wire(DUT3, "eth0", SWITCH_ID, "0/0/3", str(uuid.uuid4()), edge_key="edge-2"),
    _wire(SWITCH_ID, "0/0/4", DUT4, "eth0", str(uuid.uuid4()), edge_key="edge-2"),
]
# A cross-connect on a SECOND switch (SWITCH2): a separate component from PAIR_12, so
# both are recoverable when applied together.
PAIR_SW2 = [
    _wire(DUT3, "eth0", SWITCH2_ID, "1/0/1", str(uuid.uuid4())),
    _wire(SWITCH2_ID, "1/0/2", DUT4, "eth0", str(uuid.uuid4())),
]
# A multi-switch chain DUT1 - SWITCH - SWITCH2 - DUT2: three hops that must yield one
# cross-connect per switch, (0/0/1, 0/0/2) and (1/0/1, 1/0/2), from any wire order.
CHAIN_SW1_SW2 = [
    _wire(DUT1, "eth0", SWITCH_ID, "0/0/1", str(uuid.uuid4())),
    _wire(SWITCH_ID, "0/0/2", SWITCH2_ID, "1/0/1", str(uuid.uuid4())),
    _wire(SWITCH2_ID, "1/0/2", DUT2, "eth0", str(uuid.uuid4())),
]
# A degree-3 branch at one switch (a Y): not a simple chain.
Y_BRANCH = [
    _wire(DUT1, "eth0", SWITCH_ID, "0/0/1", str(uuid.uuid4())),
    _wire(SWITCH_ID, "0/0/2", DUT2, "eth0", str(uuid.uuid4())),
    _wire(SWITCH_ID, "0/0/3", DUT3, "eth0", str(uuid.uuid4())),
]


def _device_lookup(**overrides):
    table = {
        SWITCH_ID: SWITCH_DATA,
        SWITCH2_ID: SWITCH2_DATA,
        DUT1: DUT_DATA,
        DUT2: DUT_DATA,
        DUT3: DUT_DATA,
        DUT4: DUT_DATA,
    }
    table.update(overrides)

    async def _fetch(device_id, client=None):
        return table.get(str(device_id))

    return _fetch


def _sandbox_recorder(fail_pairs=None, fail_actions=None):
    """Return (execute_fn, calls). calls records (action, port_a, port_b) in order.

    fail_pairs: set of frozenset({port_a, port_b}) whose port op fails.
    fail_actions: set of actions ("login"/"connect_ports"/...) that fail wholesale.
    """
    fail_pairs = fail_pairs or set()
    fail_actions = fail_actions or set()
    calls = []

    def execute_fn(driver_path, action, context, **kwargs):
        mk = kwargs.get("method_kwargs") or {}
        pa, pb = mk.get("port_a"), mk.get("port_b")
        calls.append((action, pa, pb))
        if action in fail_actions:
            return {"success": False, "output": None, "error": "boom", "duration_ms": 1}
        if pa is not None and frozenset({pa, pb}) in fail_pairs:
            return {"success": False, "output": None, "error": "boom", "duration_ms": 1}
        return SUCCESS_RESULT

    return execute_fn, calls


def _patches(execute_fn, device_fetch, fork_wires=None, fork_raises=None):
    ps = [
        patch("app.services.nats_consumer._fetch_device", new=AsyncMock(side_effect=device_fetch)),
        patch(
            "app.services.nats_consumer._fetch_template", new=AsyncMock(return_value=TEMPLATE_DATA)
        ),
        patch("app.services.driver_loader.load_driver", new=AsyncMock(return_value="/tmp/driver")),
        patch("app.services.driver_sandbox.execute_driver_method", side_effect=execute_fn),
    ]
    if fork_raises is not None:
        ps.append(
            patch(
                "app.services.nats_consumer._fetch_fork_intended_wires",
                new=AsyncMock(side_effect=fork_raises),
            )
        )
    else:
        ps.append(
            patch(
                "app.services.nats_consumer._fetch_fork_intended_wires",
                new=AsyncMock(return_value=fork_wires or []),
            )
        )
    return ps


async def _seed_state(last_applied=None, frozen=False):
    async with TestSessionLocal() as s:
        s.add(
            ReservationWiringState(
                reservation_id=uuid.UUID(RES_ID),
                last_applied_fork_version=last_applied,
                frozen=frozen,
            )
        )
        await s.commit()


async def _seed_active(port_a, port_b):
    async with TestSessionLocal() as s:
        s.add(
            L1ConnectionAssignment(
                reservation_id=uuid.UUID(RES_ID),
                switch_device_id=uuid.UUID(SWITCH_ID),
                port_a=port_a,
                port_b=port_b,
                status="ACTIVE",
            )
        )
        await s.commit()


async def _assignments(status=None):
    async with TestSessionLocal() as s:
        rows = list((await s.execute(select(L1ConnectionAssignment))).scalars().all())
    return [r for r in rows if status is None or r.status == status]


async def _last_applied():
    async with TestSessionLocal() as s:
        row = (
            await s.execute(
                select(ReservationWiringState).where(
                    ReservationWiringState.reservation_id == uuid.UUID(RES_ID)
                )
            )
        ).scalar_one_or_none()
    return row.last_applied_fork_version if row else None


def _event(fork_version, released, built):
    return {
        "event": "reservation.wiring_changed",
        "reservation_id": RES_ID,
        "fork_version": fork_version,
        "released": released,
        "built": built,
    }


async def _run(event, execute_fn, device_fetch=None, fork_wires=None, fork_raises=None):
    device_fetch = device_fetch or _device_lookup()
    ps = _patches(execute_fn, device_fetch, fork_wires=fork_wires, fork_raises=fork_raises)
    for p in ps:
        p.start()
    try:
        await handle_reservation_event(event, _db_session_factory())
    finally:
        for p in ps:
            p.stop()


# --- Ordering (Decision 4) ---------------------------------------------------


@pytest.mark.asyncio
async def test_contiguous_delta_apply_builds_pair():
    """version == last_applied + 1 with a delta applies the carried built pair."""
    await _seed_state(last_applied=0)
    execute_fn, calls = _sandbox_recorder()
    await _run(_event(1, released=[], built=PAIR_12), execute_fn)

    active = await _assignments("ACTIVE")
    assert len(active) == 1
    assert (active[0].port_a, active[0].port_b) == ("0/0/1", "0/0/2")
    assert ("connect_ports", "0/0/1", "0/0/2") in calls
    assert await _last_applied() == 1


@pytest.mark.asyncio
async def test_stale_version_is_noop():
    """version <= last_applied is a stale/duplicate delivery: no driver call, no change."""
    await _seed_state(last_applied=5)
    execute_fn, calls = _sandbox_recorder()
    await _run(_event(3, released=[], built=PAIR_12), execute_fn)

    assert calls == []
    assert await _assignments() == []
    assert await _last_applied() == 5


@pytest.mark.asyncio
async def test_replay_after_success_is_noop():
    """Redelivering the same version after a successful apply does not re-drive."""
    await _seed_state(last_applied=0)
    execute_fn, calls = _sandbox_recorder()
    await _run(_event(1, released=[], built=PAIR_12), execute_fn)
    first = len(calls)
    assert first > 0
    # Replay the identical event.
    await _run(_event(1, released=[], built=PAIR_12), execute_fn)
    assert len(calls) == first  # stale skip: no second driver call
    assert len(await _assignments("ACTIVE")) == 1


@pytest.mark.asyncio
async def test_gap_triggers_full_reconcile():
    """version > last_applied + 1 fetches the full intended set and converges."""
    await _seed_state(last_applied=0)
    execute_fn, calls = _sandbox_recorder()
    await _run(_event(5, released=[], built=[]), execute_fn, fork_wires=PAIR_12)

    active = await _assignments("ACTIVE")
    assert len(active) == 1
    assert (active[0].port_a, active[0].port_b) == ("0/0/1", "0/0/2")
    assert ("connect_ports", "0/0/1", "0/0/2") in calls
    assert await _last_applied() == 5


@pytest.mark.asyncio
async def test_missing_state_is_gap_then_stamped():
    """No wiring_state row: full-reconcile (gap by definition), then stamp the version."""
    execute_fn, calls = _sandbox_recorder()
    await _run(_event(3, released=[], built=PAIR_12), execute_fn, fork_wires=PAIR_12)

    assert len(await _assignments("ACTIVE")) == 1
    assert ("connect_ports", "0/0/1", "0/0/2") in calls
    assert await _last_applied() == 3


@pytest.mark.asyncio
async def test_heal_at_last_applied_plus_one_takes_full_reconcile():
    """REQUIRED (ADR 0007 Decision 2): a delta-less heal at exactly last_applied + 1
    must take the full-reconcile path and apply the missed save, never the empty
    carried delta. The intended set has a pair not yet ACTIVE; a wrong contiguous
    path would connect nothing."""
    await _seed_state(last_applied=0)
    execute_fn, calls = _sandbox_recorder()
    # released/built absent (None) => heal marker; version 1 == last_applied + 1.
    await _run(_event(1, released=None, built=None), execute_fn, fork_wires=PAIR_12)

    active = await _assignments("ACTIVE")
    assert len(active) == 1, "heal must full-reconcile and build the missed pair"
    assert (active[0].port_a, active[0].port_b) == ("0/0/1", "0/0/2")
    assert ("connect_ports", "0/0/1", "0/0/2") in calls
    assert await _last_applied() == 1


# --- Frozen guard (Decision 7) ----------------------------------------------


@pytest.mark.asyncio
async def test_frozen_reservation_is_noop_zero_driver_calls():
    """A wiring_changed for a frozen reservation acks with zero driver calls."""
    await _seed_state(last_applied=0, frozen=True)
    execute_fn, calls = _sandbox_recorder()
    await _run(_event(9, released=[], built=PAIR_12), execute_fn, fork_wires=PAIR_12)

    assert calls == []
    assert await _assignments() == []
    # Frozen no-op does not advance the marker.
    assert await _last_applied() == 0


# --- Release-before-build (Decision 4/6) ------------------------------------


@pytest.mark.asyncio
async def test_move_a_wire_releases_before_builds():
    """A moved cable (0/0/1-0/0/2 to 0/0/1-0/0/3) disconnects the old pair before it
    connects the new one, so the shared port 0/0/1 is freed before the re-claim."""
    await _seed_state(last_applied=0)
    await _seed_active("0/0/1", "0/0/2")
    execute_fn, calls = _sandbox_recorder()
    await _run(_event(1, released=PAIR_12, built=PAIR_13), execute_fn)

    port_calls = [c for c in calls if c[0] in ("connect_ports", "disconnect_ports")]
    dis_idx = next(i for i, c in enumerate(port_calls) if c[0] == "disconnect_ports")
    con_idx = next(i for i, c in enumerate(port_calls) if c[0] == "connect_ports")
    assert dis_idx < con_idx, "release must precede build"

    active = await _assignments("ACTIVE")
    released = await _assignments("RELEASED")
    assert len(active) == 1 and (active[0].port_a, active[0].port_b) == ("0/0/1", "0/0/3")
    assert len(released) == 1 and (released[0].port_a, released[0].port_b) == ("0/0/1", "0/0/2")
    assert await _last_applied() == 1


@pytest.mark.asyncio
async def test_release_of_absent_pair_is_noop():
    """A released pair with no ACTIVE row is an idempotent no-op: no disconnect call."""
    await _seed_state(last_applied=0)
    execute_fn, calls = _sandbox_recorder()
    await _run(_event(1, released=PAIR_12, built=[]), execute_fn)

    assert [c for c in calls if c[0] == "disconnect_ports"] == []
    assert await _assignments() == []
    assert await _last_applied() == 1


# --- Per-connection failure (Decision 6) ------------------------------------


@pytest.mark.asyncio
async def test_per_connection_failure_does_not_abort_siblings():
    """One failing build lands FAILED; the sibling on another switch still builds; the
    version advances. Two switches keep the two cross-connects independently pairable."""
    await _seed_state(last_applied=0)
    # PAIR_12 on SWITCH (fails), PAIR_SW2 on SWITCH2 (succeeds).
    built = PAIR_12 + PAIR_SW2
    execute_fn, _calls = _sandbox_recorder(fail_pairs={frozenset({"0/0/1", "0/0/2"})})
    await _run(_event(1, released=[], built=built), execute_fn)

    active = await _assignments("ACTIVE")
    failed = await _assignments("FAILED")
    assert len(active) == 1 and (active[0].port_a, active[0].port_b) == ("1/0/1", "1/0/2")
    assert len(failed) == 1 and (failed[0].port_a, failed[0].port_b) == ("0/0/1", "0/0/2")
    assert failed[0].attempts >= 1 and failed[0].last_error
    # The version was processed, so the marker advances despite the FAILED row.
    assert await _last_applied() == 1


# --- Chain-walk pairing (order-independent) ---------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("seed", [0, 1, 2, 3, 4])
async def test_multi_switch_chain_pairs_both_switches(seed):
    """A multi-switch chain DUT-SW1-SW2-DUT yields one cross-connect per switch,
    correct under any wire order (chain walk is order-independent)."""
    import random

    wires = list(CHAIN_SW1_SW2)
    random.Random(seed).shuffle(wires)

    await _seed_state(last_applied=0)
    execute_fn, calls = _sandbox_recorder()
    await _run(_event(1, released=[], built=wires), execute_fn)

    active = await _assignments("ACTIVE")
    pairs = {(str(r.switch_device_id), r.port_a, r.port_b) for r in active}
    assert pairs == {
        (SWITCH_ID, "0/0/1", "0/0/2"),
        (SWITCH2_ID, "1/0/1", "1/0/2"),
    }, f"chain mispaired under seed {seed}: {pairs}"
    assert await _assignments("FAILED") == []
    assert ("connect_ports", "0/0/1", "0/0/2") in calls
    assert ("connect_ports", "1/0/1", "1/0/2") in calls
    assert await _last_applied() == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("seed", [0, 1, 2, 3, 4])
async def test_two_edges_one_switch_keyed_pairs_correctly(seed):
    """Two cross-connects on the SAME switch, each hop carrying its canvas edge_key
    (PR #367), pair EXACTLY under any wire order: grouping by edge_key first makes each
    edge's chain unambiguous even though both traverse the one switch."""
    import random

    wires = PAIR_12_E1 + PAIR_34_E2
    random.Random(seed).shuffle(wires)

    await _seed_state(last_applied=0)
    execute_fn, calls = _sandbox_recorder()
    await _run(_event(1, released=[], built=wires), execute_fn)

    active = await _assignments("ACTIVE")
    pairs = {(str(r.switch_device_id), r.port_a, r.port_b) for r in active}
    assert pairs == {
        (SWITCH_ID, "0/0/1", "0/0/2"),
        (SWITCH_ID, "0/0/3", "0/0/4"),
    }, f"edge_key grouping mispaired under seed {seed}: {pairs}"
    assert await _assignments("FAILED") == []
    assert ("connect_ports", "0/0/1", "0/0/2") in calls
    assert ("connect_ports", "0/0/3", "0/0/4") in calls
    assert await _last_applied() == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("seed", [0, 1, 2, 3, 4])
async def test_two_edges_one_switch_null_edge_key_fails_safe(seed):
    """The legacy posture: the same two same-switch cross-connects with NO edge_key
    (pre-migration rows) are ambiguous once flattened (the pairing lived in the canvas
    edge). Chain walk over the NULL remainder refuses to guess: every hop lands FAILED
    with the pinned reason and no driver port op fires. Positional pairing would
    silently mis-cross-connect the ports here."""
    import random

    wires = PAIR_12 + PAIR_34
    random.Random(seed).shuffle(wires)

    await _seed_state(last_applied=0)
    execute_fn, calls = _sandbox_recorder()
    await _run(_event(1, released=[], built=wires), execute_fn)

    assert await _assignments("ACTIVE") == []
    failed = await _assignments("FAILED")
    assert len(failed) == 4, f"expected all 4 hops FAILED under seed {seed}"
    assert all(r.last_error == WIRING_NOT_SIMPLE_CHAIN_REASON for r in failed)
    # Fail-safe: no physical cross-connect attempted for an unrecoverable pairing.
    assert [c for c in calls if c[0] in ("connect_ports", "disconnect_ports")] == []
    assert await _last_applied() == 1


@pytest.mark.asyncio
async def test_mixed_keyed_and_null_edge_keys():
    """A mixed delta: a keyed same-switch edge is paired exactly by its group, and the
    NULL remainder (a lone cross-connect here) pairs via the fallback. Both land ACTIVE."""
    await _seed_state(last_applied=0)
    execute_fn, _calls = _sandbox_recorder()
    # PAIR_12_E1 carries edge-1; PAIR_34 carries no edge_key. Both on SWITCH.
    await _run(_event(1, released=[], built=PAIR_12_E1 + PAIR_34), execute_fn)

    active = await _assignments("ACTIVE")
    pairs = {(str(r.switch_device_id), r.port_a, r.port_b) for r in active}
    assert pairs == {(SWITCH_ID, "0/0/1", "0/0/2"), (SWITCH_ID, "0/0/3", "0/0/4")}
    assert await _assignments("FAILED") == []
    assert await _last_applied() == 1


@pytest.mark.asyncio
async def test_branch_is_not_a_simple_chain_fails_safe():
    """A degree-3 branch at a switch (a Y) is not a simple chain: every hop lands
    FAILED with the pinned reason rather than a guessed pairing."""
    await _seed_state(last_applied=0)
    execute_fn, calls = _sandbox_recorder()
    await _run(_event(1, released=[], built=Y_BRANCH), execute_fn)

    assert await _assignments("ACTIVE") == []
    failed = await _assignments("FAILED")
    assert len(failed) == 3
    assert all(r.last_error == WIRING_NOT_SIMPLE_CHAIN_REASON for r in failed)
    assert [c for c in calls if c[0] in ("connect_ports", "disconnect_ports")] == []
    assert await _last_applied() == 1


@pytest.mark.asyncio
async def test_two_edges_two_switches_both_pair():
    """Two cross-connects on DIFFERENT switches are separate components: both pair and
    build, no ambiguity."""
    await _seed_state(last_applied=0)
    execute_fn, _calls = _sandbox_recorder()
    await _run(_event(1, released=[], built=PAIR_12 + PAIR_SW2), execute_fn)

    active = await _assignments("ACTIVE")
    pairs = {(str(r.switch_device_id), r.port_a, r.port_b) for r in active}
    assert pairs == {(SWITCH_ID, "0/0/1", "0/0/2"), (SWITCH2_ID, "1/0/1", "1/0/2")}
    assert await _assignments("FAILED") == []
    assert await _last_applied() == 1


@pytest.mark.asyncio
async def test_driver_failure_acks_and_does_not_raise():
    """A driver failure lands a FAILED row and returns (acks); it never raises/NAKs."""
    await _seed_state(last_applied=0)
    execute_fn, _calls = _sandbox_recorder(fail_pairs={frozenset({"0/0/1", "0/0/2"})})
    # Should not raise.
    await _run(_event(1, released=[], built=PAIR_12), execute_fn)
    assert len(await _assignments("FAILED")) == 1
    assert await _last_applied() == 1


@pytest.mark.asyncio
async def test_driver_result_failure_lands_failed_row():
    """A driver that RETURNS success=False without raising (sandbox transport success is
    True, the driver's own payload reports failure) is a per-connection failure on the
    wiring path too: a FAILED row, never a phantom ACTIVE (#345 phase-4 live-gate fix,
    _run_driver_with_retry gates on the driver result payload, not just transport)."""
    await _seed_state(last_applied=0)

    def execute_fn(driver_path, action, context, **kwargs):
        if action == "connect_ports":
            return {
                "success": True,
                "output": {"success": False, "error": "mock injected failure on connect_ports"},
                "error": None,
                "duration_ms": 1,
            }
        return SUCCESS_RESULT

    await _run(_event(1, released=[], built=PAIR_12), execute_fn)
    assert await _assignments("ACTIVE") == []
    failed = await _assignments("FAILED")
    assert len(failed) == 1
    assert failed[0].last_error == "mock injected failure on connect_ports"
    assert await _last_applied() == 1


# --- Verbatim-hop / unresolvable (Decision 5) -------------------------------


@pytest.mark.asyncio
async def test_unresolvable_hop_lands_failed_with_pinned_reason():
    """A built hop whose switch endpoint is gone (404) is a FAILED row, never re-routed."""
    await _seed_state(last_applied=0)
    # A wire whose switch side (GONE_ID) no longer resolves in inventory.
    gone_wire = _wire(DUT1, "eth0", GONE_ID, "0/0/9", str(uuid.uuid4()))
    execute_fn, calls = _sandbox_recorder()
    fetch = _device_lookup(**{GONE_ID: None})
    await _run(_event(1, released=[], built=[gone_wire]), execute_fn, device_fetch=fetch)

    failed = await _assignments("FAILED")
    assert len(failed) == 1
    assert failed[0].last_error == WIRING_UNRESOLVABLE_REASON
    # Verbatim apply, no re-route: no driver port op fired for the stranded hop.
    assert [c for c in calls if c[0] in ("connect_ports", "disconnect_ports")] == []
    assert await _last_applied() == 1


# --- Error split (Decision 7) -----------------------------------------------


@pytest.mark.asyncio
async def test_upstream_error_raises_for_nak():
    """A transient UPSTREAM failure while resolving raises (NAK) and does not stamp."""
    await _seed_state(last_applied=0)
    execute_fn, _calls = _sandbox_recorder()

    async def boom(device_id, client=None):
        raise TransientUpstreamError("upstream 503")

    with pytest.raises(TransientUpstreamError):
        await _run(_event(1, released=[], built=PAIR_12), execute_fn, device_fetch=boom)

    # NAK path: the version marker must not have advanced.
    assert await _last_applied() == 0


@pytest.mark.asyncio
async def test_upstream_error_on_fork_fetch_raises():
    """A 5xx from cabling's fork GET during a gap reconcile raises (NAK)."""
    await _seed_state(last_applied=0)
    execute_fn, _calls = _sandbox_recorder()

    async def boom(*a, **kw):
        raise TransientUpstreamError("cabling 503")

    with pytest.raises(TransientUpstreamError):
        await _run(_event(5, released=[], built=[]), execute_fn, fork_raises=boom)
    assert await _last_applied() == 0
