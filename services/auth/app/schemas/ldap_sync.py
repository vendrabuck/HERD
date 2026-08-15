import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class LdapSyncStatusResponse(BaseModel):
    # Drives the phase 6 admin UI's mode gating (ADR 0011): the page reads
    # this once on load to decide whether mapping-create and sync-now should
    # be enabled, and to show the configured interval when the background
    # loop is on. Deliberately minimal: extends the ADR's ratified
    # {auth_method} shape with the two settings the page needs for honest
    # interval-loop context, nothing else.
    auth_method: Literal["local", "ldap"]
    group_sync_enabled: bool
    sync_interval_seconds: int


class MappingCreateRequest(BaseModel):
    group_dn: str = Field(min_length=1, max_length=2000)
    herd_group_id: uuid.UUID


class MappingResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    group_dn: str
    directory_name: str
    herd_group_id: uuid.UUID
    created_by: uuid.UUID | None
    created_at: datetime


class MappingCreateResponse(MappingResponse):
    # Set when the validated directory entry lacks the member attribute:
    # either an AD-style empty group (fine) or a non-group entry the DN was
    # typo'd onto (the thing the admin should double-check). Decision with
    # vendra 2026-08-12: accept with warning, never refuse.
    warning: str | None = None


class PaginatedMappingResponse(BaseModel):
    items: list[MappingResponse]
    total: int
    skip: int
    limit: int


class SyncRunResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    started_at: datetime
    finished_at: datetime | None
    trigger: str
    status: str
    users_provisioned: int
    members_added: int
    members_removed: int
    members_skipped: int
    users_deactivated: int
    users_reactivated: int
    detail: dict
    error: str | None


class PaginatedSyncRunResponse(BaseModel):
    items: list[SyncRunResponse]
    total: int
    skip: int
    limit: int


class SyncRunStartResponse(BaseModel):
    run_id: uuid.UUID
