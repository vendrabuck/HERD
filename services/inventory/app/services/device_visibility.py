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

import logging
import uuid

import httpx
from fastapi import HTTPException
from herd_common.internal_client import InternalTokenAuth, call_service
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.device import Device
from app.models.hypervisor import Hypervisor
from app.models.template import DeviceTemplate
from app.services.manage_guard import _is_admin

logger = logging.getLogger(__name__)

# Timeout for the reservations lookup behind the instance-device grant.
_HELD_DEVICES_TIMEOUT_SECONDS = 5.0


async def _resolve_visible_device_ids(
    db: AsyncSession, user_id: uuid.UUID, authorization: str | None
) -> set[uuid.UUID]:
    """Resolve the set of device IDs a non-admin user can see via their groups.

    Fails closed: if the auth service is unreachable or errors,
    `_fetch_user_group_ids` raises HTTPException(503) and that propagates so the
    request fails rather than falling back to showing every device. A user who
    legitimately belongs to no groups resolves to an empty set (sees no DUTs),
    which is distinct from an auth-service outage.

    The result also carries the instance devices the user's own live
    reservations hold (issue #1030, `_instance_devices_held_by`), so every
    device read gated through this function shows a non-admin owner the device
    that represents their dynamic instance.
    """
    from app.routers.device_groups import _fetch_user_group_ids
    from app.services.device_group_service import get_visible_device_ids

    user_group_ids = await _fetch_user_group_ids(user_id, authorization)
    visible = await get_visible_device_ids(db, user_group_ids)
    return visible | await _instance_devices_held_by(db, user_id, visible)


async def _fetch_held_device_ids(user_id: uuid.UUID) -> set[uuid.UUID]:
    """Ask reservations which devices the user's live reservations hold (issue #1030).

    Reads reservations' internal `GET /internal/held-devices` with the internal
    token. Any failure (no token configured, a transport error, a non-200, a
    body that is not `{"device_ids": [<uuid str>, ...]}`) returns an empty set
    and logs `instance_device_grant_unavailable`: the grant only ever WIDENS
    group visibility, so a lookup that cannot be answered narrows to plain group
    visibility, never past it.
    """
    if not settings.internal_api_token:
        logger.warning(
            "No internal token configured; the instance-device grant is skipped",
            extra={"action": "instance_device_grant_unavailable", "reason": "no_token"},
        )
        return set()
    try:
        resp = await call_service(
            settings.reservations_service_url.rstrip("/"),
            "GET",
            "/internal/held-devices",
            params={"user_id": str(user_id)},
            timeout=_HELD_DEVICES_TIMEOUT_SECONDS,
            auth=InternalTokenAuth(token=settings.internal_api_token),
        )
    except httpx.HTTPError:
        logger.warning(
            "Reservations unreachable; the instance-device grant is skipped",
            extra={"action": "instance_device_grant_unavailable", "reason": "transport"},
        )
        return set()
    if resp.status_code != 200:
        logger.warning(
            "Reservations answered %s; the instance-device grant is skipped",
            resp.status_code,
            extra={"action": "instance_device_grant_unavailable", "reason": "status"},
        )
        return set()
    try:
        ids = resp.json().get("device_ids")
        return {uuid.UUID(i) for i in ids}
    except (ValueError, AttributeError, TypeError):
        logger.warning(
            "Reservations answered a misshapen body; the instance-device grant is skipped",
            extra={"action": "instance_device_grant_unavailable", "reason": "body"},
        )
        return set()


async def _instance_devices_held_by(
    db: AsyncSession, user_id: uuid.UUID, already_visible: set[uuid.UUID]
) -> set[uuid.UUID]:
    """The instance devices a non-admin sees through their own reservation (issue #1030).

    An instance device (the inventory device a dynamic instance is materialized
    as, the only kind of device that carries a `request_id`) joins the "No
    Pool" group, which a non-admin usually has no permission on. Visibility of
    such a device is granted through the reservation that holds it: a device is
    added when one of the user's PENDING_PROVISION or ACTIVE reservations holds
    it. Physical devices are never granted this way; their visibility stays
    group membership only.

    Reservations is asked only when an instance device exists that group
    visibility does not already cover, so the common case costs one local
    query and no HTTP call.
    """
    result = await db.execute(select(Device.id).where(Device.request_id.is_not(None)))
    candidates = {row[0] for row in result.all()} - already_visible
    if not candidates:
        return set()
    return candidates & await _fetch_held_device_ids(user_id)


async def resolve_visible_dynamic_hypervisor_ids(
    db: AsyncSession, payload: dict, authorization: str | None
) -> set[uuid.UUID] | None:
    """The hypervisors whose dynamic templates the caller may see (issue #1053).

    None for an admin (no filter). For anyone else, the hypervisors whose
    `device_group_id` is a device group one of the caller's user groups holds a
    permission on: the same DeviceGroupPermission rows that grant physical
    devices. A hypervisor with no device group is in nobody's set. Fails
    closed like device visibility: an auth outage raises the 503 of
    `_fetch_user_group_ids`, and a malformed subject resolves to the empty set.
    """
    if _is_admin(payload):
        return None
    try:
        user_id = uuid.UUID(payload["sub"])
    except (KeyError, ValueError):
        return set()
    from app.routers.device_groups import _fetch_user_group_ids
    from app.services.device_group_service import get_permitted_device_group_ids

    user_group_ids = await _fetch_user_group_ids(user_id, authorization)
    group_ids = await get_permitted_device_group_ids(db, user_group_ids)
    if not group_ids:
        return set()
    result = await db.execute(
        select(Hypervisor.id).where(Hypervisor.device_group_id.in_(group_ids))
    )
    return {row[0] for row in result.all()}


async def dynamic_templates_exist(db: AsyncSession) -> bool:
    """Whether any dynamic template exists, so a template listing knows it must gate."""
    result = await db.execute(
        select(DeviceTemplate.id).where(DeviceTemplate.template_type == "dynamic").limit(1)
    )
    return result.first() is not None


def dynamic_template_visible(template, visible_hypervisor_ids: set[uuid.UUID] | None) -> bool:
    """Is this template visible under the dynamic-template gate (issue #1053)?

    Only dynamic templates are gated; every other template is visible to every
    signed-in user as before. None means an admin (everything visible).
    """
    if visible_hypervisor_ids is None or template.template_type != "dynamic":
        return True
    return template.hypervisor_id in visible_hypervisor_ids


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
