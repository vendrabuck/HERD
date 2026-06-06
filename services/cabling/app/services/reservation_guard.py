import logging
import uuid
from datetime import datetime, timedelta, timezone

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_BLOCKING_STATUSES = {"ACTIVE", "PENDING_PROVISION", "PENDING"}


async def find_blocking_reservations(topology_id: uuid.UUID, bearer_token: str) -> list[dict]:
    """Return any reservations currently referencing this topology.

    Queries the reservations calendar for a one-minute window around now. Any
    reservation whose topology_id matches and whose status is ACTIVE,
    PENDING_PROVISION, or PENDING is considered blocking. Returns an empty list
    if the reservations service is unreachable (fail-open: we'd rather let a
    restore proceed than block on a downed service).
    """
    now = datetime.now(timezone.utc)
    url = f"{settings.reservations_service_url.rstrip('/')}/calendar"
    params = {
        "range_start": (now - timedelta(minutes=1)).isoformat(),
        "range_end": (now + timedelta(minutes=1)).isoformat(),
    }
    headers = {"Authorization": f"Bearer {bearer_token}"}
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(url, params=params, headers=headers)
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
