"""Device-manage authorization helpers shared by apply_jobs and device_configs.

Promoted from two byte-identical copies (app/routers/apply_jobs.py and
app/routers/device_configs.py). These bind this service's own acl_service_url /
reservations_service_url / internal_api_token settings, so herd_common is the
wrong home; herd_common.acl.user_has_manage_or_owns_active_reservation is the
service-agnostic helper this wraps.
"""

import uuid

from herd_common.acl import user_has_manage_or_owns_active_reservation

from app.config import settings


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
