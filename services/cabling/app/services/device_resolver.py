"""Resolve device names to this instance's device ids via the inventory service.

Topology import carries device references by name (not raw UUID, which does not
match across instances). This calls inventory's internal resolve-by-name
endpoint with the X-Internal-Token, mirroring the cross-service pattern in
reservation_guard.py and device_group_guard.py. There is no cross-schema query:
the boundary is an HTTP call to the owning service.
"""

import logging

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


async def resolve_device_names(names: list[str]) -> dict[str, str]:
    """Return a name to device-id (string) map for the names that exist.

    Names with no matching device are omitted from the returned map, so the
    caller treats a missing key as an unresolved reference. Raises on transport
    failure so the import surfaces the inventory outage rather than silently
    dropping every device reference.
    """
    unique = sorted({n for n in names if n})
    if not unique:
        return {}
    url = f"{settings.inventory_service_url.rstrip('/')}/devices/resolve-by-name"
    headers = {"X-Internal-Token": settings.internal_api_token}
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(url, json={"names": unique}, headers=headers)
        resp.raise_for_status()
        return resp.json().get("resolved", {})
