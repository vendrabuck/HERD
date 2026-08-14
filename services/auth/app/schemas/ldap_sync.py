import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


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
