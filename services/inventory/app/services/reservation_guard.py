"""Cross-service guard: does an active reservation block a config-version restore?

Issue #337: config-version restore had no equivalent of topology restore's
active-reservation lock (services/cabling/app/services/reservation_guard.py,
services/cabling/app/routes/versions.py). Restoring an old config onto a
device changes what a later automated action (e.g. execution's L3 route
provisioning, which pulls the device's *latest* config version, not the
applied one) would push, so doing that out from under someone else's active
reservation is the same class of surprise the topology guard exists to
prevent.

This mirrors that guard's shape (device-scoped instead of topology-scoped,
same blocking-status set) but deliberately does NOT mirror its fail-open
behavior on an unreachable upstream: it standardizes to 503, matching this
service's existing cross-service-check convention (see
hypervisor_service.validate_secret_exists and device_groups' auth-service
calls, issue #131). A config rollback is a higher-stakes write than a
topology-canvas edit, so an unconfirmed guard should block, not silently let
the restore through.
"""

import logging
import uuid

import httpx
from fastapi import HTTPException

from app.config import settings

logger = logging.getLogger(__name__)

_BLOCKING_STATUSES = {"ACTIVE", "PENDING_PROVISION", "PENDING"}


async def find_blocking_reservations_for_device(device_id: uuid.UUID) -> list[dict]:
    """Return reservations on this device with a status that blocks a restore.

    Calls reservations' internal by-device endpoint with X-Internal-Token, the
    same internal-token pattern cabling's find_blocking_reservations uses for
    /internal/by-topology, so the lock sees every reservation on the device
    regardless of who holds it or which device groups the caller can see.

    Raises HTTPException(503) on a transport error or a non-200 response
    instead of failing open; see the module docstring for why this differs
    from the topology guard.
    """
    url = f"{settings.reservations_service_url.rstrip('/')}/internal/by-device/{device_id}"
    headers = {"X-Internal-Token": settings.internal_api_token}
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        logger.error(
            "reservations service unreachable while checking device %s for active reservations: %s",
            device_id,
            exc,
        )
        raise HTTPException(
            status_code=503,
            detail="reservations service unreachable while checking active reservations",
        ) from exc

    if resp.status_code != 200:
        logger.error(
            "reservations service returned %s while checking device %s for active reservations",
            resp.status_code,
            device_id,
        )
        raise HTTPException(
            status_code=503,
            detail="reservations service returned an error while checking active reservations",
        )

    items = resp.json()
    return [item for item in items if str(item.get("status") or "").upper() in _BLOCKING_STATUSES]
