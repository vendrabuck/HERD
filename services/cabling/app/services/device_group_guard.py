import logging
import uuid

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


class DeviceNotFoundError(Exception):
    """Raised when inventory confirms the device does not exist (404).

    Distinct from the None-returning fail-open path below, which covers
    transport failures and non-404 error responses: those are genuine
    "could not verify" cases, while a 404 is inventory affirmatively saying
    the device is gone. See issue #392.
    """

    def __init__(self, device_id: uuid.UUID):
        self.device_id = device_id
        super().__init__(f"Device {device_id} not found")


async def fetch_device_group_ids(device_id: uuid.UUID, bearer_token: str) -> set[str] | None:
    """Return the set of device-group ids the device belongs to.

    An empty set means the device is in no group. None means membership could
    not be determined (inventory unreachable or returned a non-404 error);
    callers treat None as fail-open so an inventory hiccup does not block
    admin cabling. Raises DeviceNotFoundError on a 404, so callers can
    distinguish "device confirmed gone" from "could not verify" instead of
    fail-open on both.

    Reuses inventory's existing JWT endpoint by forwarding the caller's token,
    mirroring the cross-service pattern in reservation_guard.py.
    """
    url = f"{settings.inventory_service_url.rstrip('/')}/device-groups/device/{device_id}"
    headers = {"Authorization": f"Bearer {bearer_token}"}
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 404:
                raise DeviceNotFoundError(device_id)
            if resp.status_code >= 400:
                logger.warning(
                    "device_group_guard_bad_response",
                    extra={"status": resp.status_code, "device_id": str(device_id)},
                )
                return None
            return {str(group["id"]) for group in resp.json()}
    except DeviceNotFoundError:
        raise
    except Exception:
        logger.exception("device_group_guard_unreachable", extra={"device_id": str(device_id)})
        return None
