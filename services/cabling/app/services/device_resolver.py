"""Resolve device names to this instance's device ids via the inventory service.

Topology import carries device references by name (not raw UUID, which does not
match across instances). This calls inventory's internal resolve-by-name
endpoint with the X-Internal-Token, mirroring the cross-service pattern in
reservation_guard.py and device_group_guard.py. There is no cross-schema query:
the boundary is an HTTP call to the owning service.

The names come from an import file, which may hold up to the import route's own
bound (OPS-VALID-10), so they are sent in batches whose JSON body stays under
`RESOLVE_BATCH_BYTES`, half the smallest request body limit a service accepts
(issue #1466): no import can make inventory refuse a batch for its size. A name
longer than a device name may be (`DEVICE_NAME_MAX_LENGTH`), or one holding a NUL
character (which no database column takes, so no device name holds one), cannot name a
device, so it is not sent and stays unresolved.
"""

import json
import logging

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

# Inventory's devices.name column width (String(255)).
DEVICE_NAME_MAX_LENGTH = 255
# Half of herd_common.validation_errors.MIN_MAX_REQUEST_BODY_BYTES (64 KiB).
RESOLVE_BATCH_BYTES = 32 * 1024
# `{"names":[` and `]}`.
_ENVELOPE_BYTES = len(b'{"names":[]}')


def _encoded_length(name: str) -> int:
    """The bytes `name` takes in the batch body, as httpx encodes it, plus a comma."""
    return len(json.dumps(name, ensure_ascii=False).encode("utf-8")) + 1


def name_batches(names: list[str]) -> list[list[str]]:
    """The unique names that could name a device, sorted, in batches whose encoded
    body is at most `RESOLVE_BATCH_BYTES`."""
    unique = sorted(
        {n for n in names if n and len(n) <= DEVICE_NAME_MAX_LENGTH and "\x00" not in n}
    )
    batches: list[list[str]] = []
    current: list[str] = []
    size = _ENVELOPE_BYTES
    for name in unique:
        length = _encoded_length(name)
        if current and size + length > RESOLVE_BATCH_BYTES:
            batches.append(current)
            current, size = [], _ENVELOPE_BYTES
        current.append(name)
        size += length
    if current:
        batches.append(current)
    return batches


async def resolve_device_names(names: list[str]) -> dict[str, str]:
    """Return a name to device-id (string) map for the names that exist.

    Names with no matching device are omitted from the returned map, so the
    caller treats a missing key as an unresolved reference. Raises on transport
    failure so the import surfaces the inventory outage rather than silently
    dropping every device reference.
    """
    batches = name_batches(names)
    if not batches:
        return {}
    url = f"{settings.inventory_service_url.rstrip('/')}/devices/resolve-by-name"
    headers = {"X-Internal-Token": settings.internal_api_token}
    resolved: dict[str, str] = {}
    async with httpx.AsyncClient(timeout=10.0) as client:
        for batch in batches:
            resp = await client.post(url, json={"names": batch}, headers=headers)
            resp.raise_for_status()
            resolved.update(resp.json().get("resolved", {}))
    return resolved
