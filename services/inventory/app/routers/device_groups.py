import logging
import uuid

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.dependencies.auth import get_current_user_payload, require_admin
from app.models.device import Device
from app.schemas.device_group import (
    BulkDeviceIds,
    BulkRemoveResult,
    BulkResult,
    BulkUserGroupIds,
    DeviceGroupCreate,
    DeviceGroupDetailResponse,
    DeviceGroupDeviceResponse,
    DeviceGroupMembershipResponse,
    DeviceGroupPermissionResponse,
    DeviceGroupResponse,
    DeviceGroupUpdate,
    PaginatedDeviceGroupResponse,
)
from app.services.device_group_service import (
    bulk_add_devices,
    bulk_add_user_groups,
    bulk_remove_devices,
    bulk_remove_user_groups,
    create_device_group,
    delete_device_group,
    get_device_group,
    get_device_groups_for_device,
    get_visible_device_ids,
    list_device_groups,
    update_device_group,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/device-groups", tags=["device-groups"])


def _group_to_response(
    group, device_count: int = 0, user_group_count: int = 0
) -> DeviceGroupResponse:
    return DeviceGroupResponse(
        id=group.id,
        name=group.name,
        description=group.description,
        created_by=group.created_by,
        created_at=group.created_at,
        updated_at=group.updated_at,
        modified_by=group.modified_by,
        device_count=device_count,
        user_group_count=user_group_count,
    )


def _group_to_response_loaded(group) -> DeviceGroupResponse:
    """Use when devices and permissions relationships are already loaded."""
    return DeviceGroupResponse(
        id=group.id,
        name=group.name,
        description=group.description,
        created_by=group.created_by,
        created_at=group.created_at,
        updated_at=group.updated_at,
        modified_by=group.modified_by,
        device_count=len(group.devices) if group.devices else 0,
        user_group_count=len(group.permissions) if group.permissions else 0,
    )


def _group_to_detail(group, device_map: dict | None = None) -> DeviceGroupDetailResponse:
    devices = []
    for dgd in group.devices or []:
        d = device_map.get(dgd.device_id) if device_map else None
        devices.append(
            DeviceGroupDeviceResponse(
                device_id=dgd.device_id,
                device_name=d.name if d else None,
                template_name=d.template.name if d and d.template else None,
                added_at=dgd.added_at,
            )
        )
    user_groups = [
        DeviceGroupPermissionResponse(
            user_group_id=p.user_group_id,
            assigned_by=p.assigned_by,
            assigned_at=p.assigned_at,
        )
        for p in (group.permissions or [])
    ]
    return DeviceGroupDetailResponse(
        id=group.id,
        name=group.name,
        description=group.description,
        created_by=group.created_by,
        created_at=group.created_at,
        updated_at=group.updated_at,
        modified_by=group.modified_by,
        device_count=len(devices),
        user_group_count=len(user_groups),
        devices=devices,
        user_groups=user_groups,
    )


@router.post("", response_model=DeviceGroupResponse, status_code=status.HTTP_201_CREATED)
async def create_device_group_endpoint(
    body: DeviceGroupCreate,
    db: AsyncSession = Depends(get_db),
    payload: dict = Depends(require_admin),
):
    user_id = uuid.UUID(payload["sub"])
    group = await create_device_group(db, body.name, body.description, user_id)
    logger.info(
        "Device group created: %s",
        group.name,
        extra={"action": "device_group_create", "group_id": str(group.id)},
    )
    return _group_to_response(group)


@router.get("", response_model=PaginatedDeviceGroupResponse)
async def list_device_groups_endpoint(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(require_admin),
):
    groups, total = await list_device_groups(db, skip=skip, limit=limit)
    return PaginatedDeviceGroupResponse(
        items=[_group_to_response_loaded(g) for g in groups],
        total=total,
        skip=skip,
        limit=limit,
    )


@router.get("/visible-devices")
async def get_visible_devices_endpoint(
    user_id: uuid.UUID = Query(...),
    authorization: str | None = Header(None),
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(get_current_user_payload),
):
    """Get visible device IDs for a user by resolving their group memberships."""
    user_group_ids = await _fetch_user_group_ids(user_id, authorization)
    visible = await get_visible_device_ids(db, user_group_ids)
    return {"device_ids": [str(d) for d in visible]}


@router.get("/device/{device_id}", response_model=list[DeviceGroupMembershipResponse])
async def get_device_groups_for_device_endpoint(
    device_id: uuid.UUID,
    authorization: str | None = Header(None),
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(get_current_user_payload),
):
    """Get all device groups a device belongs to, with user group names resolved."""
    groups = await get_device_groups_for_device(db, device_id)
    # Collect all unique user_group_ids to resolve names in a single call
    all_ug_ids: set[uuid.UUID] = set()
    for g in groups:
        for p in g.permissions or []:
            all_ug_ids.add(p.user_group_id)

    # Resolve user group names from auth service
    ug_name_map = await _fetch_user_group_names(list(all_ug_ids), authorization)

    result = []
    for g in groups:
        user_groups = [
            DeviceGroupPermissionResponse(
                user_group_id=p.user_group_id,
                user_group_name=ug_name_map.get(p.user_group_id),
                assigned_by=p.assigned_by,
                assigned_at=p.assigned_at,
            )
            for p in (g.permissions or [])
        ]
        result.append(
            DeviceGroupMembershipResponse(
                id=g.id,
                name=g.name,
                description=g.description,
                user_groups=user_groups,
            )
        )
    return result


@router.get("/{group_id}", response_model=DeviceGroupDetailResponse)
async def get_device_group_endpoint(
    group_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(require_admin),
):
    group = await get_device_group(db, group_id)
    if not group:
        raise HTTPException(status_code=404, detail="Device group not found")
    # Build device map for names
    from sqlalchemy import select

    device_ids = [dgd.device_id for dgd in group.devices]
    device_map = {}
    if device_ids:
        result = await db.execute(select(Device).where(Device.id.in_(device_ids)))
        for d in result.unique().scalars().all():
            device_map[d.id] = d
    return _group_to_detail(group, device_map)


@router.put("/{group_id}", response_model=DeviceGroupResponse)
async def update_device_group_endpoint(
    group_id: uuid.UUID,
    body: DeviceGroupUpdate,
    db: AsyncSession = Depends(get_db),
    payload: dict = Depends(require_admin),
):
    group = await get_device_group(db, group_id)
    if not group:
        raise HTTPException(status_code=404, detail="Device group not found")
    await update_device_group(
        db, group, body.name, body.description, modified_by=uuid.UUID(payload["sub"])
    )
    logger.info(
        "Device group updated: %s",
        group_id,
        extra={"action": "device_group_update", "group_id": str(group_id)},
    )
    # Re-fetch with relationships loaded
    reloaded = await get_device_group(db, group_id)
    return _group_to_response_loaded(reloaded)


@router.delete("/{group_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_device_group_endpoint(
    group_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    payload: dict = Depends(require_admin),
):
    group = await get_device_group(db, group_id)
    if not group:
        raise HTTPException(status_code=404, detail="Device group not found")
    await delete_device_group(db, group)
    logger.info(
        "Device group deleted: %s",
        group_id,
        extra={"action": "device_group_delete", "group_id": str(group_id)},
    )


@router.post("/{group_id}/devices/bulk", response_model=BulkResult)
async def bulk_add_devices_endpoint(
    group_id: uuid.UUID,
    body: BulkDeviceIds,
    db: AsyncSession = Depends(get_db),
    payload: dict = Depends(require_admin),
):
    group = await get_device_group(db, group_id)
    if not group:
        raise HTTPException(status_code=404, detail="Device group not found")
    added, skipped = await bulk_add_devices(db, group_id, body.device_ids)
    logger.info(
        "Devices added to group %s: %d added, %d skipped",
        group_id,
        added,
        skipped,
        extra={"action": "device_group_add_devices", "group_id": str(group_id)},
    )
    return BulkResult(added=added, skipped=skipped)


@router.post("/{group_id}/devices/bulk-remove", response_model=BulkRemoveResult)
async def bulk_remove_devices_endpoint(
    group_id: uuid.UUID,
    body: BulkDeviceIds,
    db: AsyncSession = Depends(get_db),
    payload: dict = Depends(require_admin),
):
    group = await get_device_group(db, group_id)
    if not group:
        raise HTTPException(status_code=404, detail="Device group not found")
    removed, not_found = await bulk_remove_devices(db, group_id, body.device_ids)
    logger.info(
        "Devices removed from group %s: %d removed, %d not found",
        group_id,
        removed,
        not_found,
        extra={"action": "device_group_remove_devices", "group_id": str(group_id)},
    )
    return BulkRemoveResult(removed=removed, not_found=not_found)


@router.post("/{group_id}/permissions/bulk", response_model=BulkResult)
async def bulk_add_user_groups_endpoint(
    group_id: uuid.UUID,
    body: BulkUserGroupIds,
    db: AsyncSession = Depends(get_db),
    payload: dict = Depends(require_admin),
):
    group = await get_device_group(db, group_id)
    if not group:
        raise HTTPException(status_code=404, detail="Device group not found")
    assigned_by = uuid.UUID(payload["sub"])
    added, skipped = await bulk_add_user_groups(db, group_id, body.user_group_ids, assigned_by)
    logger.info(
        "User groups assigned to device group %s: %d added, %d skipped",
        group_id,
        added,
        skipped,
        extra={"action": "device_group_add_permissions", "group_id": str(group_id)},
    )
    return BulkResult(added=added, skipped=skipped)


@router.post("/{group_id}/permissions/bulk-remove", response_model=BulkRemoveResult)
async def bulk_remove_user_groups_endpoint(
    group_id: uuid.UUID,
    body: BulkUserGroupIds,
    db: AsyncSession = Depends(get_db),
    payload: dict = Depends(require_admin),
):
    group = await get_device_group(db, group_id)
    if not group:
        raise HTTPException(status_code=404, detail="Device group not found")
    removed, not_found = await bulk_remove_user_groups(db, group_id, body.user_group_ids)
    logger.info(
        "User groups removed from device group %s: %d removed, %d not found",
        group_id,
        removed,
        not_found,
        extra={"action": "device_group_remove_permissions", "group_id": str(group_id)},
    )
    return BulkRemoveResult(removed=removed, not_found=not_found)


def _auth_base() -> str:
    return settings.auth_service_url.rstrip("/")


async def _fetch_user_group_ids(user_id: uuid.UUID, authorization: str | None) -> list[uuid.UUID]:
    """Fetch user's group IDs from the auth service, forwarding the caller JWT.

    Raises HTTPException(503) on upstream failure. Callers that want fail-open
    behavior (e.g. show-all-devices on auth outage) must wrap this call in
    their own try/except.

    A None authorization is a contract violation (the bearer dep on each route
    guarantees it's present) and raises HTTPException(500).
    """
    if authorization is None:
        raise HTTPException(
            status_code=500,
            detail="internal: missing Authorization header while resolving user groups",
        )
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{_auth_base()}/groups/user/{user_id}",
                headers={"Authorization": authorization},
                timeout=10.0,
            )
    except httpx.HTTPError as exc:
        logger.error("auth service unreachable while fetching groups for user %s: %s", user_id, exc)
        raise HTTPException(
            status_code=503, detail="auth service unreachable while fetching user groups"
        ) from exc

    if resp.status_code != 200:
        logger.error(
            "auth service returned %s for user %s groups: %s",
            resp.status_code,
            user_id,
            resp.text[:200],
        )
        raise HTTPException(
            status_code=503,
            detail=f"auth service returned {resp.status_code} when fetching user groups",
        )

    groups = resp.json()
    return [uuid.UUID(g["id"]) for g in groups]


async def _fetch_user_group_names(
    user_group_ids: list[uuid.UUID],
    authorization: str | None,
) -> dict[uuid.UUID, str]:
    """Fetch user group names from the auth service. Returns a map of id to name.

    Raises HTTPException(503) on upstream failure. A None authorization raises
    HTTPException(500); the bearer dep on each route guarantees it's present.
    """
    if not user_group_ids:
        return {}
    if authorization is None:
        raise HTTPException(
            status_code=500,
            detail="internal: missing Authorization header while resolving group names",
        )
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{_auth_base()}/groups",
                headers={"Authorization": authorization},
                timeout=10.0,
            )
    except httpx.HTTPError as exc:
        logger.error("auth service unreachable while fetching group names: %s", exc)
        raise HTTPException(
            status_code=503, detail="auth service unreachable while fetching group names"
        ) from exc

    if resp.status_code != 200:
        logger.error(
            "auth service returned %s for group names: %s",
            resp.status_code,
            resp.text[:200],
        )
        raise HTTPException(
            status_code=503,
            detail=f"auth service returned {resp.status_code} when fetching group names",
        )

    data = resp.json()
    groups = data.get("items", data) if isinstance(data, dict) else data
    wanted = set(user_group_ids)
    name_map: dict[uuid.UUID, str] = {}
    for g in groups:
        gid = uuid.UUID(g["id"])
        if gid in wanted:
            name_map[gid] = g["name"]
    return name_map
