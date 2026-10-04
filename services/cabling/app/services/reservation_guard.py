import logging
import uuid

import httpx
from fastapi import HTTPException

from app.config import settings

logger = logging.getLogger(__name__)

_BLOCKING_STATUSES = {"ACTIVE", "PENDING_PROVISION", "PENDING"}


async def find_blocking_reservations(topology_id: uuid.UUID) -> list[dict]:
    """Return any reservations referencing this topology with a blocking status.

    Calls the reservations service's internal by-topology endpoint with the
    X-Internal-Token rather than the visibility-filtered /calendar route: the
    lock must see reservations held by ANY user, not just ones the editing user
    can see, or a non-admin topology creator could rewire a topology out from
    under another user's reservation (the /calendar route hides reservations on
    devices the caller cannot see).

    A reservation is blocking when its status is ACTIVE, PENDING_PROVISION, or
    PENDING. Returns an empty list if the reservations service is unreachable
    (fail-open: we would rather let an edit proceed than block on a downed
    service).
    """
    url = f"{settings.reservations_service_url.rstrip('/')}/internal/by-topology/{topology_id}"
    headers = {"X-Internal-Token": settings.internal_api_token}
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code >= 400:
                logger.warning(
                    "reservation_guard_bad_response",
                    extra={"status": resp.status_code, "topology_id": str(topology_id)},
                )
                return []
            items = resp.json()
    except Exception:
        logger.exception("reservation_guard_unreachable", extra={"topology_id": str(topology_id)})
        return []

    blocking = []
    for item in items:
        if str(item.get("topology_id") or "") != str(topology_id):
            continue
        if str(item.get("status") or "").upper() not in _BLOCKING_STATUSES:
            continue
        blocking.append(item)
    return blocking


# --- Topology DELETE guard (issue #977) ------------------------------------
#
# The invariant: no topology row referenced by a non-terminal reservation
# (PENDING, PENDING_PROVISION, ACTIVE) is ever deleted. A PENDING reservation
# whose topology is gone activates with an EMPTY fork canvas
# (fork_service._resolve_parent_canvas finds neither a version nor the
# topology), holding its devices with no wiring built and nothing logged.
# COMPLETED, CANCELLED, and FAILED reservations do not block: their forks are
# archived and self-contained.
#
# Unlike find_blocking_reservations above, this check fails CLOSED: a delete is
# destructive, so an unreachable reservations service, a non-200, or a body not
# in the expected shape is a 503 and nothing is deleted. The edit-lock callers
# keep their fail-open behavior unchanged.
#
# KNOWN LIMIT (documented, not closed): a reservation created between this
# check and the delete's commit still lands on a deleted topology. Reservation
# create validates the topology over HTTP and commits in its own database, so
# closing the window would need cross-service coordination; the window is the
# reservation's validate-to-commit time, well under a second.

TOPOLOGY_DELETE_UNVERIFIABLE_DETAIL = "Could not verify topology is not in use"

# Every status the reservations service can report. A status outside this set
# reads as unverifiable rather than as terminal, so a status added later cannot
# silently let a live reservation's topology be deleted.
_KNOWN_STATUSES = _BLOCKING_STATUSES | {"COMPLETED", "CANCELLED", "FAILED"}


def _topology_delete_unverifiable() -> HTTPException:
    return HTTPException(status_code=503, detail=TOPOLOGY_DELETE_UNVERIFIABLE_DETAIL)


async def find_blocking_reservations_strict(topology_id: uuid.UUID) -> list[str]:
    """Ids of non-terminal reservations referencing the topology, failing closed.

    Reads the same internal by-topology route as find_blocking_reservations,
    which returns EVERY reservation with that topology_id (all statuses, no
    pagination, no cap), so an empty answer is a complete "none". Raises
    HTTPException(503) on a transport error, a non-200, or a body that is not a
    list of objects each carrying a string `id`, a known `status`, and this
    topology's `topology_id`.
    """
    url = f"{settings.reservations_service_url.rstrip('/')}/internal/by-topology/{topology_id}"
    headers = {"X-Internal-Token": settings.internal_api_token}
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(url, headers=headers)
    except Exception as exc:
        logger.error(
            "reservations service unreachable while checking topology %s for live reservations: %s",
            topology_id,
            exc,
        )
        raise _topology_delete_unverifiable() from exc

    if resp.status_code != 200:
        logger.error(
            "reservations service returned %s while checking topology %s for live reservations",
            resp.status_code,
            topology_id,
        )
        raise _topology_delete_unverifiable()

    try:
        items = resp.json()
        if not isinstance(items, list):
            raise TypeError("by-topology body must be a list")
        blocking: list[str] = []
        for item in items:
            if not isinstance(item, dict):
                raise TypeError("by-topology item must be an object")
            rid = item["id"]
            status = item["status"]
            if not isinstance(rid, str) or not isinstance(status, str):
                raise TypeError("id and status must be strings")
            if status.upper() not in _KNOWN_STATUSES:
                raise ValueError(f"unknown reservation status {status!r}")
            if str(item.get("topology_id") or "") != str(topology_id):
                raise ValueError("by-topology item names another topology")
            if status.upper() in _BLOCKING_STATUSES:
                blocking.append(rid)
    except (ValueError, KeyError, TypeError) as exc:
        logger.error(
            "reservations service returned an unparseable by-topology body for topology %s: %s",
            topology_id,
            exc,
        )
        raise _topology_delete_unverifiable() from exc
    return sorted(set(blocking))


async def assert_topology_deletable(topology_id: uuid.UUID) -> None:
    """Return None when no live reservation references the topology, else raise.

    Raises 409 {"error": "topology_in_use", "reservation_ids": [sorted]} while
    any PENDING, PENDING_PROVISION, or ACTIVE reservation references it, and 503
    when reservations cannot answer. No force flag and no cascade, matching the
    device delete guards (issues #900 and #940). The ONE gate for every path
    that deletes a topology row.
    """
    blocking = await find_blocking_reservations_strict(topology_id)
    if blocking:
        raise HTTPException(
            status_code=409,
            detail={"error": "topology_in_use", "reservation_ids": blocking},
        )
