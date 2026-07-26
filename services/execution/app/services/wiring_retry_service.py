"""Per-connection wiring retry: the manual and background reattempt of FAILED rows.

ADR 0007 Decision 6 items 2-3 (issue #345 P3b phase 4); direction-aware since ADR 0009
Decision 2 (issue #369). A hardware apply failure never rolls back the durable fork
save; it lands a FAILED l1_connection_assignments row (phase 3) tagged with the
direction (`intended`) the failed write was attempting. This module reattempts those
rows through the SAME driver machinery the consumer uses
(nats_consumer._apply_wiring_pairs), split by intended: an ACTIVE-intended row is
reattempted as a build (connect_ports, respecting the is_pair_active convergence gate
and the active-unique index, flipping the FAILED row to ACTIVE on success); a
RELEASED-intended row is reattempted as a release (disconnect_ports, respecting the
pair_needs_release idempotency gate, flipping the FAILED row to RELEASED on success).
Either way attempts/last_error accumulate on a repeat failure.

The freeze is direction-scoped (ADR 0009 phase 3, issue #369; vendra-approved
2026-07-22): a frozen reservation (ended: fork ARCHIVED, teardown ran) refuses only
BUILD-direction retries, since re-establishing a cross-connect for a dead reservation
is what the freeze exists to prevent. RELEASE-direction rows (intended RELEASED) are
still retryable while frozen: a disconnect that never confirmed leaves hardware
possibly still connected, and letting the terminal cleanup finish is exactly what a
frozen reservation wants. This is why the reservations proxy also permits retry for
COMPLETED/CANCELLED/FAILED and not just ACTIVE.

Two entry points share one core:

  - reattempt_reservation: the manual retry (the internal POST endpoint's worker).
    Reattempts every hardware-retryable FAILED row for one reservation regardless of
    the total-attempts cap (manual retry is the fallback for rows past the cap). On a
    frozen reservation it processes only RELEASE-direction rows and reports each
    BUILD-direction row with the outcome "frozen"; a frozen reservation with NO
    release-direction FAILED rows still raises WiringReservationFrozen (the pure-build
    refusal contract is unchanged).
  - run_wiring_retry_tick / run_wiring_retry_loop: the background auto-retry channel.
    Each tick sweeps at most wiring_retry_batch_size FAILED rows still under
    wiring_retry_max_attempts, skips a row only when its reservation is frozen AND the
    row is build-direction, and reattempts the survivors, batch capped exactly like
    the issue #24 health scheduler.

Both channels share one supersession guard (l1_assignment_service
supersede_release_if_reclaimed) run before a release retry fires the driver: a
release whose old cross-connect a newer build on another reservation already
destroyed is settled RELEASED with no driver call, reported as "superseded".

A FAILED row whose reason is one of the pinned verbatim-apply reasons (a recorded hop
that no longer resolves, or a hop set that is not a simple chain: ADR 0007 Decision 5)
is NOT hardware-retryable: the wiring intent itself cannot be applied and the recovery
is a fork re-save, so it is reported as such without burning a driver call.
"""

from __future__ import annotations

import asyncio
import logging
import uuid

import httpx
from sqlalchemy import select

from app.config import settings
from app.models.l1_connection_assignment import L1ConnectionAssignment
from app.models.l2_port_assignment import L2PortAssignment
from app.models.route_assignment import RouteAssignment
from app.services.l1_assignment_service import (
    due_failed_rows,
    failed_assignments_for_reservation,
    get_wiring_state,
    supersede_release_if_reclaimed,
)
from app.services.l2_membership_service import (
    due_failed_l2_rows,
    failed_memberships_for_reservation,
    supersede_l2_release_if_reclaimed,
)
from app.services.nats_consumer import (
    WIRING_NOT_SIMPLE_CHAIN_REASON,
    WIRING_UNRESOLVABLE_REASON,
)
from app.services.route_service import (
    due_failed_route_rows,
    failed_route_assignments_for_reservation,
)

logger = logging.getLogger(__name__)

# The pinned non-retryable reason prefixes (ADR 0007 Decision 5). A FAILED row whose
# last_error starts with either one records an intent that a verbatim apply cannot
# realize (the recorded switch/port is gone, or the flattened hop set cannot be paired):
# a hardware retry would re-hit the same wall, so recovery is a fork re-save. The
# load-error variants ("recorded hop unresolvable: switch <id> not found", etc.) start
# with the same prefix and are non-retryable for the same reason: there is no resolvable
# driver to call.
_NON_RETRYABLE_PREFIXES = (WIRING_UNRESOLVABLE_REASON, WIRING_NOT_SIMPLE_CHAIN_REASON)


class WiringReservationFrozen(RuntimeError):
    """Raised when a build-only retry targets a reservation whose wiring is frozen.

    A frozen reservation has ended (fork ARCHIVED, teardown ran): reattempting a
    BUILD (connect_ports) would re-establish hardware for a dead reservation, so a
    frozen reservation with only build-direction FAILED rows refuses retry. The
    refusal is direction-scoped (ADR 0009 phase 3, issue #369): a frozen reservation
    that still has RELEASE-direction FAILED rows does NOT raise; its releases are
    processed and its build rows are reported "frozen". The internal endpoint maps
    this raise to 409 (ADR 0007 Decision 6/7).
    """


def is_retryable_failure(last_error: str | None) -> bool:
    """True when a FAILED row's reason is a transient/driver failure worth reattempting.

    False for the pinned verbatim-apply reasons (Decision 5): those name an
    unresolvable intent whose recovery is a re-save, not a driver retry. A None or
    empty reason is treated as retryable (a driver failure that recorded no message).
    """
    if not last_error:
        return True
    return not last_error.startswith(_NON_RETRYABLE_PREFIXES)


def _identity(row: L1ConnectionAssignment) -> dict:
    """The per-connection identity fields surfaced in a retry outcome."""
    return {
        "id": str(row.id),
        "switch_device_id": str(row.switch_device_id),
        "port_a": row.port_a,
        "port_b": row.port_b,
        "physical_connection_id": (
            str(row.physical_connection_id) if row.physical_connection_id else None
        ),
    }


def _outcome(row: L1ConnectionAssignment, outcome: str) -> dict:
    return {
        **_identity(row),
        "outcome": outcome,
        "status": row.status,
        "attempts": row.attempts,
        "last_error": row.last_error,
        # `layer` is additive (ADR 0009 phase 4): existing L1 consumers ignore it, and it
        # lets a caller group L1 cross-connect rows from L2 membership rows in one payload.
        "layer": "l1",
    }


def _l2_identity(row: L2PortAssignment) -> dict:
    """The per-membership identity fields surfaced in an L2 retry outcome.

    L2 rows are per (switch, port), not a port pair, so they carry `port` (not port_a/
    port_b) and their `vlan_assignment_id`. port_a/port_b stay absent (the L1 shape is
    unchanged; the layered WiringRetryOutcome model makes them optional for L2 rows).
    """
    return {
        "id": str(row.id),
        "switch_device_id": str(row.switch_device_id),
        "port": row.port,
        "vlan_assignment_id": str(row.vlan_assignment_id),
    }


def _l2_outcome(row: L2PortAssignment, outcome: str, vlan_id: int | None = None) -> dict:
    return {
        **_l2_identity(row),
        "outcome": outcome,
        "status": row.status,
        "attempts": row.attempts,
        "last_error": row.last_error,
        "layer": "l2",
        "vlan": vlan_id,
    }


def _l3_identity(row: RouteAssignment) -> dict:
    """The per-switch identity fields surfaced in an L3 route-pin retry outcome.

    An L3 row is a per-switch pinned route SET, not a port or a port pair, so it carries
    only switch_device_id and a route-set summary (route_count). The L1/L2 port fields stay
    absent (the layered WiringRetryOutcome model makes them optional for L3 rows).
    """
    return {
        "id": str(row.id),
        "switch_device_id": str(row.device_id),
        "route_count": len(row.routes or []),
    }


def _l3_outcome(row: RouteAssignment, outcome: str) -> dict:
    return {
        **_l3_identity(row),
        "outcome": outcome,
        "status": row.status,
        "attempts": row.attempts,
        "last_error": row.last_error,
        "layer": "l3",
    }


async def _reattempt_rows(rows: list[L1ConnectionAssignment], get_db_session) -> list[dict]:
    """Reattempt a list of retryable FAILED rows in their own direction; return outcomes.

    Splits the rows by `intended` (issue #369, ADR 0009 Decision 2) before grouping by
    (reservation, switch): a row whose intended is ACTIVE is reattempted as a build
    (connect_ports), a row whose intended is RELEASED is reattempted as a release
    (disconnect_ports) - the direction fix this module exists for. Both directions are
    driven through the SAME consumer apply (_apply_wiring_pairs) per reservation, so a
    release-direction retry gets the same idempotency gate (pair_needs_release), driver
    machinery, and per-connection failure handling as the normal reconcile path. The
    rows may span reservations (the background batch does), so _apply_wiring_pairs is
    called once per reservation with both of that reservation's sets. On success a
    build flips the FAILED row to ACTIVE in place (record_l1_connect reuses the row); a
    release flips it to RELEASED in place (release_l1_connection reuses the row); on
    repeat failure record_l1_failed accumulates attempts and last_error. Outcomes are
    read back by row id: ACTIVE is "reconnected", RELEASED is "released", anything else
    is "still_failed". A row settled by the supersession guard reports "superseded".

    Before the release set is built, each release-direction row runs through the shared
    supersession guard (supersede_release_if_reclaimed): a release whose old
    cross-connect a newer build on another reservation already destroyed is flipped
    RELEASED with no driver call and reported "superseded", never entering the release
    set (ADR 0009 phase 3, issue #369). Both retry channels reach this function, so
    both get the guard.

    A TransientUpstreamError from resolving a switch (inventory/cabling 5xx) propagates
    to the caller: the manual endpoint maps it to 503; the background tick logs it and
    moves on. The rows stay FAILED for the next sweep.
    """
    from app.services.nats_consumer import _apply_wiring_pairs, _FetchContext

    if not rows:
        return []

    retry_ids: list[uuid.UUID] = [row.id for row in rows]

    # Supersession guard (shared by both channels): a release whose port a newer build
    # on another reservation already reclaimed needs no driver call. Flip it RELEASED
    # up front and keep its id so the read-back reports "superseded", not "released".
    superseded_ids: set[uuid.UUID] = set()
    async with get_db_session() as db:
        for row in rows:
            if row.intended == "RELEASED" and await supersede_release_if_reclaimed(db, row):
                superseded_ids.add(row.id)

    # reservation -> switch -> [(port_a, port_b, physical_connection_id)], split by
    # the row's intended direction. Superseded rows are already settled, so they are
    # left out of both apply sets.
    by_res_build: dict[str, dict[str, list[tuple[str, str, str | None]]]] = {}
    by_res_release: dict[str, dict[str, list[tuple[str, str, str | None]]]] = {}
    for row in rows:
        if row.id in superseded_ids:
            continue
        phys = str(row.physical_connection_id) if row.physical_connection_id else None
        res_str = str(row.reservation_id)
        target = by_res_release if row.intended == "RELEASED" else by_res_build
        target.setdefault(res_str, {}).setdefault(str(row.switch_device_id), []).append(
            (row.port_a, row.port_b, phys)
        )

    async with httpx.AsyncClient() as client:
        ctx = _FetchContext(client)
        for res_str in {*by_res_build, *by_res_release}:
            build_by_switch = by_res_build.get(res_str, {})
            release_by_switch = by_res_release.get(res_str, {})
            await _apply_wiring_pairs(
                res_str, release_by_switch, build_by_switch, [], ctx, get_db_session
            )

    async with get_db_session() as db:
        refreshed = (
            (
                await db.execute(
                    select(L1ConnectionAssignment).where(L1ConnectionAssignment.id.in_(retry_ids))
                )
            )
            .scalars()
            .all()
        )
    by_id = {r.id: r for r in refreshed}

    outcomes: list[dict] = []
    for rid in retry_ids:
        row = by_id.get(rid)
        if row is None:
            continue
        if rid in superseded_ids:
            outcome = "superseded"
        elif row.status == "ACTIVE":
            outcome = "reconnected"
        elif row.status == "RELEASED":
            outcome = "released"
        else:
            outcome = "still_failed"
        outcomes.append(_outcome(row, outcome))
    return outcomes


async def _reattempt_l2_rows(rows: list[L2PortAssignment], get_db_session) -> list[dict]:
    """Reattempt FAILED L2 membership rows in their own direction; return outcomes.

    The L2 analogue of _reattempt_rows. Split by intended: an ACTIVE-intended row is an
    add_to_vlan (join) reattempt (respecting is_membership_active), a RELEASED-intended
    row is a remove_from_vlan (leave) reattempt (respecting membership_needs_remove).
    Both directions drive through the SAME consumer apply (_apply_l2_memberships) per
    reservation, so a retry gets the same result gating, allocation lifecycle coupling
    (the last leave frees the allocation), and per-membership failure handling as the
    reconcile. Before the release set is built, each release-direction row runs through
    supersede_l2_release_if_reclaimed: a leave whose port another reservation's ACTIVE
    membership already reclaimed is settled RELEASED with no driver call ("superseded").
    Outcomes are read back by id: ACTIVE is "reconnected", RELEASED is "released", else
    "still_failed"; each carries layer "l2" plus its switch/port/vlan.

    A build-direction row whose stored allocation is the nil-UUID placeholder (a row the
    legacy device-set path parked when its allocation readback failed) or points at a
    vlan_assignment that no longer exists is re-resolved through _resolve_add_allocations
    before the op is built, so the retry drives add_to_vlan with the REAL fabric VLAN (not
    0) and record_l2_membership_active heals the row's allocation on success. If
    re-resolution fails again, the row is NOT driven: it is re-parked FAILED with the
    no-allocation reason (attempts accumulate) and reported "still_failed".
    """
    from app.models.vlan_assignment import VlanAssignment
    from app.services.l2_membership_service import record_l2_failed
    from app.services.nats_consumer import (
        WIRING_UNRESOLVABLE_REASON,
        _apply_l2_memberships,
        _FetchContext,
        _resolve_add_allocations,
    )

    if not rows:
        return []

    _NIL = uuid.UUID(int=0)
    retry_ids: list[uuid.UUID] = [row.id for row in rows]

    va_ids = {row.vlan_assignment_id for row in rows}
    async with get_db_session() as db:
        va_rows = (
            (await db.execute(select(VlanAssignment).where(VlanAssignment.id.in_(va_ids))))
            .scalars()
            .all()
        )
    vlan_by_va = {v.id: v.vlan_id for v in va_rows}

    superseded_ids: set[uuid.UUID] = set()
    async with get_db_session() as db:
        for row in rows:
            if row.intended == "RELEASED" and await supersede_l2_release_if_reclaimed(db, row):
                superseded_ids.add(row.id)

    def _add_needs_resolution(row: L2PortAssignment) -> bool:
        # A build-direction row whose allocation is unusable: the nil placeholder or a
        # vlan_assignment that no longer resolves. Re-resolve before driving the driver.
        return row.intended != "RELEASED" and (
            row.vlan_assignment_id == _NIL or row.vlan_assignment_id not in vlan_by_va
        )

    # Re-resolve allocations per reservation for the add rows that need it, so the retry
    # never drives add_to_vlan against the nil/vlan-0 placeholder.
    resolve_targets: dict[str, set[tuple[str, str]]] = {}
    for row in rows:
        if row.id in superseded_ids:
            continue
        if _add_needs_resolution(row):
            resolve_targets.setdefault(str(row.reservation_id), set()).add(
                (str(row.switch_device_id), row.port)
            )
    resolved: dict[str, dict[str, tuple[uuid.UUID, int]]] = {}
    for res_str, targets in resolve_targets.items():
        alloc = await _resolve_add_allocations(res_str, targets, get_db_session)
        resolved[res_str] = alloc
        # Fold the freshly-resolved VLAN numbers into the read-back map so a healed row's
        # outcome reports its real VLAN, not None.
        for va_id, vlan_id in alloc.values():
            vlan_by_va[va_id] = vlan_id

    # reservation -> {"removes": [...], "adds": [...]} of membership op dicts.
    by_res: dict[str, dict[str, list[dict]]] = {}
    unresolved_ids: set[uuid.UUID] = set()
    for row in rows:
        if row.id in superseded_ids:
            continue
        res_str = str(row.reservation_id)
        if row.intended == "RELEASED":
            entry = {
                "switch_device_id": str(row.switch_device_id),
                "port": row.port,
                "vlan_assignment_id": row.vlan_assignment_id,
                "vlan_id": vlan_by_va.get(row.vlan_assignment_id, 0),
            }
            by_res.setdefault(res_str, {"removes": [], "adds": []})["removes"].append(entry)
            continue

        if _add_needs_resolution(row):
            alloc = resolved.get(res_str, {}).get(str(row.switch_device_id))
            if alloc is None:
                # Re-resolution failed again: do NOT drive the driver against a bad VLAN.
                # Re-park FAILED with the (non-retryable) no-allocation reason so recovery
                # is a re-save, and report still_failed on read-back.
                unresolved_ids.add(row.id)
                async with get_db_session() as db:
                    await record_l2_failed(
                        db,
                        row.reservation_id,
                        None,
                        row.switch_device_id,
                        row.port,
                        1,
                        f"{WIRING_UNRESOLVABLE_REASON}: no VLAN allocation for fabric",
                        intended="ACTIVE",
                    )
                continue
            va_id, vlan_id = alloc
        else:
            va_id = row.vlan_assignment_id
            vlan_id = vlan_by_va[row.vlan_assignment_id]

        entry = {
            "switch_device_id": str(row.switch_device_id),
            "port": row.port,
            "vlan_assignment_id": va_id,
            "vlan_id": vlan_id,
        }
        by_res.setdefault(res_str, {"removes": [], "adds": []})["adds"].append(entry)

    async with httpx.AsyncClient() as client:
        ctx = _FetchContext(client)
        for res_str, sets in by_res.items():
            await _apply_l2_memberships(res_str, sets["removes"], sets["adds"], ctx, get_db_session)

    async with get_db_session() as db:
        refreshed = (
            (await db.execute(select(L2PortAssignment).where(L2PortAssignment.id.in_(retry_ids))))
            .scalars()
            .all()
        )
    by_id = {r.id: r for r in refreshed}

    outcomes: list[dict] = []
    for rid in retry_ids:
        row = by_id.get(rid)
        if row is None:
            continue
        if rid in superseded_ids:
            outcome = "superseded"
        elif row.status == "ACTIVE":
            outcome = "reconnected"
        elif row.status == "RELEASED":
            outcome = "released"
        else:
            outcome = "still_failed"
        outcomes.append(_l2_outcome(row, outcome, vlan_by_va.get(row.vlan_assignment_id)))
    return outcomes


async def _reattempt_l3_rows(rows: list[RouteAssignment], get_db_session) -> list[dict]:
    """Reattempt FAILED L3 route pins in their own direction; return outcomes.

    The L3 analogue of _reattempt_rows. Split by intended: an ACTIVE-intended row is a
    provision (configure_route) reattempt, a RELEASED-intended row is a deprovision
    (remove_route) reattempt. Both directions drive the PINNED route set verbatim (issue
    #20: never re-derived from the switch's current config) through the SAME consumer apply
    (_apply_l3_adjacency) per reservation, so a retry gets the same result gating, adjacency
    handling, and per-switch failure handling as the reconcile. Outcomes are read back by
    id: ACTIVE is "reconnected", RELEASED is "released", else "still_failed"; each carries
    layer "l3".

    There is NO supersession guard for L3 (unlike L1/L2). A route pin is a per-reservation,
    non-exclusive set: route_assignments keys on (reservation, switch) and holds only THIS
    reservation's routes, so another reservation gaining routes on the same switch never
    reconfigures or removes this reservation's pinned routes. A stale release therefore
    cannot strip a newer reservation's live state the way a re-fired L1/L2 port op could,
    so there is nothing for a supersession guard to settle. See the module note below.
    """
    from app.services.nats_consumer import _apply_l3_adjacency, _FetchContext

    if not rows:
        return []

    retry_ids: list[uuid.UUID] = [row.id for row in rows]

    # reservation -> {"deprovisions": [...], "provisions": [...]} of {device_id, routes}.
    by_res: dict[str, dict[str, list[dict]]] = {}
    for row in rows:
        res_str = str(row.reservation_id)
        entry = {"device_id": str(row.device_id), "routes": row.routes or []}
        bucket = "deprovisions" if row.intended == "RELEASED" else "provisions"
        by_res.setdefault(res_str, {"deprovisions": [], "provisions": []})[bucket].append(entry)

    async with httpx.AsyncClient() as client:
        ctx = _FetchContext(client)
        for res_str, sets in by_res.items():
            await _apply_l3_adjacency(
                res_str, sets["deprovisions"], sets["provisions"], ctx, get_db_session
            )

    async with get_db_session() as db:
        refreshed = (
            (await db.execute(select(RouteAssignment).where(RouteAssignment.id.in_(retry_ids))))
            .scalars()
            .all()
        )
    by_id = {r.id: r for r in refreshed}

    outcomes: list[dict] = []
    for rid in retry_ids:
        row = by_id.get(rid)
        if row is None:
            continue
        if row.status == "ACTIVE":
            outcome = "reconnected"
        elif row.status == "RELEASED":
            outcome = "released"
        else:
            outcome = "still_failed"
        outcomes.append(_l3_outcome(row, outcome))
    return outcomes


async def reattempt_reservation(reservation_id: uuid.UUID | str, get_db_session) -> dict:
    """Manual retry: reattempt every hardware-retryable FAILED row of one reservation.

    The frozen refusal is direction-scoped (ADR 0009 phase 3, issue #369): on a frozen
    reservation only RELEASE-direction rows are processed, each BUILD-direction row is
    reported with the outcome "frozen", and a frozen reservation with NO
    release-direction FAILED rows still raises WiringReservationFrozen (the pure-build
    refusal contract is unchanged). Ignores the total-attempts cap by design: manual
    retry is exactly the fallback for a row parked past the cap. Non-retryable rows
    (pinned reasons) are reported without a driver call.

    Returns {"reservation_id", "results": [outcome, ...]} where each outcome carries the
    connection identity, the post-retry status/attempts/last_error, and an `outcome` of
    "reconnected" (a build succeeded), "released" (a release succeeded, issue #369),
    "superseded" (a release a newer build already made redundant, no driver call),
    "still_failed", "not_retryable", or "frozen" (a build refused on a frozen
    reservation).
    """
    res_str = str(reservation_id)
    async with get_db_session() as db:
        state = await get_wiring_state(db, reservation_id)
        frozen = state is not None and state.frozen
        failed = await failed_assignments_for_reservation(db, reservation_id)
        failed_l2 = await failed_memberships_for_reservation(db, reservation_id)
        failed_l3 = await failed_route_assignments_for_reservation(db, reservation_id)

    has_release = (
        any(row.intended == "RELEASED" for row in failed)
        or any(row.intended == "RELEASED" for row in failed_l2)
        or any(row.intended == "RELEASED" for row in failed_l3)
    )
    if frozen and not has_release:
        # Pure-build (or empty) frozen case: nothing to release in EITHER layer, so retry
        # is refused exactly as before (the internal endpoint maps this to 409).
        raise WiringReservationFrozen(res_str)

    results: list[dict] = []
    retryable: list[L1ConnectionAssignment] = []
    for row in failed:
        if frozen and row.intended != "RELEASED":
            # A build cannot be reattempted for a dead reservation; report it without
            # a driver call while the release-direction rows below are still processed.
            results.append(_outcome(row, "frozen"))
        elif is_retryable_failure(row.last_error):
            retryable.append(row)
        else:
            results.append(_outcome(row, "not_retryable"))

    retryable_l2: list[L2PortAssignment] = []
    for row in failed_l2:
        if frozen and row.intended != "RELEASED":
            results.append(_l2_outcome(row, "frozen"))
        elif is_retryable_failure(row.last_error):
            retryable_l2.append(row)
        else:
            results.append(_l2_outcome(row, "not_retryable"))

    retryable_l3: list[RouteAssignment] = []
    for row in failed_l3:
        if frozen and row.intended != "RELEASED":
            results.append(_l3_outcome(row, "frozen"))
        elif is_retryable_failure(row.last_error):
            retryable_l3.append(row)
        else:
            results.append(_l3_outcome(row, "not_retryable"))

    results.extend(await _reattempt_rows(retryable, get_db_session))
    results.extend(await _reattempt_l2_rows(retryable_l2, get_db_session))
    results.extend(await _reattempt_l3_rows(retryable_l3, get_db_session))
    return {"reservation_id": res_str, "results": results}


# --- Background auto-retry channel (Decision 6 item 2) -----------------------


async def run_wiring_retry_tick(get_db_session) -> dict:
    """One sweep of the background auto-retry channel; returns tick stats.

    Selects at most wiring_retry_batch_size FAILED rows still under
    wiring_retry_max_attempts (rows past the cap are manual-retry only), drops the
    non-hardware-retryable ones and any BUILD-direction row whose reservation is frozen,
    then reattempts the survivors. The frozen skip is row-scoped and direction-aware
    (ADR 0009 phase 3, issue #369): a RELEASE-direction row is retried even on a frozen
    reservation, so a stuck disconnect can still finish after the reservation ends. The
    batch cap bounds one tick exactly as the health scheduler's batch cap does; a repeat
    failure accumulates attempts, so a row converges toward the cap and eventually stops
    being swept.
    """
    batch = max(1, settings.wiring_retry_batch_size)
    max_attempts = settings.wiring_retry_max_attempts

    async with get_db_session() as db:
        due = await due_failed_rows(db, batch, max_attempts)
        due_l2 = await due_failed_l2_rows(db, batch, max_attempts)
        due_l3 = await due_failed_route_rows(db, batch, max_attempts)

    # Combined totals across all layers (existing keys, unchanged shape) plus additive
    # per-layer retried counts (ADR 0009 phases 4-5): a caller can read the grand totals as
    # before or break them down by layer without the old keys shifting meaning.
    stats = {
        "rows_due": len(due) + len(due_l2) + len(due_l3),
        "rows_retried": 0,
        "reconnected": 0,
        "released": 0,
        "superseded": 0,
        "still_failed": 0,
        "skipped_frozen": 0,
        "skipped_not_retryable": 0,
        "l1_rows_retried": 0,
        "l2_rows_retried": 0,
        "l3_rows_retried": 0,
    }

    frozen_cache: dict[uuid.UUID, bool] = {}

    async def _is_frozen(res_id: uuid.UUID) -> bool:
        if res_id not in frozen_cache:
            async with get_db_session() as db:
                state = await get_wiring_state(db, res_id)
            frozen_cache[res_id] = bool(state is not None and state.frozen)
        return frozen_cache[res_id]

    to_retry: list[L1ConnectionAssignment] = []
    to_retry_l2: list[L2PortAssignment] = []
    to_retry_l3: list[RouteAssignment] = []
    for row in due:
        if not is_retryable_failure(row.last_error):
            stats["skipped_not_retryable"] += 1
            continue
        # Direction-scoped freeze: only a BUILD-direction row is skipped on a frozen
        # reservation; a RELEASE-direction row is still retried (issue #369).
        if row.intended != "RELEASED" and await _is_frozen(row.reservation_id):
            stats["skipped_frozen"] += 1
            continue
        to_retry.append(row)
    for row in due_l2:
        if not is_retryable_failure(row.last_error):
            stats["skipped_not_retryable"] += 1
            continue
        if row.intended != "RELEASED" and await _is_frozen(row.reservation_id):
            stats["skipped_frozen"] += 1
            continue
        to_retry_l2.append(row)
    for row in due_l3:
        if not is_retryable_failure(row.last_error):
            stats["skipped_not_retryable"] += 1
            continue
        if row.intended != "RELEASED" and await _is_frozen(row.reservation_id):
            stats["skipped_frozen"] += 1
            continue
        to_retry_l3.append(row)

    def _tally(outcomes: list[dict]) -> None:
        for o in outcomes:
            if o["outcome"] == "reconnected":
                stats["reconnected"] += 1
            elif o["outcome"] == "released":
                stats["released"] += 1
            elif o["outcome"] == "superseded":
                stats["superseded"] += 1
            elif o["outcome"] == "still_failed":
                stats["still_failed"] += 1

    if to_retry:
        try:
            outcomes = await _reattempt_rows(to_retry, get_db_session)
        except Exception:
            # A TransientUpstreamError (or any resolve failure) must never wedge the
            # loop: the rows stay FAILED and the next tick re-sweeps them.
            logger.warning(
                "wiring retry tick: L1 reattempt failed; rows stay FAILED", exc_info=True
            )
            outcomes = []
        stats["l1_rows_retried"] = len(outcomes)
        _tally(outcomes)

    if to_retry_l2:
        try:
            outcomes_l2 = await _reattempt_l2_rows(to_retry_l2, get_db_session)
        except Exception:
            logger.warning(
                "wiring retry tick: L2 reattempt failed; rows stay FAILED", exc_info=True
            )
            outcomes_l2 = []
        stats["l2_rows_retried"] = len(outcomes_l2)
        _tally(outcomes_l2)

    if to_retry_l3:
        try:
            outcomes_l3 = await _reattempt_l3_rows(to_retry_l3, get_db_session)
        except Exception:
            logger.warning(
                "wiring retry tick: L3 reattempt failed; rows stay FAILED", exc_info=True
            )
            outcomes_l3 = []
        stats["l3_rows_retried"] = len(outcomes_l3)
        _tally(outcomes_l3)

    stats["rows_retried"] = (
        stats["l1_rows_retried"] + stats["l2_rows_retried"] + stats["l3_rows_retried"]
    )

    log = logger.info if stats["rows_due"] else logger.debug
    log("wiring_retry_tick", extra={"action": "wiring_retry_tick", **stats})
    return stats


async def run_wiring_retry_loop(get_db_session) -> None:
    """Long-running background auto-retry loop. Cancellable via task.cancel().

    Each tick runs run_wiring_retry_tick on the wiring_retry_interval_seconds cadence.
    A tick that raises (unexpected: the tick swallows reattempt failures) backs off
    exponentially up to a cap, exactly like the health scheduler loop; a healthy tick
    resets to the base interval.
    """
    base_interval = max(1, settings.wiring_retry_interval_seconds)
    max_backoff = max(base_interval * 10, 300)
    current_backoff = base_interval
    logger.info(
        "wiring retry channel started; interval=%ss batch=%s max_attempts=%s",
        base_interval,
        settings.wiring_retry_batch_size,
        settings.wiring_retry_max_attempts,
    )
    while True:
        tick_failed = False
        try:
            await run_wiring_retry_tick(get_db_session)
        except asyncio.CancelledError:
            logger.info("wiring retry channel cancelled; exiting loop")
            raise
        except Exception:
            logger.exception("wiring retry tick failed")
            tick_failed = True

        current_backoff = min(current_backoff * 2, max_backoff) if tick_failed else base_interval
        await asyncio.sleep(current_backoff)


def make_session_ctx_factory():
    """A get_db_session context factory over AsyncSessionLocal (consumer convention).

    Mirrors the NATS consumer's _get_db_session: `async with factory() as db` yields a
    fresh AsyncSession closed on exit. Used by the background loop, which has no request
    scope of its own.
    """
    from app.database import AsyncSessionLocal

    class _SessionCtx:
        async def __aenter__(self):
            self._session = AsyncSessionLocal()
            return self._session

        async def __aexit__(self, *args):
            await self._session.close()

    def _get():
        return _SessionCtx()

    return _get


async def start_wiring_retry_scheduler(app) -> None:
    """Start the auto-retry channel as a background task on the FastAPI app.

    Gated by settings.wiring_retry_enabled, mirroring the health scheduler's run-mode
    posture (issue #24): enabled by default so a poller-only replica runs it, and set
    false on API replicas to keep the background work on the poller fleet. Stored under
    app.state.wiring_retry_task so the shutdown hook can cancel and await it.
    """
    if not settings.wiring_retry_enabled:
        logger.info("wiring retry channel disabled; skipping startup")
        return
    get_db_session = make_session_ctx_factory()
    task = asyncio.create_task(run_wiring_retry_loop(get_db_session))

    def _surface_crash(t: asyncio.Task) -> None:
        if t.cancelled():
            return
        exc = t.exception()
        if exc is not None:
            logger.error("wiring retry task exited unexpectedly: %s", exc)

    task.add_done_callback(_surface_crash)
    app.state.wiring_retry_task = task


async def stop_wiring_retry_scheduler(app) -> None:
    """Cancel the auto-retry channel task on app shutdown."""
    task = getattr(app.state, "wiring_retry_task", None)
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
