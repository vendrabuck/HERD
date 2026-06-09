"""Resolve a device's driver-published config schema by proxying execution.

The execution service captures each driver's config_schema() once per SHA256
(issue #23). Inventory asks for it over the internal-token endpoint and uses it
to validate config-version writes, falling back to the hardcoded registry in
herd_common.device_config when the driver published nothing or execution is
unreachable.

Design choices (see docs/design/0002-driver-published-config-schemas.md):
  - No second persistent cache. Execution already caches by SHA256. We add only
    a tiny in-process TTL memo keyed by (driver_id, sha256) to coalesce bursts;
    the key includes the SHA256 so replacing a driver file can never serve a
    stale schema.
  - Fail-open. Any error (execution down, non-2xx, malformed payload) returns
    None so the caller degrades to the registry rather than 503-ing a config
    write.
"""

from __future__ import annotations

import logging
import time

import httpx

from app.config import settings
from app.models.device import Device
from app.models.driver_package import DriverPackage

logger = logging.getLogger(__name__)

# Short in-process memo: {(driver_id, sha256): (expires_at, schema_or_None)}.
# Keyed by SHA256 so a driver-file replace invalidates implicitly.
_MEMO: dict[tuple[str, str], tuple[float, dict | None]] = {}
_MEMO_TTL_SECONDS = 30.0


def _driver_for_device(device: Device) -> DriverPackage | None:
    template = device.template
    return template.driver if template else None


async def _fetch_published_schema(driver: DriverPackage) -> dict | None:
    """Proxy execution's /drivers/{id}/config-schema. Returns the schema or None.

    None means "no published schema; use the registry", which also covers every
    failure mode (network error, non-2xx, malformed payload, has_schema=False).
    """
    cache_key = (str(driver.id), driver.sha256)
    now = time.monotonic()
    memo = _MEMO.get(cache_key)
    if memo is not None and memo[0] > now:
        return memo[1]

    url = f"{settings.execution_service_url.rstrip('/')}/drivers/{driver.id}/config-schema"
    params = {
        "sha256": driver.sha256,
        "filename": driver.filename,
        "connection_type": driver.connection_type,
    }
    headers = {"X-Internal-Token": settings.internal_api_token}
    schema: dict | None = None
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, params=params, headers=headers)
        if resp.status_code == 200:
            body = resp.json()
            if body.get("has_schema") and isinstance(body.get("schema"), dict):
                schema = body["schema"]
        else:
            logger.warning(
                "execution config-schema returned %s for driver %s; using registry",
                resp.status_code,
                driver.id,
            )
    except (httpx.HTTPError, ValueError) as exc:
        # Fail-open: a transient execution outage must not block config authoring.
        logger.warning(
            "could not resolve published schema for driver %s (%s); using registry",
            driver.id,
            exc,
        )
        schema = None

    _MEMO[cache_key] = (now + _MEMO_TTL_SECONDS, schema)
    return schema


async def published_schema_for_device(device: Device) -> dict | None:
    """Return the device driver's published config schema, or None for fallback.

    None signals the caller to validate against the hardcoded registry. This is
    the single resolution point shared by the create/restore write paths and the
    admin-readable proxy route.
    """
    driver = _driver_for_device(device)
    if driver is None:
        return None
    return await _fetch_published_schema(driver)


async def published_schema_for_driver(driver: DriverPackage) -> dict | None:
    """Return a driver package's published config schema, or None for fallback."""
    return await _fetch_published_schema(driver)


def _invalidate_memo() -> None:
    """Test hook: clear the in-process memo."""
    _MEMO.clear()
