"""Non-admin device-visibility resolution and read gating, shared by devices,
ports, device_groups, apply_jobs, and device_configs routers.

Promoted out of app/routers/devices.py (issue #718) so device_configs.py's
config-version reads can apply the same group-visibility gate as the device
and port reads, without duplicating the visibility query or importing
router-to-router. `check_device_read_visibility` (issue #909) generalizes
device_configs.py's former private `_check_read_visibility` into a single
helper so device_groups.py and apply_jobs.py can apply the identical gate
instead of each carrying their own copy.
"""

import uuid

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.manage_guard import _is_admin


async def _resolve_visible_device_ids(
    db: AsyncSession, user_id: uuid.UUID, authorization: str | None
) -> set[uuid.UUID]:
    """Resolve the set of device IDs a non-admin user can see via their groups.

    Fails closed: if the auth service is unreachable or errors,
    `_fetch_user_group_ids` raises HTTPException(503) and that propagates so the
    request fails rather than falling back to showing every device. A user who
    legitimately belongs to no groups resolves to an empty set (sees no DUTs),
    which is distinct from an auth-service outage.
    """
    from app.routers.device_groups import _fetch_user_group_ids
    from app.services.device_group_service import get_visible_device_ids

    user_group_ids = await _fetch_user_group_ids(user_id, authorization)
    return await get_visible_device_ids(db, user_group_ids)


async def check_device_read_visibility(
    db: AsyncSession,
    device_id: uuid.UUID,
    payload: dict,
    authorization: str | None,
    *,
    not_found_detail: str = "Device not found",
) -> None:
    """Gate a device-scoped read behind the same group visibility as the
    device and port reads (issue #718): a non-admin caller outside the
    device's groups gets 404, identical status and detail to the caller's own
    unknown-device 404, so the endpoint cannot be used to tell "hidden" from
    "absent". Admins are unfiltered, matching the device read.

    `not_found_detail` lets each call site reuse ITS OWN unknown-id 404
    wording (issue #909): most routes use the plain "Device not found", but
    `GET /device-groups/device/{id}` embeds the device id in its 404 (issue
    #392, pinned by test_device_groups.py) and `GET /apply-jobs/{id}` answers
    by job id rather than device id, so the hidden case must reuse each
    route's existing phrasing verbatim rather than introducing a second one.

    Deliberately plain visibility, not manage_guard's reservation-widened
    check: a user can only book devices they can already see, so visibility
    is the right boundary for a read.
    """
    if _is_admin(payload):
        return
    try:
        user_id = uuid.UUID(payload["sub"])
    except (KeyError, ValueError):
        raise HTTPException(status_code=404, detail=not_found_detail) from None
    visible_ids = await _resolve_visible_device_ids(db, user_id, authorization)
    if device_id not in visible_ids:
        raise HTTPException(status_code=404, detail=not_found_detail)
