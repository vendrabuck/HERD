"""Admin CRUD for directory-group mappings (ADR 0011 phase 2).

Mapping creation validates the DN against the live directory, keeping the
distinguishes-not-found-from-cannot-ask convention: a DN the directory
proves unresolvable is a 422 (the admin typo'd or the group is gone), a
directory that cannot be asked is a 503 (nothing was proven; try again).
A resolvable entry that lacks the member attribute is ACCEPTED with a
warning in the response (decision 2026-08-12): AD models empty groups
that way, so refusal would block legitimate mappings, while the warning
surfaces the non-group-typo case the reconciler would otherwise read as
an empty desired set.

Creation requires auth_method=ldap (validation needs a directory to ask).
Listing and deletion work in any mode so stale mappings can always be
cleaned up.
"""

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.dependencies.auth import require_role
from app.models.ldap_group_mapping import LdapGroupMapping
from app.models.user import Role, User
from app.schemas.ldap_sync import (
    MappingCreateRequest,
    MappingCreateResponse,
    MappingResponse,
    PaginatedMappingResponse,
)
from app.services import ldap_service
from app.services.group_service import get_group_by_id

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/ldap-sync", tags=["ldap-sync"])

_admin_or_superadmin = Depends(require_role(Role.ADMIN, Role.SUPERADMIN))

MISSING_MEMBER_ATTRIBUTE_WARNING = (
    "The directory entry has no member attribute. If this is an empty group "
    "that is expected; if not, the DN may point at a non-group entry and the "
    "sync would treat it as having no members."
)


@router.post("/mappings", response_model=MappingCreateResponse, status_code=status.HTTP_201_CREATED)
async def create_mapping(
    body: MappingCreateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = _admin_or_superadmin,
):
    if settings.auth_method != "ldap":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Directory mappings require auth_method=ldap",
        )
    group = await get_group_by_id(db, body.herd_group_id)
    if group is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="HERD group not found",
        )
    try:
        entry = await ldap_service.fetch_group(body.group_dn)
    except ldap_service.LdapUnavailableError as exc:
        # Nothing was proven; do not let an outage read as a bad DN.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Directory unavailable, mapping not validated: {exc}",
        )
    if entry is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="group_dn does not resolve in the directory",
        )
    mapping = LdapGroupMapping(
        group_dn=body.group_dn,
        directory_name=entry.name,
        herd_group_id=body.herd_group_id,
        created_by=current_user.id,
    )
    db.add(mapping)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A mapping for this group_dn already exists",
        )
    await db.refresh(mapping)
    warning = None if entry.member_attribute_present else MISSING_MEMBER_ATTRIBUTE_WARNING
    logger.info(
        "LDAP group mapping created: %s -> %s by %s",
        body.group_dn,
        group.name,
        current_user.username,
        extra={
            "action": "ldap_mapping_create",
            "mapping_id": str(mapping.id),
            "group_dn": body.group_dn,
            "herd_group_id": str(body.herd_group_id),
            "member_attribute_present": entry.member_attribute_present,
        },
    )
    return MappingCreateResponse(
        id=mapping.id,
        group_dn=mapping.group_dn,
        directory_name=mapping.directory_name,
        herd_group_id=mapping.herd_group_id,
        created_by=mapping.created_by,
        created_at=mapping.created_at,
        warning=warning,
    )


@router.get("/mappings", response_model=PaginatedMappingResponse)
async def list_mappings(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    _: User = _admin_or_superadmin,
):
    total = (await db.execute(select(func.count()).select_from(LdapGroupMapping))).scalar_one()
    rows = (
        (
            await db.execute(
                select(LdapGroupMapping)
                .order_by(LdapGroupMapping.created_at)
                .offset(skip)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return PaginatedMappingResponse(
        items=[MappingResponse.model_validate(r) for r in rows],
        total=total,
        skip=skip,
        limit=limit,
    )


@router.delete("/mappings/{mapping_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_mapping(
    mapping_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = _admin_or_superadmin,
):
    mapping = (
        await db.execute(select(LdapGroupMapping).where(LdapGroupMapping.id == mapping_id))
    ).scalar_one_or_none()
    if mapping is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Mapping not found")
    await db.delete(mapping)
    await db.commit()
    logger.info(
        "LDAP group mapping deleted: %s by %s",
        mapping.group_dn,
        current_user.username,
        extra={"action": "ldap_mapping_delete", "mapping_id": str(mapping_id)},
    )
    return None
