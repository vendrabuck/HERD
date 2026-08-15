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
from app.models.ldap_group_mapping import DIRECTORY_NAME_MAX, LdapGroupMapping
from app.models.ldap_sync_run import LdapSyncRun
from app.models.user import Role, User
from app.schemas.ldap_sync import (
    LdapSyncStatusResponse,
    MappingCreateRequest,
    MappingCreateResponse,
    MappingResponse,
    PaginatedMappingResponse,
    PaginatedSyncRunResponse,
    SyncRunResponse,
    SyncRunStartResponse,
)
from app.services import ldap_service, ldap_sync_service
from app.services.group_service import get_group_by_id

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/ldap-sync", tags=["ldap-sync"])

_admin_or_superadmin = Depends(require_role(Role.ADMIN, Role.SUPERADMIN))

NO_MEMBERS_WARNING = (
    "The directory entry reports no members. If this is an empty group that "
    "is expected; if not, the DN may point at a non-group entry and the sync "
    "would treat it as having no members."
)
_DUPLICATE_DN_DETAIL = "A mapping for this group_dn already exists"
_GROUP_ALREADY_MAPPED_DETAIL = "This HERD group already has a directory mapping"


async def _conflicting_mapping_detail(db: AsyncSession, group_dn: str, herd_group_id) -> str | None:
    """Which unique constraint would (or did) a create violate, if any."""
    dn_taken = (
        await db.execute(select(LdapGroupMapping.id).where(LdapGroupMapping.group_dn == group_dn))
    ).scalar_one_or_none()
    if dn_taken is not None:
        return _DUPLICATE_DN_DETAIL
    group_taken = (
        await db.execute(
            select(LdapGroupMapping.id).where(LdapGroupMapping.herd_group_id == herd_group_id)
        )
    ).scalar_one_or_none()
    if group_taken is not None:
        return _GROUP_ALREADY_MAPPED_DETAIL
    return None


@router.get("/status", response_model=LdapSyncStatusResponse)
async def get_sync_status(
    _: User = _admin_or_superadmin,
):
    """Mode context for the phase 6 admin UI: current auth backend plus the
    interval-loop settings, so the page can gate create/sync-now on
    auth_method == "ldap" and show honest context when the loop is off.
    """
    return LdapSyncStatusResponse(
        auth_method=settings.auth_method,
        group_sync_enabled=settings.ldap_group_sync_enabled,
        sync_interval_seconds=settings.ldap_sync_interval_seconds,
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
    # Answer the common conflicts from the indexed unique columns before
    # paying the directory round-trip (a duplicate against a degraded
    # directory would otherwise take the full connect+bind timeouts, or
    # even 503). The unique constraints below remain the race backstop.
    conflict = await _conflicting_mapping_detail(db, body.group_dn, body.herd_group_id)
    if conflict is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=conflict)
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
        # Store the canonical DN the directory returned, not the admin-typed
        # form: DNs are case-insensitive, so persisting the raw input would
        # let case variants of one directory group slip past the unique
        # constraint and map it twice.
        group_dn=entry.dn,
        directory_name=entry.name[:DIRECTORY_NAME_MAX],
        herd_group_id=body.herd_group_id,
        created_by=current_user.id,
    )
    db.add(mapping)
    try:
        await db.commit()
    except IntegrityError:
        # Disambiguate: the canonical DN or the group may have been claimed
        # by a concurrent create, or the HERD group (or acting user) deleted
        # while the directory round-trip ran. A blanket duplicate message
        # would misreport those.
        await db.rollback()
        if await get_group_by_id(db, body.herd_group_id) is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="HERD group not found",
            )
        conflict = await _conflicting_mapping_detail(db, entry.dn, body.herd_group_id)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=conflict or "Mapping conflicts with concurrent changes; retry",
        )
    warning = None if entry.member_dns else NO_MEMBERS_WARNING
    logger.info(
        "LDAP group mapping created: %s -> %s by %s",
        entry.dn,
        group.name,
        current_user.username,
        extra={
            "action": "ldap_mapping_create",
            "mapping_id": str(mapping.id),
            "group_dn": entry.dn,
            "herd_group_id": str(body.herd_group_id),
            "member_count": len(entry.member_dns),
        },
    )
    response = MappingCreateResponse.model_validate(mapping)
    response.warning = warning
    return response


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
                # id tiebreaker: equal timestamps otherwise make skip/limit
                # pages nondeterministic (rows skipped or duplicated).
                .order_by(LdapGroupMapping.created_at, LdapGroupMapping.id)
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


# ---------------------------------------------------------------------------
# Sync-now (ADR 0011 phase 3). Run serialization itself (the in-process
# asyncio.Lock plus the Postgres advisory-lock cross-replica layer) lives in
# ldap_sync_service (S1): it is a run invariant, not an HTTP concern, and the
# phase 5 interval loop (app/tasks/ldap_sync_loop.py) shares it without a
# second lock. This router only maps ldap_sync_service.SyncBusyError to the
# two existing 409s.
# ---------------------------------------------------------------------------

_RUN_IN_PROGRESS_DETAIL = "A sync run is already in progress"
_RUN_IN_PROGRESS_REPLICA_DETAIL = "A sync run is already in progress on another replica"


@router.post("/run", response_model=SyncRunStartResponse, status_code=status.HTTP_202_ACCEPTED)
async def start_sync_run(
    current_user: User = _admin_or_superadmin,
):
    if settings.auth_method != "ldap":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Directory sync requires auth_method=ldap",
        )
    try:
        run_id = await ldap_sync_service.start_background_run(trigger="manual")
    except ldap_sync_service.SyncBusyError as exc:
        detail = (
            _RUN_IN_PROGRESS_DETAIL
            if exc.reason == "in_process"
            else _RUN_IN_PROGRESS_REPLICA_DETAIL
        )
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)
    logger.info(
        "LDAP sync run accepted: %s by %s",
        run_id,
        current_user.username,
        extra={"action": "ldap_sync_run_accepted", "run_id": str(run_id)},
    )
    return SyncRunStartResponse(run_id=run_id)


@router.get("/runs", response_model=PaginatedSyncRunResponse)
async def list_sync_runs(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    _: User = _admin_or_superadmin,
):
    total = (await db.execute(select(func.count()).select_from(LdapSyncRun))).scalar_one()
    rows = (
        (
            await db.execute(
                select(LdapSyncRun)
                # Newest first; id tiebreaker keeps equal timestamps from
                # making skip/limit pages nondeterministic.
                .order_by(LdapSyncRun.started_at.desc(), LdapSyncRun.id.desc())
                .offset(skip)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return PaginatedSyncRunResponse(
        items=[SyncRunResponse.model_validate(r) for r in rows],
        total=total,
        skip=skip,
        limit=limit,
    )


@router.get("/runs/{run_id}", response_model=SyncRunResponse)
async def get_sync_run(
    run_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: User = _admin_or_superadmin,
):
    run = await db.get(LdapSyncRun, run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sync run not found")
    return SyncRunResponse.model_validate(run)
