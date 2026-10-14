"""The enumerated row space for the retry tick's selection predicate (issue #1190).

Shared by the SQLite suite (test_wiring_due.py) and the Postgres-live suite
(test_wiring_retry_due_predicate_live_pg.py), so the same cases are judged on both
databases. That is the point: the predicate's traps (case-sensitivity of the prefix
test, NULL under three-valued logic) are exactly the places where SQLite and Postgres
can disagree, and a case list kept twice would drift.

The invariant under test, for every row r of a ledger:

    r is returned by the ledger's due query (ignoring LIMIT)
        if and only if
    r.status == "FAILED" and r.attempts < cap and r is unclaimed at `now`
    and is_retryable_failure(r.last_error)
    and not (r.intended != "RELEASED" and frozen(r.reservation_id))

The right-hand side is computed here in Python (`Case.expected`). Its reason part is
`retryable_by_definition`, the rule written out a third time, and
`assert_reason_expectations_match_python` checks the production
`is_retryable_failure` against it, so a test fails when the SQL and the Python
disagree and also when the Python rule itself moves.
"""

from __future__ import annotations

import itertools
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.models.l1_connection_assignment import L1ConnectionAssignment
from app.models.l2_port_assignment import L2PortAssignment
from app.models.reservation_wiring_state import ReservationWiringState
from app.models.route_assignment import RouteAssignment
from app.services import l1_assignment_service, l2_membership_service, route_service
from app.services.wiring_retry_service import is_retryable_failure

SWITCH_ID = uuid.uuid4()
ROUTES = [{"destination": "198.51.100.0/24", "next_hop": "198.51.100.1"}]


@dataclass(frozen=True)
class Ledger:
    """One wiring ledger: its model, its due query, and how to build a row for it.

    `due` and `failed_reader` look the function up on its module at call time, so a
    test that patches or edits the real query is the one that runs.
    """

    name: str
    model: type
    service: object
    due_name: str
    failed_reader_name: str
    release_action: str

    async def due(self, db, limit, max_attempts, now=None):
        return await getattr(self.service, self.due_name)(db, limit, max_attempts, now)

    async def failed_for_reservation(self, db, reservation_id):
        return await getattr(self.service, self.failed_reader_name)(db, reservation_id)

    def make(self, reservation_id: uuid.UUID, n: int, **cols):
        """A FAILED row of this ledger; `n` keeps the row's own identity distinct."""
        cols.setdefault("status", "FAILED")
        if self.model is L1ConnectionAssignment:
            return L1ConnectionAssignment(
                reservation_id=reservation_id,
                switch_device_id=SWITCH_ID,
                port_a=f"due/{n}/a",
                port_b=f"due/{n}/b",
                **cols,
            )
        if self.model is L2PortAssignment:
            cols.setdefault("vlan_assignment_id", uuid.uuid4())
            return L2PortAssignment(
                reservation_id=reservation_id,
                switch_device_id=SWITCH_ID,
                port=f"due/{n}",
                **cols,
            )
        cols.setdefault("routes", ROUTES)
        cols.setdefault("device_id", uuid.uuid4())
        return RouteAssignment(reservation_id=reservation_id, **cols)


LEDGERS = (
    Ledger(
        "l1",
        L1ConnectionAssignment,
        l1_assignment_service,
        "due_failed_rows",
        "failed_assignments_for_reservation",
        "disconnect_ports",
    ),
    Ledger(
        "l2",
        L2PortAssignment,
        l2_membership_service,
        "due_failed_l2_rows",
        "failed_memberships_for_reservation",
        "remove_from_vlan",
    ),
    Ledger(
        "l3",
        RouteAssignment,
        route_service,
        "due_failed_route_rows",
        "failed_route_assignments_for_reservation",
        "remove_route",
    ),
)
LEDGER_IDS = [ledger.name for ledger in LEDGERS]

# Every ledger's build direction is the literal "ACTIVE"; the release direction is
# "RELEASED". The tick's rule reads only "is it RELEASED or not".
BUILD = "ACTIVE"
RELEASE = "RELEASED"
INTENDED = (BUILD, RELEASE)

# The reservation's wiring state: no row at all, a row that is not frozen, a frozen row.
STATES = ("no_state_row", "not_frozen", "frozen")
CLAIMS = ("unclaimed", "claimed", "expired_claim")


def _swap_case_once(text: str) -> str | None:
    """`text` with its first cased character's case flipped, or None when it has none."""
    for i, ch in enumerate(text):
        if ch.swapcase() != ch:
            return text[:i] + ch.swapcase() + text[i + 1 :]
    return None


def retryable_by_definition(text: str | None, prefixes: tuple[str, ...]) -> bool:
    """The reason rule written out: not retryable exactly when the reason is non-empty
    and its first len(prefix) characters equal a prefix, compared case-sensitively.

    Deliberately neither `str.startswith` (what the production Python calls) nor SQL:
    a third statement of the same rule, which both of them are checked against.
    """
    if text is None or text == "":
        return True
    return not any(text[: len(prefix)] == prefix for prefix in prefixes)


def reason_texts(prefixes: tuple[str, ...]) -> list[str | None]:
    """The reason dimension for `prefixes`: every shape the prefix test can get wrong."""
    texts: list[str | None] = [
        None,
        "",
        "boom",
        # LIKE wildcards in a retryable reason, alone and inside text.
        "%",
        "_",
        "100%_done: driver said no_such_port",
        # Text outside ASCII.
        "pilote injoignable: d\u00e9lai d\u00e9pass\u00e9",
        "\u30c9\u30e9\u30a4\u30d0\u304c\u5fdc\u7b54\u3057\u307e\u305b\u3093",
    ]
    for prefix in prefixes:
        variants = [prefix.upper(), prefix.lower(), prefix.title(), _swap_case_once(prefix)]
        texts += [
            # The prefix exactly, and with a suffix (ASCII, and non-ASCII with wildcards).
            prefix,
            f"{prefix}: switch 0f3a not found",
            f"{prefix} \u00e9\u00e8 100%_",
            # One character short of the prefix.
            prefix[:-1],
            # The prefix present, but not at the start.
            f"driver said: {prefix}",
            f" {prefix}",
            f"\u00e9{prefix}",
        ]
        # The same letters in another case, alone and with a suffix.
        for variant in variants:
            if variant is not None:
                texts += [variant, f"{variant}: switch 0f3a not found"]
    return list(dict.fromkeys(texts))


def reason_cases(prefixes: tuple[str, ...]) -> list[tuple[str | None, bool]]:
    """(last_error, retryable) for every reason text, by the written-out definition."""
    return [(text, retryable_by_definition(text, prefixes)) for text in reason_texts(prefixes)]


@dataclass(frozen=True)
class Case:
    last_error: str | None
    retryable: bool
    intended: str
    state: str
    at_cap: bool
    claim: str

    @property
    def frozen(self) -> bool:
        return self.state == "frozen"

    @property
    def expected(self) -> bool:
        """The drive rule plus the query's own cap and claim filters, in Python."""
        eligible = not self.at_cap and self.claim != "claimed"
        would_drive = self.retryable and not (self.intended != RELEASE and self.frozen)
        return eligible and would_drive

    def describe(self) -> str:
        return (
            f"last_error={self.last_error!r} intended={self.intended} state={self.state} "
            f"at_cap={self.at_cap} claim={self.claim}"
        )


def all_cases(prefixes: tuple[str, ...]) -> list[Case]:
    """The full product of the five dimensions; nothing hand-picked."""
    return [
        Case(text, retryable, intended, state, at_cap, claim)
        for (text, retryable), intended, state, at_cap, claim in itertools.product(
            reason_cases(prefixes), INTENDED, STATES, (False, True), CLAIMS
        )
    ]


@dataclass
class Population:
    """One ledger's rows for the full case product, plus what each row stands for."""

    rows: list
    state_rows: list
    cases_by_id: dict[uuid.UUID, Case]
    reservation_ids: dict[str, uuid.UUID]

    @property
    def expected_ids(self) -> set[uuid.UUID]:
        return {row_id for row_id, case in self.cases_by_id.items() if case.expected}


def build_population(
    ledger: Ledger, prefixes: tuple[str, ...], now: datetime, cap: int
) -> Population:
    """Unsaved rows for every case, on three fresh reservations (one per wiring state)."""
    reservation_ids = {state: uuid.uuid4() for state in STATES}
    state_rows = [
        ReservationWiringState(reservation_id=reservation_ids["not_frozen"], frozen=False),
        ReservationWiringState(reservation_id=reservation_ids["frozen"], frozen=True),
    ]
    claimed_until = {
        "unclaimed": None,
        "claimed": now + timedelta(hours=1),
        "expired_claim": now - timedelta(hours=1),
    }
    rows = []
    cases_by_id: dict[uuid.UUID, Case] = {}
    for n, case in enumerate(all_cases(prefixes)):
        row_id = uuid.uuid4()
        rows.append(
            ledger.make(
                reservation_ids[case.state],
                n,
                id=row_id,
                intended=case.intended,
                attempts=cap if case.at_cap else cap - 1,
                last_error=case.last_error,
                claimed_until=claimed_until[case.claim],
            )
        )
        cases_by_id[row_id] = case
    return Population(rows, state_rows, cases_by_id, reservation_ids)


def assert_reason_expectations_match_python(prefixes: tuple[str, ...]) -> None:
    """`is_retryable_failure` answers every reason case as the written-out rule does.

    Called with the prefix tuple the production code is reading, so the definition in
    this module and the production Python rule pin each other.
    """
    wrong = [
        (text, retryable)
        for text, retryable in reason_cases(prefixes)
        if is_retryable_failure(text) is not retryable
    ]
    assert not wrong, f"is_retryable_failure disagrees with the written expectation: {wrong}"


def assert_parity(population: Population, returned_ids: set[uuid.UUID]) -> None:
    """`returned_ids` (the due query's answer, any foreign ids ignored) equals the rule."""
    own = set(population.cases_by_id)
    returned = returned_ids & own
    expected = population.expected_ids
    missing = [population.cases_by_id[i].describe() for i in expected - returned]
    extra = [population.cases_by_id[i].describe() for i in returned - expected]
    assert not missing and not extra, (
        f"due query and drive rule disagree on {len(missing) + len(extra)} of {len(own)} rows.\n"
        f"  the tick would drive these, the query did not return them ({len(missing)}):\n    "
        + "\n    ".join(sorted(missing)[:12])
        + f"\n  the query returned these, the tick would not drive them ({len(extra)}):\n    "
        + "\n    ".join(sorted(extra)[:12])
    )


FrozenLookup = Callable[[uuid.UUID], bool]


def python_would_drive(row, is_frozen: FrozenLookup) -> bool:
    """The tick's drive rule for a loaded ledger row, as run_wiring_retry_tick applies it."""
    return is_retryable_failure(row.last_error) and not (
        row.intended != RELEASE and is_frozen(row.reservation_id)
    )
