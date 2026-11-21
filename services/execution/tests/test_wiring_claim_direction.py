"""The drive claim compares the row's direction (issue #1496).

A wiring retry pass plans each row's direction from rows it read earlier, then claims
the row immediately before that row's own driver call, which can be minutes later. If
another pass parks the row in between (intended RELEASED, the stale-build settlement
of #491), the claim used to match anyway (id, FAILED, unclaimed) and the planning pass
connected, added, or provisioned a row whose intent was gone. The claim now also
requires the row's `intended` to equal the direction its call site drives, in the same
UPDATE: ACTIVE for connect_ports, add_to_vlan, and an L3 provision; RELEASED for
disconnect_ports, remove_from_vlan, and an L3 deprovision.

The space is 3 layers times 2 directions times the row's 2 intended values. Covered
here against in-memory SQLite:

  - the claim itself, every cell: a matching direction stamps and holds the row; a
    mismatched one changes nothing on the row (status, intended, attempts, last_error,
    claimed_until), joins `wrong_direction` and not `lost`, and leaves the row
    claimable by another pass in its true direction (conservation);
  - the two refusals told apart: a row another pass holds, or one no longer FAILED, is
    `lost` whatever its direction; a held row reached again in the other direction;
  - every cell through the real per-row applies (`_apply_one_port_action`,
    `_apply_one_vlan_action`) and the L3 per-switch apply (`_apply_l3_adjacency`),
    with a stub driver: a mismatch makes no driver call, no execution run, and no
    ledger write, and the same apply then drives the row in its true direction;
  - the issue's reproduction through the retry service at all three layers: a pass
    revalidates a build, a second real retry pass parks the row (intent gone) between
    that revalidation and the first pass's claim, and the first pass makes no build
    call and reads the row back `still_failed`; the next pass releases it;
  - what the manual endpoint and the tick report for such a row: `still_failed`, not
    `in_progress`.

The two-connection Postgres proof of the same interleaving is
`test_a_park_between_a_pass_plan_and_its_claim_refuses_the_build` in
tests/test_wiring_retry_claim_race_live_pg.py.
"""

import uuid
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from app.database import Base
from app.models.execution_run import ExecutionRun
from app.models.l1_connection_assignment import L1ConnectionAssignment
from app.models.l2_port_assignment import L2PortAssignment
from app.models.route_assignment import RouteAssignment
from app.models.vlan_assignment import VlanAssignment
from app.services.nats_consumer import WIRING_STALE_BUILD_REASON
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


async def _seed(layer: str, intended: str, *, attempts=2, last_error="driver timeout"):
    """One FAILED unclaimed row of `layer` intending `intended`; returns its id."""
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
            )
        s.add(row)
        await s.commit()
        return row.id


async def _state(layer: str, row_id):
    model = MODEL[layer]
    async with TestSessionLocal() as s:
        row = (await s.execute(select(model).where(model.id == row_id))).scalar_one()
        return (row.status, row.intended, row.attempts, row.last_error, row.claimed_until)


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
    row_id = await _seed(layer, row_intended)
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
    assert await _state(layer, row_id) == before, "a refused claim writes nothing"

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
        row_id = await _seed(layer, direction)
        flip_to = row_intended
    else:
        row_id = await _seed(layer, row_intended)
        flip_to = None
    claims = WiringRowClaims()

    calls = await _apply_one(layer, direction, row_id, claims, flip_to=flip_to)

    if direction == row_intended:
        assert action in calls, f"a matching direction drives {action}, got {calls}"
        assert (await _state(layer, row_id))[0] == DRIVEN_STATUS[direction]
        assert claims.wrong_direction == set()
        return

    assert calls == [], f"a mismatched claim makes no driver call, got {calls}"
    assert await _execution_runs() == 0, "and writes no execution run"
    assert await _state(layer, row_id) == ("FAILED", row_intended, 2, "driver timeout", None), (
        "and no ledger write: status, intended, attempts, last_error, claimed_until"
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
