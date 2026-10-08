"""Reachability-scoped VLAN assignment service.

Assigns VLAN IDs to reservations so that a number held by a live allocation is
never handed to another reservation anywhere in the connected cabling component
of the switches being allocated for, transit switches included (issue #1003).
Reservations whose switches sit in separate components (no cabled path between
them) may safely reuse a number.

The current cabling graph at allocation time is the authority. Cabling's internal
GET /fabric/internal answers a fabric id that is a deterministic hash of a device's
connected component on the current graph; connectivity is an equivalence relation,
so two switches reach each other through cabling if and only if cabling answers the
same id for both. The fabric id stored on an allocation row is therefore only a
cache of the walk at creation time, never the authority: every comparison re-asks
cabling for the CURRENT id of the switches the allocation is anchored at.

Fail closed: a fabric lookup that cannot be answered raises TransientUpstreamError,
so the consumer NAKs and retries (the posture of the intended-wires fetch); nothing
is allocated against a guessed fabric.

Limit by decision: the check runs when an allocation is made. A cable added later
that joins two components already holding the same number is not re-checked.

The release-side supersession guard (find_superseding_allocation, issue #1065) uses
the same reachability rule: a freed number's delete_vlan is skipped when another live
allocation holding it reaches the current component of a switch the number is
defined on.
"""

import logging
import uuid
from collections.abc import Awaitable, Callable

import httpx
from herd_common import advisory_lock
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.vlan_assignment import VlanAssignment
from app.services.nats_consumer import PermanentEventError, TransientUpstreamError

logger = logging.getLogger(__name__)

VLAN_MIN = 2
VLAN_MAX = 4094

# Bound on retries when a concurrent insert trips the partial-unique index. Each
# retry recomputes the free VLAN against the updated in-use set, so a small cap
# is enough to absorb realistic contention; exhaustion signals a real anomaly.
_MAX_ASSIGN_RETRIES = 5

# One transaction-scoped Postgres advisory lock serializes every VLAN allocation
# (issue #1003). The partial-unique index on (fabric_id, vlan_id) cannot arbitrate
# reachability on its own, because two rows in one component can carry different
# stored fabric ids after a cable change. Allocation happens only on a component's
# first built membership per reservation, so one global lock is cheap. The string
# must stay byte-identical across builds so rolling replicas take the same lock.
_ALLOCATION_LOCK_KEY = advisory_lock.advisory_key_from_string("herd-execution-vlan-allocation")


def _derive_vlan_id(reservation_id: str) -> int:
    """Derive a preferred VLAN ID (2-4094) from a reservation UUID."""
    return (uuid.UUID(reservation_id).int % (VLAN_MAX - VLAN_MIN + 1)) + VLAN_MIN


async def fetch_fabric_id(device_id: str) -> uuid.UUID:
    """Fetch a device's CURRENT fabric id (connected-component key) from cabling.

    Raises TransientUpstreamError on any non-200 answer, a transport error, or an
    unparseable body (issue #1003, fail closed): there is no stand-in fabric, so an
    unanswerable lookup defers the allocation instead of guessing a scope.
    """
    url = f"{settings.cabling_service_url}/fabric/internal"
    what = f"cabling fabric lookup for device {device_id}"
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                url,
                params={"device_id": device_id},
                headers={"X-Internal-Token": settings.internal_api_token},
                timeout=10.0,
            )
    except httpx.HTTPError as exc:
        raise TransientUpstreamError(f"{what}: transport error {type(exc).__name__}") from exc
    if resp.status_code != 200:
        raise TransientUpstreamError(f"{what}: upstream {resp.status_code}")
    try:
        return uuid.UUID(str(resp.json()["fabric_id"]))
    except (ValueError, KeyError, TypeError) as exc:
        raise TransientUpstreamError(f"{what}: unparseable answer") from exc


class FabricResolver:
    """Memoized CURRENT-fabric lookup for one allocation pass (issue #1003).

    Each switch is looked up at most once per pass. The default fetch is the
    module-level fetch_fabric_id, resolved at call time. A failed lookup raises
    TransientUpstreamError and is not cached.
    """

    def __init__(self, fetch: Callable[[str], Awaitable[uuid.UUID]] | None = None) -> None:
        self._fetch = fetch
        self._cache: dict[str, uuid.UUID] = {}

    async def fabric_of(self, switch_id: object) -> uuid.UUID:
        key = str(switch_id)
        if key not in self._cache:
            fetch = self._fetch if self._fetch is not None else fetch_fabric_id
            self._cache[key] = await fetch(key)
        return self._cache[key]


def allocation_anchor_switches(row: VlanAssignment) -> list[str]:
    """The switches an allocation's number is positioned at: its definition scope
    (switch_device_ids) plus the switches create_vlan provably defined it on
    (defined_switch_ids), in a stable order without duplicates."""
    seen: dict[str, None] = {}
    for sid in list(row.switch_device_ids or []) + list(row.defined_switch_ids or []):
        seen.setdefault(str(sid), None)
    return list(seen)


async def allocation_reaches(
    row: VlanAssignment, fabric_id: uuid.UUID, resolver: FabricResolver
) -> bool:
    """True when the allocation sits in the connected component whose CURRENT
    fabric id is fabric_id: its stored fabric id equals it (the same member set),
    or any anchor switch's current fabric id does."""
    if row.fabric_id == fabric_id:
        return True
    for sid in allocation_anchor_switches(row):
        if await resolver.fabric_of(sid) == fabric_id:
            return True
    return False


async def find_superseding_allocation(
    db: AsyncSession,
    row: VlanAssignment,
    switch_ids: list[str],
    resolver: FabricResolver,
) -> uuid.UUID | None:
    """The id of another ACTIVE allocation holding row's VLAN number in the CURRENT
    connected component of any of switch_ids, or None (issue #1065).

    The release-side supersession guard (WIRE-VLAN-11) judges by the same reachability
    rule as allocation: an allocation sits in a switch's component when allocation_reaches
    says so for that switch's current fabric id. row's own stored fabric id is never
    compared, because a cable change since row was allocated re-keys its component.
    With no other ACTIVE allocation holding the number nothing is looked up. A fabric
    lookup that cannot be answered raises TransientUpstreamError (fail closed).
    """
    rivals = (
        (
            await db.execute(
                select(VlanAssignment)
                .where(
                    VlanAssignment.vlan_id == row.vlan_id,
                    VlanAssignment.status == "ACTIVE",
                    VlanAssignment.id != row.id,
                )
                .order_by(VlanAssignment.created_at, VlanAssignment.id)
            )
        )
        .scalars()
        .all()
    )
    if not rivals:
        return None
    fabrics: list[uuid.UUID] = []
    for sid in switch_ids:
        fid = await resolver.fabric_of(sid)
        if fid not in fabrics:
            fabrics.append(fid)
    for rival in rivals:
        for fid in fabrics:
            if await allocation_reaches(rival, fid, resolver):
                return rival.id
    return None


async def _reachable_active(
    db: AsyncSession,
    reservation_uuid: uuid.UUID,
    fabric_id: uuid.UUID,
    resolver: FabricResolver,
) -> tuple[VlanAssignment | None, set[int]]:
    """Return (this reservation's reachable ACTIVE allocation, numbers in use in reach).

    Own rows are checked first and short-circuit: an existing reachable allocation
    is the answer and the other reservations' rows are not walked. Among several
    reachable own rows (rows split by an earlier cable change) the oldest wins.
    """
    rows = (
        (
            await db.execute(
                select(VlanAssignment)
                .where(VlanAssignment.status == "ACTIVE")
                .order_by(VlanAssignment.created_at, VlanAssignment.id)
                # The pre-pass already loaded these rows into the session; refresh
                # them so the locked re-read sees a concurrent scope change too.
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        if row.reservation_id == reservation_uuid and await allocation_reaches(
            row, fabric_id, resolver
        ):
            return row, set()
    used: set[int] = set()
    for row in rows:
        if row.reservation_id == reservation_uuid or row.vlan_id in used:
            continue
        if await allocation_reaches(row, fabric_id, resolver):
            used.add(row.vlan_id)
    return None, used


async def find_or_assign_allocation(
    db: AsyncSession,
    reservation_id: str,
    fabric_id: uuid.UUID,
    switch_device_ids: list[str],
    resolver: FabricResolver,
) -> tuple[uuid.UUID, int]:
    """Find or create the reservation's VLAN allocation for one connected component.

    fabric_id is the CURRENT fabric id of switch_device_ids, resolved through the
    same resolver. Returns (vlan_assignment id, vlan_id).

    1. If this reservation already holds an ACTIVE allocation that reaches the
       component, return it (idempotency, and no second number for one reservation
       inside one component after a cable change).
    2. Otherwise collect the numbers of every other ACTIVE allocation that reaches
       the component, take the preferred number derived from the reservation id if
       free, else the lowest free one, and insert a new ACTIVE allocation.

    Concurrency: the read-choose-insert runs under one transaction-scoped advisory
    lock (_ALLOCATION_LOCK_KEY), which releases on commit or rollback. The fabric
    lookups are made once before the lock is taken (memoized in the resolver), so
    under the lock only switches anchoring a row inserted meanwhile are looked up.
    The partial-unique index on (fabric_id, vlan_id) stays as a backstop: an insert
    that trips it rolls back and retries, at most _MAX_ASSIGN_RETRIES times.
    """
    reservation_uuid = uuid.UUID(reservation_id)
    preferred = _derive_vlan_id(reservation_id)

    # Pre-pass outside the lock: warms the resolver and answers the common
    # idempotent case without serializing on the lock.
    own, _used = await _reachable_active(db, reservation_uuid, fabric_id, resolver)
    if own is not None:
        return own.id, own.vlan_id

    for _attempt in range(_MAX_ASSIGN_RETRIES):
        await advisory_lock.xact_lock(db, _ALLOCATION_LOCK_KEY)
        own, used_vlans = await _reachable_active(db, reservation_uuid, fabric_id, resolver)
        if own is not None:
            found = (own.id, own.vlan_id)
            await db.rollback()  # release the lock; nothing was written
            return found

        # Try preferred VLAN first, else the lowest free VLAN in range
        if preferred not in used_vlans:
            vlan_id = preferred
        else:
            vlan_id = None
            for candidate in range(VLAN_MIN, VLAN_MAX + 1):
                if candidate not in used_vlans:
                    vlan_id = candidate
                    break
            if vlan_id is None:
                await db.rollback()
                # Pool exhausted: the in-use set is fixed for this component, so a
                # NATS redelivery would recompute the identical empty free set.
                # Raise a permanent error so the consumer DLQs on first delivery
                # instead of burning the full max_deliver backoff schedule.
                raise PermanentEventError(
                    f"No free VLAN IDs in fabric {fabric_id} (all {VLAN_MAX - VLAN_MIN + 1} in use)"
                )

        assignment = VlanAssignment(
            reservation_id=reservation_uuid,
            fabric_id=fabric_id,
            vlan_id=vlan_id,
            switch_device_ids=[str(sid) for sid in switch_device_ids],
            status="ACTIVE",
        )
        db.add(assignment)
        try:
            await db.commit()
        except IntegrityError:
            # The backstop index tripped (a writer outside the lock, or a pre-lock
            # build during a rolling deploy). Roll back and retry: the loop re-reads
            # the in-use set and either picks another number or finds our row.
            await db.rollback()
            continue
        await db.refresh(assignment)

        logger.info(
            "Assigned VLAN %d to reservation %s in fabric %s (preferred was %d)",
            vlan_id,
            reservation_id,
            fabric_id,
            preferred,
        )
        return assignment.id, vlan_id

    raise RuntimeError(
        f"Could not assign a VLAN in fabric {fabric_id} after {_MAX_ASSIGN_RETRIES} "
        "attempts due to persistent contention"
    )


async def find_or_assign_vlan(
    db: AsyncSession,
    reservation_id: str,
    fabric_id: uuid.UUID,
    switch_device_ids: list[str],
    resolver: FabricResolver,
) -> int:
    """find_or_assign_allocation, returning only the VLAN number."""
    _va_id, vlan_id = await find_or_assign_allocation(
        db, reservation_id, fabric_id, switch_device_ids, resolver
    )
    return vlan_id
