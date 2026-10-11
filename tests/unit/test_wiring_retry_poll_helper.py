"""Unit tests for the integration suite's wiring-retry poll (issue #1158).

`tests/integration/_wiring_retry.py` backs the retry pollers of the L1, L2, L3, and
ledger-teardown integration tests. Those tests need a running stack and run only in
the advisory integration job, so the poll's decision is pinned here, stack-free: a
scripted client answers `POST .../wiring/retry` and `GET .../wiring-status`, and the
module's clock and sleep are replaced, so no test waits.

What the poll must do:

- return the retry row that carries the wanted outcome, and nothing else from a
  retry answer (`in_progress`, `still_failed`, and friends mean "ask again");
- when a retry answer names NO row for the identity (a 200 without it, or the 409 a
  frozen reservation gets) and the caller passed the id of the row it saw FAILED,
  return that row from wiring-status if it is in the settled status the outcome
  stands for, matched by id;
- never accept a row that is still FAILED, a row in the other settled status, an id
  wiring-status does not list, or anything on the strength of a 5xx;
- stop for good, with None, once the retry reports the caller's own row
  `superseded` (settled with no driver call).

Regression note: the two "tick converged first" sequences below (a 200 that no
longer names the row, and the frozen 409) are what the background retry tick
converging a row before the manual call looks like. The poller these tests replace
read only the retry answer, so it returned None for both after its whole timeout,
and the integration test failed on a correct stack.
"""

import pytest

from tests.integration import _wiring_retry as wr

ROW = "11111111-1111-1111-1111-111111111111"
OTHER = "22222222-2222-2222-2222-222222222222"


class FakeLoop:
    def __init__(self, clock):
        self._clock = clock

    def time(self) -> float:
        return self._clock.now


class FakeAsyncio:
    """Stands in for the helper module's `asyncio`: sleeping only moves the clock."""

    def __init__(self):
        self.now = 0.0
        self.slept: list[float] = []

    def get_event_loop(self) -> FakeLoop:
        return FakeLoop(self)

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    fake = FakeAsyncio()
    monkeypatch.setattr(wr, "asyncio", fake)
    return fake


class Resp:
    def __init__(self, status_code, body=None):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


class ScriptedStack:
    """Answers come from a script per endpoint; the last answer repeats."""

    def __init__(self, retries, statuses):
        self.retries = list(retries)
        self.statuses = list(statuses)
        self.retry_calls = 0
        self.status_calls = 0

    async def post(self, url):
        assert url == "/reservations/r1/wiring/retry", url
        answer = self.retries[min(self.retry_calls, len(self.retries) - 1)]
        self.retry_calls += 1
        return answer

    async def get(self, url):
        assert url == "/reservations/r1/wiring-status", url
        answer = self.statuses[min(self.status_calls, len(self.statuses) - 1)]
        self.status_calls += 1
        return answer


def retry(*rows):
    return Resp(200, {"reservation_id": "r1", "results": list(rows)})


def l2(outcome, status, row_id=ROW, port="1"):
    return {"id": row_id, "layer": "l2", "port": port, "outcome": outcome, "status": status}


def wiring(*conns):
    return Resp(200, {"connections": list(conns), "frozen": False})


def conn(status, row_id=ROW, layer="l2", port="1"):
    return {"id": row_id, "layer": layer, "port": port, "status": status}


def is_l2_port_1(row):
    return row.get("layer") == "l2" and row.get("port") == "1"


FROZEN_409 = Resp(409, {"detail": "Reservation wiring is frozen; retry is not allowed"})

RETRY_ROW = "retry-row"
STATUS_ROW = "status-row"

# name: (retry answers, wiring-status answers, wanted outcome, failed_row_id, expected)
SCENARIOS = {
    "manual retry converges": (
        [retry(l2("reconnected", "ACTIVE"))],
        [wiring()],
        "reconnected",
        ROW,
        RETRY_ROW,
    ),
    "in_progress, then the manual retry converges": (
        [retry(l2("in_progress", "FAILED")), retry(l2("reconnected", "ACTIVE"))],
        [wiring(conn("FAILED"))],
        "reconnected",
        ROW,
        RETRY_ROW,
    ),
    "tick converged first on an ACTIVE reservation (200, row not named)": (
        [retry()],
        [wiring(conn("ACTIVE"))],
        "reconnected",
        ROW,
        STATUS_ROW,
    ),
    "tick converged first after an in_progress answer": (
        [retry(l2("in_progress", "FAILED")), retry()],
        [wiring(conn("ACTIVE"))],
        "reconnected",
        ROW,
        STATUS_ROW,
    ),
    "tick converged first on a terminal reservation (frozen 409)": (
        [FROZEN_409],
        [wiring(conn("RELEASED"))],
        "released",
        ROW,
        STATUS_ROW,
    ),
    "tick converged this port while the retry still names another port": (
        [retry(l2("still_failed", "FAILED", row_id=OTHER, port="2"))],
        [wiring(conn("RELEASED"), conn("FAILED", row_id=OTHER, port="2"))],
        "released",
        ROW,
        STATUS_ROW,
    ),
    "wiring-status answers once the tick has finished": (
        [retry()],
        [wiring(conn("FAILED")), wiring(conn("FAILED")), wiring(conn("ACTIVE"))],
        "reconnected",
        ROW,
        STATUS_ROW,
    ),
    "row genuinely still FAILED and named still_failed": (
        [retry(l2("still_failed", "FAILED"))],
        [wiring(conn("ACTIVE"))],
        "reconnected",
        ROW,
        None,
    ),
    "row FAILED on wiring-status though the retry does not name it": (
        [FROZEN_409],
        [wiring(conn("FAILED"))],
        "released",
        ROW,
        None,
    ),
    "row never existed: no such id on wiring-status": (
        [retry()],
        [wiring(conn("ACTIVE", row_id=OTHER))],
        "reconnected",
        ROW,
        None,
    ),
    "another port is ACTIVE, ours is in neither answer": (
        [retry()],
        [wiring(conn("ACTIVE", row_id=OTHER, port="2"))],
        "reconnected",
        ROW,
        None,
    ),
    "wrong settled state: wanted reconnected, row RELEASED": (
        [retry()],
        [wiring(conn("RELEASED"))],
        "reconnected",
        ROW,
        None,
    ),
    "wrong settled state: wanted released, row ACTIVE": (
        [FROZEN_409],
        [wiring(conn("ACTIVE"))],
        "released",
        ROW,
        None,
    ),
    "no failed_row_id: the fallback is off": (
        [retry()],
        [wiring(conn("ACTIVE"))],
        "reconnected",
        None,
        None,
    ),
    "still_failed wanted and reported": (
        [retry(l2("still_failed", "FAILED"))],
        [wiring(conn("FAILED"))],
        "still_failed",
        None,
        RETRY_ROW,
    ),
    "still_failed wanted, only in_progress ever reported": (
        [retry(l2("in_progress", "FAILED"))],
        [wiring(conn("FAILED"))],
        "still_failed",
        None,
        None,
    ),
    "still_failed wanted with an id and the row already ACTIVE: no fallback": (
        [retry()],
        [wiring(conn("ACTIVE"))],
        "still_failed",
        ROW,
        None,
    ),
    "retry 503 forever: a 5xx is never evidence": (
        [Resp(503, {})],
        [wiring(conn("ACTIVE"))],
        "reconnected",
        ROW,
        None,
    ),
    "retry 404: not an answer that means no FAILED row": (
        [Resp(404, {"detail": "Reservation not found"})],
        [wiring(conn("RELEASED"))],
        "released",
        ROW,
        None,
    ),
    "wiring-status 503 while the retry names nothing": (
        [retry()],
        [Resp(503, {})],
        "reconnected",
        ROW,
        None,
    ),
    "superseded reported for the caller's own row: never accepted as released": (
        [retry(l2("superseded", "RELEASED")), FROZEN_409],
        [wiring(conn("RELEASED"))],
        "released",
        ROW,
        None,
    ),
    "superseded reported for another row on the port does not end the poll": (
        [retry(l2("superseded", "RELEASED", row_id=OTHER)), FROZEN_409],
        [wiring(conn("RELEASED"), conn("RELEASED", row_id=OTHER))],
        "released",
        ROW,
        STATUS_ROW,
    ),
    "superseded wanted and reported": (
        [retry(l2("superseded", "RELEASED"))],
        [wiring(conn("RELEASED"))],
        "superseded",
        ROW,
        RETRY_ROW,
    ),
}


def _kind(row):
    if row is None:
        return None
    return RETRY_ROW if "outcome" in row else STATUS_ROW


@pytest.mark.asyncio
@pytest.mark.parametrize("name", sorted(SCENARIOS))
async def test_poll_returns_the_expected_row_or_none(name):
    retries, statuses, outcome, failed_row_id, expected = SCENARIOS[name]
    stack = ScriptedStack(retries, statuses)
    row = await wr.poll_wiring_retry(
        stack, "r1", is_l2_port_1, outcome, failed_row_id=failed_row_id
    )
    assert _kind(row) == expected
    if expected == RETRY_ROW:
        assert row["outcome"] == outcome and is_l2_port_1(row)
    if expected == STATUS_ROW:
        assert row["id"] == ROW
        assert row["status"] == wr.SETTLED_STATUS[outcome]


@pytest.mark.asyncio
async def test_superseded_for_the_callers_row_ends_the_poll_at_once(clock):
    """One retry call, no wiring-status read, no sleep: the poll does not go on to
    find the row RELEASED on wiring-status a moment later."""
    stack = ScriptedStack(
        [retry(l2("superseded", "RELEASED")), FROZEN_409], [wiring(conn("RELEASED"))]
    )
    row = await wr.poll_wiring_retry(stack, "r1", is_l2_port_1, "released", failed_row_id=ROW)
    assert row is None
    assert (stack.retry_calls, stack.status_calls, clock.slept) == (1, 0, [])


@pytest.mark.asyncio
async def test_a_retry_answer_that_names_the_row_blocks_the_fallback_for_that_pass(clock):
    """While the retry names the row (still FAILED), wiring-status is not consulted,
    even though it would show the row settled."""
    stack = ScriptedStack([retry(l2("still_failed", "FAILED"))], [wiring(conn("ACTIVE"))])
    row = await wr.poll_wiring_retry(
        stack, "r1", is_l2_port_1, "reconnected", failed_row_id=ROW, timeout=5.0
    )
    assert row is None
    assert stack.status_calls == 0
    assert stack.retry_calls == 10  # every 0.5 s for 5 s


@pytest.mark.asyncio
async def test_timeout_is_counted_on_the_clock_and_returns_none(clock):
    stack = ScriptedStack([retry()], [wiring(conn("FAILED"))])
    row = await wr.poll_wiring_retry(
        stack, "r1", is_l2_port_1, "reconnected", failed_row_id=ROW, timeout=3.0, interval=0.5
    )
    assert row is None
    assert clock.now == 3.0
    assert (stack.retry_calls, stack.status_calls) == (6, 6)


@pytest.mark.asyncio
async def test_convergence_seen_on_the_first_pass_does_not_sleep(clock):
    stack = ScriptedStack([FROZEN_409], [wiring(conn("RELEASED"))])
    row = await wr.poll_wiring_retry(stack, "r1", is_l2_port_1, "released", failed_row_id=ROW)
    assert row == conn("RELEASED")
    assert (stack.retry_calls, stack.status_calls, clock.slept) == (1, 1, [])


@pytest.mark.asyncio
async def test_matches_selects_layer_and_identity():
    """An l1 row and an l2 row on another port carry the wanted outcome; neither is
    the row asked for."""
    answers = [
        retry(
            {
                "id": OTHER,
                "layer": "l1",
                "port_a": "1",
                "outcome": "reconnected",
                "status": "ACTIVE",
            },
            l2("reconnected", "ACTIVE", row_id=OTHER, port="2"),
        )
    ]
    stack = ScriptedStack(answers, [wiring()])
    row = await wr.poll_wiring_retry(stack, "r1", is_l2_port_1, "reconnected", timeout=1.0)
    assert row is None


def test_settled_row_matches_by_id_and_by_the_outcomes_status():
    status = {"connections": [conn("ACTIVE"), conn("RELEASED", row_id=OTHER)]}
    assert wr.settled_row(status, ROW, "reconnected") == conn("ACTIVE")
    assert wr.settled_row(status, ROW, "released") is None
    assert wr.settled_row(status, OTHER, "released") == conn("RELEASED", row_id=OTHER)
    assert wr.settled_row(status, OTHER, "reconnected") is None
    assert wr.settled_row(status, "33333333-3333-3333-3333-333333333333", "reconnected") is None
    # An outcome with no settled state is never read off wiring-status.
    assert wr.settled_row({"connections": [conn("FAILED")]}, ROW, "still_failed") is None
    assert wr.settled_row({"connections": [conn("ACTIVE")]}, ROW, "in_progress") is None
    # A body that is not the wiring-status object is no evidence.
    assert wr.settled_row(None, ROW, "reconnected") is None
    assert wr.settled_row([], ROW, "reconnected") is None


def test_matching_retry_rows_reads_only_a_200_object():
    body = {"results": [l2("still_failed", "FAILED"), l2("still_failed", "FAILED", port="2")]}
    assert wr.matching_retry_rows(200, body, is_l2_port_1) == [l2("still_failed", "FAILED")]
    assert wr.matching_retry_rows(409, body, is_l2_port_1) == []
    assert wr.matching_retry_rows(200, None, is_l2_port_1) == []
    assert wr.matching_retry_rows(200, {}, is_l2_port_1) == []
