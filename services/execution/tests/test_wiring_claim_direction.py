"""The drive claim compares the row's direction (issue #1496).

A wiring retry pass plans each row's direction from rows it read earlier, then claims
the row immediately before that row's own driver call, which can be minutes later. If
another pass parks the row in between (intended RELEASED, the stale-build settlement
of #491), the claim used to match anyway (id, FAILED, unclaimed) and the planning pass
connected, added, or provisioned a row whose intent was gone. The claim now also
requires the direction its call site drives to be admitted, in the same UPDATE
(`direction_admits`): the row's `intended` equals it (ACTIVE for connect_ports,
add_to_vlan, and an L3 provision; RELEASED for disconnect_ports, remove_from_vlan, and
an L3 deprovision), or the direction is RELEASED and the reservation is frozen. A
retry pass's failure write by row id on a row it does not hold, which runs before any
per-row claim, takes the same compare.

The space is 3 layers times 2 directions times the row's 2 intended values. Covered
here against in-memory SQLite:

  - the claim itself, every cell: a matching direction stamps and holds the row; a
    mismatched one changes no value on the row (status, intended, attempts,
    last_error, and an expired claimed_until it was seeded with), joins
    `wrong_direction` and not `lost`, and leaves the row claimable by another pass in
    its true direction (conservation);
  - the two refusals told apart: a row another pass holds, or one no longer FAILED, is
    `lost` whatever its direction; a held row reached again in the other direction;
  - every cell through the real per-row applies (`_apply_one_port_action`,
    `_apply_one_vlan_action`) and the L3 per-switch apply (`_apply_l3_adjacency`),
    with a stub driver: a mismatch makes no driver call, no execution run, and no
    value change, and the same apply then drives the row in its true direction. The L1
    and L2 cells pre-arrange the row's direction and call the per-row apply directly:
    they test the predicate, not a reachable interleaving (in the retry pass the
    release gate reads the row right before the claim, with only database time
    between). The L3 cells move `intended` inside the driver load, a real point in the
    window between the deprovision gate and the claim;
  - the issue's reproduction through the retry service at all three layers: a pass
    revalidates a build, a second real retry pass parks the row (intent gone) between
    that revalidation and the first pass's claim, and the first pass makes no build
    call and reads the row back `still_failed`; the next pass releases it;
  - every failure write a pass makes before its claim (the switch or template read,
    the driver load, the login, L2's create_vlan and re-allocation, L3's drive gate),
    with the row parked in between: the park survives each one, and an unparked row
    still gets that failure recorded;
  - a frozen reservation: a release is admitted over a row that intends ACTIVE (at the
    claim and at the failure write, every layer), a build is not, and an unfrozen
    reservation still refuses that release;
  - what the manual endpoint and the tick report for such a row: `still_failed`, not
    `in_progress`.

The two-connection Postgres proof of the same interleaving is
`test_a_park_between_a_pass_plan_and_its_claim_refuses_the_build` in
tests/test_wiring_retry_claim_race_live_pg.py.
"""

import uuid
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from app.database import Base
from app.models.execution_run import ExecutionRun
from app.models.l1_connection_assignment import L1ConnectionAssignment
from app.models.l2_port_assignment import L2PortAssignment
from app.models.reservation_wiring_state import ReservationWiringState
from app.models.route_assignment import RouteAssignment
from app.models.vlan_assignment import VlanAssignment
from app.services.nats_consumer import WIRING_STALE_BUILD_REASON, WIRING_UNRESOLVABLE_REASON
from app.services.wiring_claim import WiringRowClaims
from sqlalchemy import func, select
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


RES_ID = uuid.uuid4()
SWITCH_ID = uuid.uuid4()
PORT_A, PORT_B = "0/0/1", "0/0/2"
L2_PORT = "0/0/9"
VLAN = 100
ROUTE = {"destination": "10.9.0.0/24", "next_hop": "10.9.0.1"}

LAYERS = ("l1", "l2", "l3")
DIRECTIONS = ("ACTIVE", "RELEASED")
MODEL = {"l1": L1ConnectionAssignment, "l2": L2PortAssignment, "l3": RouteAssignment}
CONNECTION_TYPE = {"l1": "Layer 1 Switch", "l2": "Layer 2 Switch", "l3": "Layer 3 Switch"}
# The row action each layer's call site drives, per direction.
ROW_ACTION = {
    ("l1", "ACTIVE"): "connect_ports",
    ("l1", "RELEASED"): "disconnect_ports",
    ("l2", "ACTIVE"): "add_to_vlan",
    ("l2", "RELEASED"): "remove_from_vlan",
    ("l3", "ACTIVE"): "configure_route",
    ("l3", "RELEASED"): "remove_route",
}
# The status a successful drive in each direction records.
DRIVEN_STATUS = {"ACTIVE": "ACTIVE", "RELEASED": "RELEASED"}
SUCCESS = {"success": True, "output": {}, "error": None, "duration_ms": 1}
TEMPLATE = {"id": "t", "name": "t", "sections": []}


def _switch(layer: str) -> dict:
    return {
        "id": str(SWITCH_ID),
        "name": layer,
        "template_id": "t",
        "driver_id": str(uuid.uuid4()),
        "driver_sha256": "sha",
        "driver_filename": "d.zip",
        "connection_type": CONNECTION_TYPE[layer],
        "field_data": {},
    }


async def _seed(
    layer: str, intended: str, *, attempts=2, last_error="driver timeout", claimed_until=None
):
    """One FAILED row of `layer` intending `intended`, unclaimed (`claimed_until` None
    or a past stamp); returns its id."""
    async with TestSessionLocal() as s:
        if layer == "l1":
            row = L1ConnectionAssignment(
                reservation_id=RES_ID,
                switch_device_id=SWITCH_ID,
                port_a=PORT_A,
                port_b=PORT_B,
                status="FAILED",
                intended=intended,
                attempts=attempts,
                last_error=last_error,
                claimed_until=claimed_until,
            )
        elif layer == "l2":
            va = VlanAssignment(
                reservation_id=RES_ID,
                fabric_id=uuid.uuid4(),
                vlan_id=VLAN,
                switch_device_ids=[str(SWITCH_ID)],
                defined_switch_ids=[str(SWITCH_ID)],
                status="ACTIVE",
            )
            s.add(va)
            await s.flush()
            row = L2PortAssignment(
                reservation_id=RES_ID,
                vlan_assignment_id=va.id,
                switch_device_id=SWITCH_ID,
                port=L2_PORT,
                status="FAILED",
                intended=intended,
                attempts=attempts,
                last_error=last_error,
                claimed_until=claimed_until,
            )
        else:
            row = RouteAssignment(
                reservation_id=RES_ID,
                device_id=SWITCH_ID,
                routes=[ROUTE],
                status="FAILED",
                intended=intended,
                attempts=attempts,
                last_error=last_error,
                claimed_until=claimed_until,
            )
        s.add(row)
        await s.commit()
        return row.id


async def _state(layer: str, row_id):
    model = MODEL[layer]
    async with TestSessionLocal() as s:
        row = (await s.execute(select(model).where(model.id == row_id))).scalar_one()
        return (row.status, row.intended, row.attempts, row.last_error, row.claimed_until)


def _expired():
    """A claim stamp in the past: unclaimed, but not null, so a refusal that wrote the
    stamp (to null or to anything) is visible."""
    return datetime.now(timezone.utc) - timedelta(minutes=10)


async def _freeze() -> None:
    async with TestSessionLocal() as s:
        s.add(ReservationWiringState(reservation_id=RES_ID, frozen=True))
        await s.commit()


def _naive(stamp):
    """SQLite drops the time zone a `timestamptz` keeps; compare the instant only."""
    return stamp.replace(tzinfo=None)


async def _set_intended(layer: str, row_id, intended: str) -> None:
    """Another writer moved the row's direction (attempts and reason untouched)."""
    async with TestSessionLocal() as s:
        row = await s.get(MODEL[layer], row_id)
        row.intended = intended
        await s.commit()


async def _execution_runs() -> int:
    async with TestSessionLocal() as s:
        return (await s.execute(select(func.count()).select_from(ExecutionRun))).scalar_one()


# --- The claim itself: all 12 cells ------------------------------------------


@pytest.mark.parametrize("row_intended", DIRECTIONS)
@pytest.mark.parametrize("direction", DIRECTIONS)
@pytest.mark.parametrize("layer", LAYERS)
async def test_the_claim_holds_a_row_only_in_the_direction_it_intends(
    layer, direction, row_intended
):
    """The claim's predicate, cell by cell: `direction` is what the call site would
    drive, `row_intended` what the row intends when the claim runs."""
    model = MODEL[layer]
    row_id = await _seed(layer, row_intended, claimed_until=_expired())
    before = await _state(layer, row_id)
    claims = WiringRowClaims()

    async with TestSessionLocal() as s:
        granted = await claims.claim(s, model, row_id, intended=direction)

    if direction == row_intended:
        assert granted is True
        assert set(claims.held) == {row_id}
        assert claims.wrong_direction == set() and claims.lost == set()
        after = await _state(layer, row_id)
        assert after[:4] == before[:4]
        assert _naive(after[4]) == _naive(claims.held[row_id]), "the stamp the claim wrote"
        return

    assert granted is False
    assert claims.held == {}
    assert claims.wrong_direction == {row_id}
    assert claims.lost == set(), "a direction refusal is not another holder's work"
    assert await _state(layer, row_id) == before, (
        "a refused claim changes no value, the expired stamp included"
    )

    # Conservation: the refusal neither stamps nor strands the row; a pass driving it
    # in its true direction claims it at once.
    true_pass = WiringRowClaims()
    async with TestSessionLocal() as s:
        assert await true_pass.claim(s, model, row_id, intended=row_intended) is True
    assert set(true_pass.held) == {row_id}


@pytest.mark.parametrize("direction", DIRECTIONS)
@pytest.mark.parametrize("layer", LAYERS)
async def test_a_row_another_pass_holds_is_lost_in_either_direction(layer, direction):
    """The two refusals are told apart by the claim's own statement: a row another
    pass holds matches nothing at all, so it is `lost` (read back `in_progress`) even
    when its direction is also the other one."""
    model = MODEL[layer]
    row_id = await _seed(layer, "ACTIVE")
    holder = WiringRowClaims()
    async with TestSessionLocal() as s:
        assert await holder.claim(s, model, row_id, intended="ACTIVE") is True
    before = await _state(layer, row_id)

    late = WiringRowClaims()
    async with TestSessionLocal() as s:
        assert await late.claim(s, model, row_id, intended=direction) is False
    assert late.lost == {row_id}
    assert late.wrong_direction == set()
    assert await _state(layer, row_id) == before


@pytest.mark.parametrize("direction", DIRECTIONS)
@pytest.mark.parametrize("layer", LAYERS)
async def test_a_row_no_longer_failed_is_lost_in_either_direction(layer, direction):
    model = MODEL[layer]
    row_id = await _seed(layer, "RELEASED")
    async with TestSessionLocal() as s:
        row = await s.get(model, row_id)
        row.status = "RELEASED"
        await s.commit()

    claims = WiringRowClaims()
    async with TestSessionLocal() as s:
        assert await claims.claim(s, model, row_id, intended=direction) is False
    assert claims.lost == {row_id}
    assert claims.wrong_direction == set()


async def test_a_held_row_reached_in_the_other_direction_is_refused_without_a_write():
    """A pass that holds a row in one direction is answered from that direction: the
    same direction keeps its claim (WIRE-CLAIM-6), the other is refused into
    `wrong_direction` and the stamp stays the one the first claim wrote."""
    row_id = await _seed("l1", "RELEASED")
    claims = WiringRowClaims()
    async with TestSessionLocal() as s:
        assert await claims.claim(s, L1ConnectionAssignment, row_id, intended="RELEASED")
        stamp = claims.held[row_id]
        assert await claims.claim(s, L1ConnectionAssignment, row_id, intended="RELEASED")
        assert await claims.claim(s, L1ConnectionAssignment, row_id, intended="ACTIVE") is False
    assert claims.wrong_direction == {row_id}
    assert claims.lost == set()
    assert claims.held == {row_id: stamp}
    assert _naive((await _state("l1", row_id))[4]) == _naive(stamp)


async def test_the_claim_rejects_a_direction_that_is_not_one():
    claims = WiringRowClaims()
    async with TestSessionLocal() as s:
        with pytest.raises(ValueError, match="ACTIVE or RELEASED"):
            await claims.claim(s, L1ConnectionAssignment, None, intended="active")


async def test_the_reconcile_path_claims_nothing_in_either_direction():
    """`row_id` None (not a retry channel) is granted as before, whatever the direction."""
    claims = WiringRowClaims()
    async with TestSessionLocal() as s:
        for direction in DIRECTIONS:
            assert await claims.claim(s, RouteAssignment, None, intended=direction) is True
    assert claims.held == {} and claims.wrong_direction == set()


# --- Every cell through the real applies -------------------------------------


def _recorder():
    calls: list[str] = []

    def execute_fn(driver_path, action, context, **kwargs):
        calls.append(action)
        return SUCCESS

    return execute_fn, calls


async def _apply_one(layer: str, direction: str, row_id, claims, *, flip_to=None):
    """Drive `row_id` in `direction` through its layer's real apply with a stub driver
    that always succeeds; returns the driver actions called.

    L1 and L2 go through the per-row applies, whose own claim is the call site. L3 goes
    through `_apply_l3_adjacency` (a provision or a deprovision); `flip_to`, when given,
    moves the pin's `intended` inside the driver load, after the deprovision's
    `route_needs_remove` gate read and before the claim, the window the claim guards.
    """
    from app.services.nats_consumer import (
        _apply_l3_adjacency,
        _apply_one_port_action,
        _apply_one_vlan_action,
        _FetchContext,
    )

    execute_fn, calls = _recorder()
    action = ROW_ACTION[(layer, direction)]

    async def _load(*args, **kwargs):
        if flip_to is not None:
            await _set_intended(layer, row_id, flip_to)
        return "/tmp/d"

    with ExitStack() as stack:
        stack.enter_context(
            patch("app.services.driver_sandbox.execute_driver_method", side_effect=execute_fn)
        )
        if layer == "l1":
            async with TestSessionLocal() as s:
                await _apply_one_port_action(
                    s,
                    action,
                    "/tmp/d",
                    {},
                    set(),
                    SWITCH_ID,
                    uuid.uuid4(),
                    "sha",
                    RES_ID,
                    PORT_A,
                    PORT_B,
                    None,
                    retry_row_id=row_id,
                    claims=claims,
                )
        elif layer == "l2":
            async with TestSessionLocal() as s:
                va = (await s.get(L2PortAssignment, row_id)).vlan_assignment_id
                await _apply_one_vlan_action(
                    s,
                    action,
                    "/tmp/d",
                    {},
                    set(),
                    SWITCH_ID,
                    uuid.uuid4(),
                    "sha",
                    RES_ID,
                    L2_PORT,
                    VLAN,
                    va,
                    retry_row_id=row_id,
                    claims=claims,
                )
        else:
            stack.enter_context(
                patch(
                    "app.services.nats_consumer._fetch_device",
                    AsyncMock(return_value=_switch("l3")),
                )
            )
            stack.enter_context(
                patch(
                    "app.services.nats_consumer._fetch_template", AsyncMock(return_value=TEMPLATE)
                )
            )
            stack.enter_context(
                patch("app.services.driver_loader.load_driver", AsyncMock(side_effect=_load))
            )
            stack.enter_context(
                patch("app.services.driver_loader.get_driver_metadata", AsyncMock(return_value={}))
            )
            item = {"device_id": str(SWITCH_ID), "routes": [ROUTE]}
            async with httpx.AsyncClient() as client:
                await _apply_l3_adjacency(
                    str(RES_ID),
                    [item] if direction == "RELEASED" else [],
                    [item] if direction == "ACTIVE" else [],
                    _FetchContext(client),
                    _db_session_factory(),
                    retry_row_ids={str(SWITCH_ID): row_id},
                    claims=claims,
                )
    return calls


@pytest.mark.parametrize("row_intended", DIRECTIONS)
@pytest.mark.parametrize("direction", DIRECTIONS)
@pytest.mark.parametrize("layer", LAYERS)
async def test_an_apply_drives_a_row_only_in_the_direction_it_intends(
    layer, direction, row_intended
):
    """Each call site, each direction, each intent the row can have when the claim
    runs. An L3 pin is seeded in the planned direction (so a deprovision passes its
    gate) and moved to `row_intended` inside the driver load, before the claim."""
    action = ROW_ACTION[(layer, direction)]
    if layer == "l3":
        row_id = await _seed(layer, direction, claimed_until=_expired())
        flip_to = row_intended
    else:
        row_id = await _seed(layer, row_intended, claimed_until=_expired())
        flip_to = None
    _status, _intended, attempts, last_error, stamp = await _state(layer, row_id)
    claims = WiringRowClaims()

    calls = await _apply_one(layer, direction, row_id, claims, flip_to=flip_to)

    if direction == row_intended:
        assert action in calls, f"a matching direction drives {action}, got {calls}"
        assert (await _state(layer, row_id))[0] == DRIVEN_STATUS[direction]
        assert claims.wrong_direction == set()
        return

    assert calls == [], f"a mismatched claim makes no driver call, got {calls}"
    assert await _execution_runs() == 0, "and writes no execution run"
    assert await _state(layer, row_id) == ("FAILED", row_intended, attempts, last_error, stamp), (
        "and no value change: status, intended, attempts, last_error, the expired stamp"
    )
    assert claims.wrong_direction == {row_id}
    assert claims.lost == set()

    # Conservation: another pass drives the row in its true direction through the
    # same apply, and records it.
    true_calls = await _apply_one(layer, row_intended, row_id, WiringRowClaims())
    assert ROW_ACTION[(layer, row_intended)] in true_calls
    assert (await _state(layer, row_id))[0] == DRIVEN_STATUS[row_intended]


# --- The issue's reproduction through the retry service ----------------------


def _l1_wires() -> list[dict]:
    """The recorded hops of one edge: DUT to switch:PORT_A, switch:PORT_B to DUT."""
    edge = str(uuid.uuid4())
    return [
        {
            "device_a_id": str(uuid.uuid4()),
            "port_a": "eth0",
            "device_b_id": str(SWITCH_ID),
            "port_b": PORT_A,
            "layer": "L1",
            "physical_connection_id": None,
            "edge_key": edge,
        },
        {
            "device_a_id": str(SWITCH_ID),
            "port_a": PORT_B,
            "device_b_id": str(uuid.uuid4()),
            "port_b": "eth0",
            "layer": "L1",
            "physical_connection_id": None,
            "edge_key": edge,
        },
    ]


def _l2_wires() -> list[dict]:
    return [
        {
            "device_a_id": str(uuid.uuid4()),
            "port_a": "eth0",
            "device_b_id": str(SWITCH_ID),
            "port_b": L2_PORT,
            "layer": "L1",
            "physical_connection_id": None,
            "edge_key": None,
        }
    ]


async def _loaded(layer: str, row_id):
    model = MODEL[layer]
    async with TestSessionLocal() as s:
        return (await s.execute(select(model).where(model.id == row_id))).scalars().all()


def _reattempt(layer: str):
    from app.services.wiring_retry_service import (
        _reattempt_l2_rows,
        _reattempt_l3_rows,
        _reattempt_rows,
    )

    return {"l1": _reattempt_rows, "l2": _reattempt_l2_rows, "l3": _reattempt_l3_rows}[layer]


def _staged_park(layer: str, parker_rows: list, run_parker, stack: ExitStack):
    """Patch the reads of a retry pass so that a SECOND retry pass parks the row inside
    the first pass's driver load: after the first pass revalidated the build (the wire
    is still intended) and before it claims the row.

    The parking pass is a real retry pass over its own copy of the row, which finds the
    wire gone (the owner removed it) and parks it through `write_guard`
    (`park_stale_*_build`): intended RELEASED, attempts 0, the stale reason, the claim
    cleared. `run_parker(rows)` runs it; its outcomes are recorded in the returned dict.
    """
    resaved: dict = {}

    def _intent_present() -> bool:
        return "done" not in resaved

    async def _intent(reservation, client=None):
        if not _intent_present():
            return []
        return {"l1": _l1_wires, "l2": _l2_wires, "l3": list}[layer]()

    async def _device(device_id, client=None):
        if str(device_id) == str(SWITCH_ID):
            return _switch(layer)
        return {"id": str(device_id), "name": "dut", "connection_type": "Server", "field_data": {}}

    async def _adjacency(*args, **kwargs):
        return {str(SWITCH_ID): set()} if _intent_present() else {}

    async def _load(*args, **kwargs):
        if "done" not in resaved:
            resaved["done"] = True
            resaved["parker_outcomes"] = await run_parker(parker_rows)
        return "/tmp/d"

    for target, value in (
        ("app.services.nats_consumer._fetch_fork_intended_wires", AsyncMock(side_effect=_intent)),
        ("app.services.nats_consumer._fetch_device", AsyncMock(side_effect=_device)),
        ("app.services.nats_consumer._fetch_template", AsyncMock(return_value=TEMPLATE)),
        ("app.services.nats_consumer._derive_l3_adjacency", AsyncMock(side_effect=_adjacency)),
        ("app.services.nats_consumer._wired_ports_by_device", lambda wires: {}),
        ("app.services.driver_loader.load_driver", AsyncMock(side_effect=_load)),
        ("app.services.driver_loader.get_driver_metadata", AsyncMock(return_value={})),
    ):
        stack.enter_context(patch(target, value))
    return resaved


@pytest.mark.parametrize("layer", LAYERS)
async def test_a_build_planned_before_a_park_is_not_driven_over_it(layer):
    """The issue #1496 reproduction, at each layer. Pass P1 loads a FAILED build row
    and revalidates it (the wire is still intended). Inside P1's driver load, before
    P1 claims the row, pass P2 (the other retry channel) finds the wire gone and parks
    the row. P1 then reaches its claim: the row intends RELEASED, so P1 makes no
    connect, add, or configure call, writes nothing, and reads the row back
    `still_failed`, not `reconnected` and not `in_progress`. The next pass releases it.
    """
    row_id = await _seed(layer, "ACTIVE", attempts=3)
    p1_rows = await _loaded(layer, row_id)
    p2_rows = await _loaded(layer, row_id)
    p1_claims = WiringRowClaims()
    execute_fn, calls = _recorder()

    async def _run_p2(rows):
        return await _reattempt(layer)(rows, _db_session_factory(), WiringRowClaims(), set())

    with ExitStack() as stack:
        resaved = _staged_park(layer, p2_rows, _run_p2, stack)
        stack.enter_context(
            patch("app.services.driver_sandbox.execute_driver_method", side_effect=execute_fn)
        )
        outcomes = await _reattempt(layer)(p1_rows, _db_session_factory(), p1_claims)

    assert resaved.get("done"), "the park must have landed inside P1's drive"
    assert [o["outcome"] for o in resaved["parker_outcomes"]] == ["still_failed"]
    assert ROW_ACTION[(layer, "ACTIVE")] not in calls, (
        f"P1 built a row another pass parked: {calls}"
    )
    assert await _state(layer, row_id) == ("FAILED", "RELEASED", 0, WIRING_STALE_BUILD_REASON, None)
    assert [o["outcome"] for o in outcomes] == ["still_failed"]
    assert p1_claims.wrong_direction == {row_id}
    assert p1_claims.lost == set()

    # The parked row is release work for the next pass, which takes it.
    execute_fn, calls = _recorder()
    with ExitStack() as stack:
        _staged_park(layer, [], AsyncMock(return_value=[]), stack)
        stack.enter_context(
            patch("app.services.driver_sandbox.execute_driver_method", side_effect=execute_fn)
        )
        outcomes = await _reattempt(layer)(await _loaded(layer, row_id), _db_session_factory())
    assert ROW_ACTION[(layer, "RELEASED")] in calls
    assert [o["outcome"] for o in outcomes] == ["released"]


async def test_the_manual_endpoint_reports_a_direction_refusal_still_failed():
    """What the API answers for the reproduction: the internal retry endpoint (the
    manual channel the reservations proxy calls) reports the row `still_failed` with
    its true state, FAILED, attempts 0, the stale-build reason; the frontend already
    counts `still_failed`."""
    from app.main import app
    from app.routers import executions as ex_router

    row_id = await _seed("l1", "ACTIVE", attempts=3)
    p2_rows = await _loaded("l1", row_id)

    async def _run_p2(rows):
        return await _reattempt("l1")(rows, _db_session_factory(), WiringRowClaims(), set())

    execute_fn, calls = _recorder()
    with ExitStack() as stack:
        _staged_park("l1", p2_rows, _run_p2, stack)
        stack.enter_context(
            patch("app.services.driver_sandbox.execute_driver_method", side_effect=execute_fn)
        )
        stack.enter_context(
            patch.object(ex_router, "make_session_ctx_factory", _db_session_factory)
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                f"/internal/reservations/{RES_ID}/wiring/retry",
                headers={"X-Internal-Token": "test-token"},
            )

    assert resp.status_code == 200
    (result,) = resp.json()["results"]
    assert result["id"] == str(row_id)
    assert (result["outcome"], result["status"], result["attempts"], result["last_error"]) == (
        "still_failed",
        "FAILED",
        0,
        WIRING_STALE_BUILD_REASON,
    )
    assert "connect_ports" not in calls


async def test_the_tick_counts_a_direction_refusal_still_failed_not_in_progress():
    """The tick's summary for the reproduction: one row retried, `still_failed` 1,
    `in_progress` 0, nothing reconnected."""
    from app.services.wiring_retry_service import run_wiring_retry_tick

    row_id = await _seed("l1", "ACTIVE", attempts=3)
    p2_rows = await _loaded("l1", row_id)

    async def _run_p2(rows):
        return await _reattempt("l1")(rows, _db_session_factory(), WiringRowClaims(), set())

    execute_fn, calls = _recorder()
    with ExitStack() as stack:
        _staged_park("l1", p2_rows, _run_p2, stack)
        stack.enter_context(
            patch("app.services.driver_sandbox.execute_driver_method", side_effect=execute_fn)
        )
        stats = await run_wiring_retry_tick(_db_session_factory())

    assert (stats["l1_rows_retried"], stats["still_failed"], stats["in_progress"]) == (1, 1, 0)
    assert stats["reconnected"] == 0
    assert "connect_ports" not in calls


# --- A failure recorded before the claim does not undo a park -----------------
#
# Every failure write a retry pass makes on a row before that row's per-row claim. The
# planning pass P1 revalidated the build, so a park that lands after that point and
# before the write used to be overwritten: the write set intended back to ACTIVE.
# The sites, each the seam the park lands in here:
#
#   L1 `_apply_wiring_pairs`: the switch read, the template read, the driver load, the
#      login, in both directions.
#   L2 `_apply_l2_memberships`: the same four in both directions, and the create_vlan
#      definition pre-pass (adds); `_reattempt_l2_rows`: the re-allocation of a row
#      whose allocation is the nil placeholder.
#   L3 `_apply_l3_adjacency`: the switch read, the template read, the driver load (an
#      L3 login failure comes after the pin's claim); `_reattempt_l3_rows`: the drive
#      gate's refusal.
#
# The L1 and L2 switch reads are not seams for a BUILD-planned row: the pass's fetch
# context memoizes the switch from the revalidation that admitted the build, so the
# apply reads the same answer. A RELEASE-planned row has no revalidation, so its switch
# read can be fresh and its switch-not-found failure write is a pre-claim write too,
# guarded the same way. This matrix stages build rows parked mid-pass; the writer's
# compare in both directions is the grid in
# test_a_failure_write_on_a_row_the_pass_does_not_hold_compares_the_direction.

PRE_CLAIM_SITES = (
    ("l1", "template_missing"),
    ("l1", "load_failure"),
    ("l1", "login_failure"),
    ("l2", "template_missing"),
    ("l2", "load_failure"),
    ("l2", "login_failure"),
    ("l2", "create_vlan_failure"),
    ("l2", "reallocation_failure"),
    ("l3", "switch_missing"),
    ("l3", "template_missing"),
    ("l3", "load_failure"),
    ("l3", "gate_refusal"),
)
# The reason each site records on a row nobody parked (a prefix).
SITE_REASON = {
    "switch_missing": WIRING_UNRESOLVABLE_REASON,
    "template_missing": WIRING_UNRESOLVABLE_REASON,
    "load_failure": "driver load failed: RuntimeError",
    "login_failure": "driver login failed: auth refused",
    "create_vlan_failure": "create_vlan failed: boom",
    "reallocation_failure": f"{WIRING_UNRESOLVABLE_REASON}: no VLAN allocation",
    "gate_refusal": "gate refused",
}


async def _park(layer: str, row_id) -> None:
    """The stale-build park another retry pass makes through its write guard."""
    from app.services.l1_assignment_service import park_stale_l1_build
    from app.services.l2_membership_service import park_stale_l2_build
    from app.services.route_service import park_stale_route_build

    park = {"l1": park_stale_l1_build, "l2": park_stale_l2_build, "l3": park_stale_route_build}
    async with TestSessionLocal() as s:
        await park[layer](s, row_id, WIRING_STALE_BUILD_REASON)


async def _seed_for_site(layer: str, site: str):
    row_id = await _seed(layer, "ACTIVE", attempts=3)
    if site in ("create_vlan_failure", "reallocation_failure"):
        async with TestSessionLocal() as s:
            row = await s.get(L2PortAssignment, row_id)
            if site == "create_vlan_failure":
                va = await s.get(VlanAssignment, row.vlan_assignment_id)
                va.defined_switch_ids = []
            else:
                row.vlan_assignment_id = uuid.UUID(int=0)
            await s.commit()
    return row_id


def _stage_pre_claim_failure(layer: str, site: str, before_write, stack: ExitStack):
    """Patch a retry pass's reads so `site` fails, after awaiting `before_write()` at
    the seam (once): after the revalidation, before the failure write."""
    seen: dict = {}
    calls: list[str] = []

    async def _seam():
        if "done" not in seen:
            seen["done"] = True
            await before_write()

    async def _intent(reservation, client=None):
        if layer == "l1":
            return _l1_wires()
        if layer == "l2":
            return _l2_wires()
        gated = site == "gate_refusal"
        return SimpleNamespace(l3_routes={str(SWITCH_ID): [ROUTE]} if gated else {})

    async def _device(device_id, client=None):
        if str(device_id) == str(SWITCH_ID):
            if site == "switch_missing":
                await _seam()
                return None
            return _switch(layer)
        return {"id": str(device_id), "name": "dut", "connection_type": "Server", "field_data": {}}

    async def _template(*args, **kwargs):
        if site == "template_missing":
            await _seam()
            return None
        return TEMPLATE

    async def _load(*args, **kwargs):
        if site in ("load_failure", "login_failure"):
            await _seam()
        if site == "load_failure":
            raise RuntimeError("package store unreachable")
        return "/tmp/d"

    async def _define(*args, **kwargs):
        await _seam()
        return False, 1, "boom"

    async def _resolve(*args, **kwargs):
        await _seam()
        return {}

    async def _gate(*args, **kwargs):
        await _seam()
        return None, "gate refused: test"

    def execute_fn(driver_path, action, context, **kwargs):
        calls.append(action)
        if action == "login" and site == "login_failure":
            return {"success": False, "output": None, "error": "auth refused", "duration_ms": 1}
        return SUCCESS

    for target, value in (
        ("app.services.nats_consumer._fetch_fork_intended_wires", AsyncMock(side_effect=_intent)),
        ("app.services.nats_consumer._fetch_device", AsyncMock(side_effect=_device)),
        ("app.services.nats_consumer._fetch_template", AsyncMock(side_effect=_template)),
        (
            "app.services.nats_consumer._derive_l3_adjacency",
            AsyncMock(return_value={str(SWITCH_ID): set()}),
        ),
        ("app.services.nats_consumer._wired_ports_by_device", lambda wires: {}),
        ("app.services.nats_consumer._run_vlan_definition_op", AsyncMock(side_effect=_define)),
        ("app.services.nats_consumer._resolve_add_allocations", AsyncMock(side_effect=_resolve)),
        ("app.services.nats_consumer._gate_l3_drive_routes", AsyncMock(side_effect=_gate)),
        ("app.services.driver_loader.load_driver", AsyncMock(side_effect=_load)),
        ("app.services.driver_loader.get_driver_metadata", AsyncMock(return_value={})),
    ):
        stack.enter_context(patch(target, value))
    stack.enter_context(
        patch("app.services.driver_sandbox.execute_driver_method", side_effect=execute_fn)
    )
    return seen, calls


@pytest.mark.parametrize("variant", ["unparked", "parked", "parked_frozen"])
@pytest.mark.parametrize(("layer", "site"), PRE_CLAIM_SITES)
async def test_a_failure_recorded_before_the_claim_does_not_undo_a_park(layer, site, variant):
    """P1 loads a FAILED build row and revalidates it. At the site's seam, after that
    revalidation, another pass parks the row (intended RELEASED, attempts 0, the stale
    reason, claim cleared; `parked_frozen` freezes the reservation first, the teardown
    order). Then the site fails and P1 records the failure by row id, before any
    claim. The write's direction compare refuses it: the park survives with every
    value, P1 reads the row back `still_failed`, and the next tick releases it, on an
    active and a frozen reservation alike. Unparked, the failure is recorded as
    before: FAILED intended ACTIVE with the site's reason."""
    from app.services.wiring_retry_service import run_wiring_retry_tick

    row_id = await _seed_for_site(layer, site)
    p1_rows = await _loaded(layer, row_id)
    p1_claims = WiringRowClaims()

    async def _before_write():
        if variant == "parked_frozen":
            await _freeze()
        if variant != "unparked":
            await _park(layer, row_id)

    with ExitStack() as stack:
        seen, calls = _stage_pre_claim_failure(layer, site, _before_write, stack)
        outcomes = await _reattempt(layer)(p1_rows, _db_session_factory(), p1_claims)

    assert seen.get("done"), f"the {site} seam was never reached"
    assert ROW_ACTION[(layer, "ACTIVE")] not in calls
    assert [o["outcome"] for o in outcomes] == ["still_failed"]
    status, intended, attempts, last_error, stamp = await _state(layer, row_id)

    if variant == "unparked":
        assert (status, intended, stamp) == ("FAILED", "ACTIVE", None)
        assert last_error.startswith(SITE_REASON[site]), last_error
        assert attempts >= 3
        assert p1_claims.wrong_direction == set()
        return

    assert (status, intended, attempts, last_error, stamp) == (
        "FAILED",
        "RELEASED",
        0,
        WIRING_STALE_BUILD_REASON,
        None,
    ), f"P1's {site} failure undid the park"
    assert (p1_claims.wrong_direction, p1_claims.lost) == ({row_id}, set())

    if site == "reallocation_failure":
        # The stale-build park stands in here for the writer that really moves a nil
        # row's direction in this window: a frozen join's record-time park
        # (`record_l2_membership_active`), which reuses the reservation's FAILED row
        # for the (switch, port) and gives it the join's allocation. That path, and
        # its conservation, is the real case:
        # test_a_frozen_join_on_the_nil_row_survives_the_reallocation_failure. A
        # stale-build park leaves the nil allocation, so this case drives no release.
        return
    # Conservation: the parked row is release work, and the next tick takes it, frozen
    # or not (a release-direction row is never a frozen build).
    with ExitStack() as stack:
        _staged_park(layer, [], AsyncMock(return_value=[]), stack)
        execute_fn, release_calls = _recorder()
        stack.enter_context(
            patch("app.services.driver_sandbox.execute_driver_method", side_effect=execute_fn)
        )
        stats = await run_wiring_retry_tick(_db_session_factory())
    assert ROW_ACTION[(layer, "RELEASED")] in release_calls
    assert stats["released"] == 1
    assert (await _state(layer, row_id))[0] == "RELEASED"


async def test_a_teardown_park_survives_a_provision_load_failure_on_a_frozen_reservation():
    """The review's frozen trace. Tick T1 plans an L3 provision before the freeze.
    Inside T1's driver load the reservation ends: the wiring freezes and the teardown
    parks the FAILED intended-ACTIVE pin (TEARDOWN_PENDING_REMOVAL). T1's load then
    fails and its failure write by row id is refused for direction, so the pin stays
    parked, and the teardown's deprovision removes its routes. Without the write's
    direction compare, T1's write put the pin back to intended ACTIVE, the deprovision
    found nothing to remove, and no retry ever drove it (a frozen build)."""
    from app.services.nats_consumer import _apply_l3_adjacency, _FetchContext
    from app.services.route_service import TEARDOWN_PENDING_REMOVAL, park_stale_route_build

    row_id = await _seed("l3", "ACTIVE", attempts=1)
    t1_rows = await _loaded("l3", row_id)

    async def _teardown_park():
        await _freeze()
        async with TestSessionLocal() as s:
            await park_stale_route_build(s, row_id, TEARDOWN_PENDING_REMOVAL)

    t1_claims = WiringRowClaims()
    with ExitStack() as stack:
        seen, _calls = _stage_pre_claim_failure("l3", "load_failure", _teardown_park, stack)
        await _reattempt("l3")(t1_rows, _db_session_factory(), t1_claims)
    assert seen.get("done")
    assert await _state("l3", row_id) == ("FAILED", "RELEASED", 0, TEARDOWN_PENDING_REMOVAL, None)
    assert t1_claims.wrong_direction == {row_id}

    execute_fn, calls = _recorder()
    with ExitStack() as stack:
        _staged_park("l3", [], AsyncMock(return_value=[]), stack)
        stack.enter_context(
            patch("app.services.driver_sandbox.execute_driver_method", side_effect=execute_fn)
        )
        item = {"device_id": str(SWITCH_ID), "routes": [ROUTE]}
        async with httpx.AsyncClient() as client:
            await _apply_l3_adjacency(
                str(RES_ID), [item], [], _FetchContext(client), _db_session_factory()
            )
    assert calls == ["login", "remove_route", "logout"]
    assert (await _state("l3", row_id))[0] == "RELEASED"


# --- A frozen reservation's wiring only leaves --------------------------------


@pytest.mark.parametrize("row_intended", DIRECTIONS)
@pytest.mark.parametrize("direction", DIRECTIONS)
@pytest.mark.parametrize("layer", LAYERS)
async def test_on_a_frozen_reservation_a_release_is_claimed_whatever_the_row_intends(
    layer, direction, row_intended
):
    """The 12 cells again on a frozen reservation: a release is granted over a row
    that intends either direction; a build is granted only over a row that intends
    ACTIVE (the record-time freeze check, WIRE-LEDGER-4, still parks what it builds)."""
    await _freeze()
    model = MODEL[layer]
    row_id = await _seed(layer, row_intended, claimed_until=_expired())
    before = await _state(layer, row_id)
    claims = WiringRowClaims()
    async with TestSessionLocal() as s:
        granted = await claims.claim(s, model, row_id, intended=direction)

    if direction == "RELEASED" or row_intended == "ACTIVE":
        assert granted is True
        assert set(claims.held) == {row_id}
        return
    assert granted is False, "a build never gets the frozen exception"
    assert claims.wrong_direction == {row_id}
    assert await _state(layer, row_id) == before


@pytest.mark.parametrize("frozen", [True, False], ids=["frozen", "active"])
@pytest.mark.parametrize("layer", LAYERS)
async def test_a_release_planned_before_a_flip_to_active_settles_only_when_frozen(layer, frozen):
    """A release pass planned the row (it intended RELEASED); before the claim another
    writer records a build for it (FAILED intended ACTIVE). On a frozen reservation the
    release still claims and removes it, because no retry will build or drive that row
    again; on an active reservation it is refused, and the build channel owns the row.
    L1 and L2 pre-arrange the row and call the per-row apply (their window is database
    time only); L3 flips the pin inside the driver load."""
    if frozen:
        await _freeze()
    if layer == "l3":
        row_id = await _seed(layer, "RELEASED")
        flip_to = "ACTIVE"
    else:
        row_id = await _seed(layer, "ACTIVE")
        flip_to = None
    claims = WiringRowClaims()
    calls = await _apply_one(layer, "RELEASED", row_id, claims, flip_to=flip_to)

    if frozen:
        assert ROW_ACTION[(layer, "RELEASED")] in calls
        assert (await _state(layer, row_id))[0] == "RELEASED"
        assert claims.wrong_direction == set()
        return
    assert calls == []
    assert await _state(layer, row_id) == ("FAILED", "ACTIVE", 2, "driver timeout", None)
    assert claims.wrong_direction == {row_id}


@pytest.mark.parametrize("layer", LAYERS)
async def test_a_build_over_a_released_row_is_refused_on_a_frozen_reservation(layer):
    """The exception is the release direction's only: a build planned before the row
    was parked is refused on a frozen reservation as on an active one."""
    await _freeze()
    if layer == "l3":
        row_id = await _seed(layer, "ACTIVE")
        flip_to = "RELEASED"
    else:
        row_id = await _seed(layer, "RELEASED")
        flip_to = None
    claims = WiringRowClaims()
    calls = await _apply_one(layer, "ACTIVE", row_id, claims, flip_to=flip_to)
    assert calls == []
    assert (await _state(layer, row_id))[:2] == ("FAILED", "RELEASED")
    assert claims.wrong_direction == {row_id}


async def test_a_frozen_release_refused_by_a_flip_to_active_no_longer_strands_the_pin():
    """The review's reproduction. A frozen reservation's FAILED intended-RELEASED pin (a
    teardown removal that failed). The tick selects it and plans the deprovision. Inside
    the tick's driver load a writer that does not compare direction records a build
    failure for the pin by key (a reconcile provision that passed its frozen check
    before the freeze): FAILED intended ACTIVE. The tick's release claim is admitted on
    the frozen reservation, the routes are removed, and the pin is RELEASED. Refused,
    the pin would stay FAILED intended ACTIVE, which no later tick selects."""
    from app.services.route_service import record_route_failed
    from app.services.wiring_retry_service import run_wiring_retry_tick

    row_id = await _seed("l3", "RELEASED", attempts=1)
    await _freeze()
    flipped: dict = {}

    async def _load(*args, **kwargs):
        if "done" not in flipped:
            flipped["done"] = True
            async with TestSessionLocal() as s:
                await record_route_failed(
                    s, RES_ID, str(SWITCH_ID), [ROUTE], 1, "driver timeout", intended="ACTIVE"
                )
        return "/tmp/d"

    execute_fn, calls = _recorder()
    with ExitStack() as stack:
        _staged_park("l3", [], AsyncMock(return_value=[]), stack)
        stack.enter_context(
            patch("app.services.driver_loader.load_driver", AsyncMock(side_effect=_load))
        )
        stack.enter_context(
            patch("app.services.driver_sandbox.execute_driver_method", side_effect=execute_fn)
        )
        stats = await run_wiring_retry_tick(_db_session_factory())

    assert flipped.get("done")
    assert calls == ["login", "remove_route", "logout"]
    assert stats["released"] == 1
    assert (await _state("l3", row_id))[0] == "RELEASED"


async def _write_failure_by_row_id(layer: str, row_id, claims, intended: str):
    from app.services.l1_assignment_service import record_l1_failed
    from app.services.l2_membership_service import record_l2_failed
    from app.services.route_service import record_route_failed

    common = {"intended": intended, "row_id": row_id, "claims": claims}
    async with TestSessionLocal() as s:
        if layer == "l1":
            return await record_l1_failed(
                s, RES_ID, SWITCH_ID, PORT_A, PORT_B, 1, "again", **common
            )
        if layer == "l2":
            return await record_l2_failed(s, RES_ID, None, SWITCH_ID, L2_PORT, 1, "again", **common)
        return await record_route_failed(s, RES_ID, SWITCH_ID, None, 1, "again", **common)


@pytest.mark.parametrize("frozen", [False, True], ids=["active", "frozen"])
@pytest.mark.parametrize("row_intended", DIRECTIONS)
@pytest.mark.parametrize("direction", DIRECTIONS)
@pytest.mark.parametrize("layer", LAYERS)
async def test_a_failure_write_on_a_row_the_pass_does_not_hold_compares_the_direction(
    layer, direction, row_intended, frozen
):
    """The row-id failure writer's own compare, every cell: on a row the pass does not
    hold the write lands only where the direction is admitted (the row intends it, or
    it is a release on a frozen reservation); a refused write changes no value (the
    expired stamp included) and joins `wrong_direction`."""
    if frozen:
        await _freeze()
    row_id = await _seed(layer, row_intended, claimed_until=_expired())
    before = await _state(layer, row_id)
    claims = WiringRowClaims()

    written = await _write_failure_by_row_id(layer, row_id, claims, direction)

    if direction == row_intended or (frozen and direction == "RELEASED"):
        assert written is not None
        assert await _state(layer, row_id) == ("FAILED", direction, 3, "again", None)
        assert claims.wrong_direction == set()
        return
    assert written is None
    assert await _state(layer, row_id) == before
    assert (claims.wrong_direction, claims.lost) == ({row_id}, set())


@pytest.mark.parametrize("layer", LAYERS)
async def test_a_failure_write_on_a_row_the_pass_holds_takes_no_direction_compare(layer):
    """A holder's failure write is not compared for direction: its claim compared it.
    Here a writer that never claims (a stale park) moved the row while the holder drove
    it and cleared the holder's stamp, so WIRE-CLAIM-8 refuses the write as `lost`,
    never as `wrong_direction`, exactly as before #1496."""
    model = MODEL[layer]
    row_id = await _seed(layer, "ACTIVE")
    holder = WiringRowClaims()
    async with TestSessionLocal() as s:
        assert await holder.claim(s, model, row_id, intended="ACTIVE") is True
    await _park(layer, row_id)

    assert await _write_failure_by_row_id(layer, row_id, holder, "ACTIVE") is None
    assert (holder.lost, holder.wrong_direction) == ({row_id}, set())
    assert (await _state(layer, row_id))[1] == "RELEASED"


@pytest.mark.parametrize("layer", LAYERS)
async def test_a_refused_failure_write_is_rolled_back_in_the_callers_session(layer):
    """What leaves a refused failure write's values as they were is its rollback: the
    write's UPDATE runs (each value under `CASE WHEN admitted`), and the writer rolls
    the caller's session back before it returns. Pinned here: after the refusal the
    caller's session has no transaction open, and the row reads unchanged from that
    session and from a second one."""
    row_id = await _seed(layer, "RELEASED", claimed_until=_expired())
    before = await _state(layer, row_id)
    model = MODEL[layer]
    from app.services.l1_assignment_service import record_l1_failed
    from app.services.l2_membership_service import record_l2_failed
    from app.services.route_service import record_route_failed

    common = {"intended": "ACTIVE", "row_id": row_id, "claims": WiringRowClaims()}
    async with TestSessionLocal() as s:
        if layer == "l1":
            written = await record_l1_failed(
                s, RES_ID, SWITCH_ID, PORT_A, PORT_B, 1, "again", **common
            )
        elif layer == "l2":
            written = await record_l2_failed(
                s, RES_ID, None, SWITCH_ID, L2_PORT, 1, "again", **common
            )
        else:
            written = await record_route_failed(s, RES_ID, SWITCH_ID, None, 1, "again", **common)
        assert written is None
        assert not s.in_transaction(), "the refused write left the caller's session open"
        row = (await s.execute(select(model).where(model.id == row_id))).scalar_one()
        same_session = (row.status, row.intended, row.attempts, row.last_error, row.claimed_until)
    assert same_session == before
    assert await _state(layer, row_id) == before


async def test_a_frozen_join_on_the_nil_row_survives_the_reallocation_failure():
    """The real path behind the L2 re-allocation site. P1 loads a FAILED build row whose
    allocation is the nil placeholder and revalidates it. While P1 re-allocates, the
    reservation freezes and a join for the same (switch, port) succeeds:
    `record_l2_membership_active`'s record-time freeze branch reuses the reservation's
    FAILED row for the port (this nil row), gives it the join's allocation, and parks it
    FAILED intended RELEASED (FROZEN_JOIN_PENDING_REMOVAL). P1's re-allocation then
    fails and its build failure write by row id is refused for direction, so the park
    stands, and the next tick removes the membership from the join's real VLAN."""
    from app.services.l2_membership_service import (
        FROZEN_JOIN_PENDING_REMOVAL,
        record_l2_membership_active,
    )
    from app.services.wiring_retry_service import run_wiring_retry_tick

    row_id = await _seed_for_site("l2", "reallocation_failure")
    async with TestSessionLocal() as s:
        join_va = (
            await s.execute(
                select(VlanAssignment.id).where(VlanAssignment.reservation_id == RES_ID)
            )
        ).scalar_one()
    p1_rows = await _loaded("l2", row_id)
    p1_claims = WiringRowClaims()

    async def _frozen_join():
        await _freeze()
        async with TestSessionLocal() as s:
            await record_l2_membership_active(s, RES_ID, join_va, SWITCH_ID, L2_PORT)

    with ExitStack() as stack:
        seen, calls = _stage_pre_claim_failure("l2", "reallocation_failure", _frozen_join, stack)
        outcomes = await _reattempt("l2")(p1_rows, _db_session_factory(), p1_claims)

    assert seen.get("done")
    assert "add_to_vlan" not in calls
    assert [o["outcome"] for o in outcomes] == ["still_failed"]
    async with TestSessionLocal() as s:
        row = await s.get(L2PortAssignment, row_id)
        parked = (row.status, row.intended, row.last_error, row.vlan_assignment_id)
    assert parked == ("FAILED", "RELEASED", FROZEN_JOIN_PENDING_REMOVAL, join_va)
    assert (p1_claims.wrong_direction, p1_claims.lost) == ({row_id}, set())

    removed: list = []

    def execute_fn(driver_path, action, context, **kwargs):
        if action == "remove_from_vlan":
            removed.append((kwargs.get("method_kwargs") or {}).get("vlan_id"))
        return SUCCESS

    with ExitStack() as stack:
        _staged_park("l2", [], AsyncMock(return_value=[]), stack)
        stack.enter_context(
            patch("app.services.driver_sandbox.execute_driver_method", side_effect=execute_fn)
        )
        stats = await run_wiring_retry_tick(_db_session_factory())
    assert removed == [VLAN]
    assert stats["released"] == 1
    assert (await _state("l2", row_id))[0] == "RELEASED"


# --- Known limit: a frozen release drives what it planned ---------------------
#
# On a frozen reservation a release is admitted whatever the row intends, and it drives
# the content the pass PLANNED. For L1 (one wire per row) that settles the row
# correctly. For L3 (a pin's route set) and L2 (a membership's allocation), content a
# writer that does not compare direction added in the window (a newer route, a
# re-pointed allocation) is neither driven nor kept in the record. Main behaves the
# same (its claim and writers compare nothing). This is the stale-content window issue
# #1481 tracks; these tests pin today's behaviour so a fix there shows up here.

R2 = {"destination": "10.77.0.0/24", "next_hop": "10.77.0.1"}


async def _key_based_provision_failure(routes):
    """A reconcile provision that passed its frozen check before the freeze, configured
    `routes`, and failed: the key-based write, which compares no direction."""
    from app.services.route_service import record_route_failed

    async with TestSessionLocal() as s:
        await record_route_failed(
            s, RES_ID, str(SWITCH_ID), routes, 1, "driver timeout", intended="ACTIVE"
        )


async def test_known_limit_a_frozen_l3_release_removes_only_the_routes_it_planned():
    """Known limit (#1481). A frozen reservation's pin is FAILED intended RELEASED with
    routes [R1]. The tick selects it and plans the deprovision of [R1]. Inside the
    tick's driver load a key-based provision failure records [R2, R1] intended ACTIVE.
    The tick's release claim is admitted (frozen), it removes R1 only, and the pin is
    recorded RELEASED with [R2, R1]: R2 stays on the switch under a RELEASED pin."""
    from app.services.wiring_retry_service import run_wiring_retry_tick

    row_id = await _seed("l3", "RELEASED", attempts=1)
    await _freeze()
    flipped: dict = {}

    async def _load(*args, **kwargs):
        if "done" not in flipped:
            flipped["done"] = True
            await _key_based_provision_failure([R2, ROUTE])
        return "/tmp/d"

    removed: list = []

    def execute_fn(driver_path, action, context, **kwargs):
        if action == "remove_route":
            removed.append((kwargs.get("method_kwargs") or {}).get("destination"))
        return SUCCESS

    with ExitStack() as stack:
        _staged_park("l3", [], AsyncMock(return_value=[]), stack)
        stack.enter_context(
            patch("app.services.driver_loader.load_driver", AsyncMock(side_effect=_load))
        )
        stack.enter_context(
            patch("app.services.driver_sandbox.execute_driver_method", side_effect=execute_fn)
        )
        await run_wiring_retry_tick(_db_session_factory())

    assert flipped.get("done")
    assert removed == [ROUTE["destination"]], "only the planned route is removed"
    async with TestSessionLocal() as s:
        row = await s.get(RouteAssignment, row_id)
        final = (row.status, sorted(r["destination"] for r in row.routes))
    assert final == ("RELEASED", sorted([R2["destination"], ROUTE["destination"]]))


async def test_known_limit_an_admitted_frozen_release_failure_records_only_its_routes():
    """Known limit (#1481), the failure-write form. Same frozen pin and the same
    key-based write of [R2, R1] intended ACTIVE; the release pass then fails before its
    claim (for example its driver load) and records its failure by row id with the
    routes it planned, [R1]. The write is admitted (a release on a frozen reservation),
    so the pin is FAILED intended RELEASED with [R1] only: R2 is gone from the record,
    and the next release removes R1 alone."""
    from app.services.route_service import record_route_failed

    row_id = await _seed("l3", "RELEASED", attempts=1)
    await _freeze()
    await _key_based_provision_failure([R2, ROUTE])
    async with TestSessionLocal() as s:
        written = await record_route_failed(
            s,
            RES_ID,
            str(SWITCH_ID),
            [ROUTE],
            1,
            "driver load failed: RuntimeError",
            intended="RELEASED",
            row_id=row_id,
            claims=WiringRowClaims(),
        )
    assert written is not None
    async with TestSessionLocal() as s:
        row = await s.get(RouteAssignment, row_id)
        final = (row.status, row.intended, [r["destination"] for r in row.routes])
    assert final == ("FAILED", "RELEASED", [ROUTE["destination"]])
