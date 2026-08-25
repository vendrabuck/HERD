import logging
import uuid

import httpx

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
