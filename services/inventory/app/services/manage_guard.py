"""Shared route guards for apply_jobs and device_configs.

Promoted from two byte-identical copies (app/routers/apply_jobs.py and
app/routers/device_configs.py). Authorization helpers bind this service's own
acl_service_url / reservations_service_url / internal_api_token settings, so
herd_common is the wrong home for those; herd_common.acl.
user_has_manage_or_owns_active_reservation is the service-agnostic helper they
wrap. `_assert_driver_can_configure` (issue #839) is a capability guard, not
an authorization one, but lives here for the same reason: both apply routes
need it and this is their shared home.
"""

import uuid

from fastapi import HTTPException
from herd_common.acl import user_has_manage_or_owns_active_reservation
from herd_common.device_config import connection_type_supports_configure

from app.config import settings
from app.models.device import Device
from app.services.published_schema import driver_for_device


def _is_admin(payload: dict) -> bool:
    return payload.get("role") in ("admin", "superadmin")


async def _user_can_manage_device(
    user_id: str,
    device_id: uuid.UUID,
    authorization: str | None,
) -> bool:
    """Wrap the shared herd-common helper with this service's URL config.

    Accepts an explicit `manage` grant OR reservation-owner-of-an-active-
    reservation-containing-this-device, per the iter-3 widening documented
    in docs/ROLES.md.
    """
    return await user_has_manage_or_owns_active_reservation(
        user_id=user_id,
        device_id=str(device_id),
        authorization=authorization,
        acl_service_url=settings.acl_service_url,
        reservations_service_url=settings.reservations_service_url,
        internal_api_token=settings.internal_api_token,
    )


def _assert_driver_can_configure(device: Device) -> None:
    """Refuse a config apply when the device's driver contract has no configure
    method (issue #839).

    Capability comes from the driver's connection type, not a declared flag or
    code inspection: only a Management driver's contract requires `configure`
    (herd_common.device_config.CONFIGURE_CONNECTION_TYPES, kept in parity with
    execution's REQUIRED_METHODS by a dedicated test). Called by both apply
    entry points (schedule_apply_job and apply_config_version) right after
    their authorization check succeeds and before any call to execution or job
    row is created, so an incapable driver is reported up front instead of
    surfacing only when the job runs.

    A device with NO resolvable driver is left alone here (no-op): that is a
    separate, pre-existing failure mode with its own behavior downstream, and
    this guard only judges a driver that IS present.
    """
    driver = driver_for_device(device)
    if driver is None:
        return
    if connection_type_supports_configure(driver.connection_type):
        return
    raise HTTPException(
        status_code=409,
        detail={
            "error": "driver_cannot_configure",
            "connection_type": driver.connection_type,
            "driver": driver.name,
            "message": (
                f"This device's driver implements the {driver.connection_type} "
                "contract, which has no configure method, so a config apply "
                "cannot run. Config versions on this device store intent only."
            ),
        },
    )
