"""Non-admin device-visibility resolution, shared by devices, ports, and
device_configs routers.

Promoted out of app/routers/devices.py (issue #718) so device_configs.py's
config-version reads can apply the same group-visibility gate as the device
and port reads, without duplicating the visibility query or importing
router-to-router.
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession


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
