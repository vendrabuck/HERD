"""Admin device DELETE guard (issues #391, #900, and #940).

The invariant: an admin device DELETE is refused while ANY live reservation's
wiring depends on the device, whether the device is a booked member or only a
transit hop on a saved fork, and while ANY cabling `Connection` row names it.
Deleting it would orphan the UUID across reservations, cabling, and execution
(no cross-schema FKs by design), and once the inventory row is gone there is no
driver or template left to release the device's cross-connects, so refusing is
the only real fix.

Two upstream questions, three answers:

- reservations (`find_blocking_reservations_for_device`, issue #391): the
  device is in a non-terminal reservation's booked set (members).
- cabling (`GET /internal/forks/by-device/{id}`, issue #900): the device is
  named on either end of a fork_connections row of a non-archived fork. A
  transit switch is in no `reservation_devices` row, so only this sees it.

Refusal is `409 {"error": "device_in_use", "reservation_ids": [sorted union of
both sources], "transit_reservation_ids": [sorted ids that hold the device only
as a transit hop]}`. `reservation_ids` keeps its pre-#900 meaning for existing
clients; `transit_reservation_ids` is additive and always present.

Checked AFTER that decision (so `device_in_use` wins when both apply), a device
still named by a cabling connection is refused with `409 {"error":
"device_cabled", "connection_count": N, "connection_ids": [sorted sample]}`
(`connection_count` is the true total, the id list is capped by cabling). The
admin removes the cables first; there is no cascade and no force flag. Either
upstream unreachable, erroring, or answering with an unparseable body fails
CLOSED with `503 "Could not verify device is not in use"`: a delete is
destructive and rare, so an unverifiable in-use check must block. No force flag.

KNOWN LIMIT (documented, not closed): a fork is archived when its reservation
goes terminal, while execution's hardware teardown runs asynchronously after
that. A delete in the short window after a cancel can therefore still precede
the release of the device's cross-connects.

The internal dynamic-instance delete route is deliberately exempt (its owning
reservation is itself the caller), and device_configs.py uses the reservation
guard for a different purpose; neither goes through here.
"""

import logging
import uuid
from typing import NamedTuple

import httpx
from fastapi import HTTPException
from herd_common.internal_client import InternalTokenAuth, call_service

from app.config import settings
from app.services.reservation_guard import find_blocking_reservations_for_device

logger = logging.getLogger(__name__)

UNVERIFIABLE_DETAIL = "Could not verify device is not in use"


def _unverifiable() -> HTTPException:
    return HTTPException(status_code=503, detail=UNVERIFIABLE_DETAIL)


class CablingDependents(NamedTuple):
    """What cabling says still names a device (one `by-device` response)."""

    reservation_ids: list[str]
    connection_count: int
    connection_ids: list[str]


async def find_cabling_dependents_for_device(device_id: uuid.UUID) -> CablingDependents:
    """Fork reservation ids and cabling connections that name the device.

    Raises HTTPException(503) on a transport error, a missing internal token, a
    non-200 response, or a body that lacks or mistypes `reservation_ids` (list),
    `connection_count` (int) or `connection_ids` (list). A cabling build that
    predates #940 omits the last two, which must read as unverifiable and never
    as "not cabled".
    """
    try:
        resp = await call_service(
            settings.cabling_service_url.rstrip("/"),
            "GET",
            f"/internal/forks/by-device/{device_id}",
            auth=InternalTokenAuth(token=settings.internal_api_token),
            timeout=5.0,
        )
    except (httpx.HTTPError, RuntimeError) as exc:
        logger.error(
            "cabling service unreachable while checking device %s for live fork wiring: %s",
            device_id,
            exc,
        )
        raise _unverifiable() from exc

    if resp.status_code != 200:
        logger.error(
            "cabling service returned %s while checking device %s for live fork wiring",
            resp.status_code,
            device_id,
        )
        raise _unverifiable()

    try:
        body = resp.json()
        ids = body["reservation_ids"]
        count = body["connection_count"]
        conn_ids = body["connection_ids"]
        if not isinstance(ids, list) or not isinstance(conn_ids, list):
            raise TypeError("reservation_ids and connection_ids must be lists")
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise TypeError("connection_count must be a non-negative int")
        return CablingDependents([str(i) for i in ids], count, [str(c) for c in conn_ids])
    except (ValueError, KeyError, TypeError) as exc:
        logger.error(
            "cabling service returned an unparseable fork-by-device body for device %s",
            device_id,
        )
        raise _unverifiable() from exc


async def assert_device_deletable(device_id: uuid.UUID) -> None:
    """Return None when nothing live depends on the device, else raise.

    Raises 409 `device_in_use`, then 409 `device_cabled` (see module docstring
    for the exact shapes and the order) or 503 when either upstream cannot be
    consulted.
    """
    try:
        blocking = await find_blocking_reservations_for_device(device_id)
    except HTTPException as exc:
        if exc.status_code == 503:
            raise _unverifiable() from exc
        raise
    member_ids = {str(b.get("id")) for b in blocking}

    dependents = await find_cabling_dependents_for_device(device_id)
    fork_ids = set(dependents.reservation_ids)
    transit_ids = fork_ids - member_ids

    if member_ids or transit_ids:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "device_in_use",
                "reservation_ids": sorted(member_ids | fork_ids),
                "transit_reservation_ids": sorted(transit_ids),
            },
        )

    if dependents.connection_count > 0:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "device_cabled",
                "connection_count": dependents.connection_count,
                "connection_ids": sorted(dependents.connection_ids),
            },
        )
