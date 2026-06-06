import logging
import uuid

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


async def fetch_device_group_ids(device_id: uuid.UUID, bearer_token: str) -> set[str] | None:
    """Return the set of device-group ids the device belongs to.

    An empty set means the device is in no group. None means membership could
    not be determined (inventory unreachable or returned an error); callers
    treat None as fail-open so an inventory hiccup does not block admin cabling.

    Reuses inventory's existing JWT endpoint by forwarding the caller's token,
    mirroring the cross-service pattern in reservation_guard.py.
    """
    url = f"{settings.inventory_service_url.rstrip('/')}/device-groups/device/{device_id}"
    headers = {"Authorization": f"Bearer {bearer_token}"}
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code >= 400:
                logger.warning(
                    "device_group_guard_bad_response",
                    extra={"status": resp.status_code, "device_id": str(device_id)},
                )
                return None
            return {str(group["id"]) for group in resp.json()}
    except Exception:
        logger.exception("device_group_guard_unreachable", extra={"device_id": str(device_id)})
        return None
