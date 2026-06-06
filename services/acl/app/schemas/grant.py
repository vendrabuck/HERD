import uuid
from datetime import datetime

from pydantic import BaseModel, field_validator

VALID_RESOURCE_TYPES = {"device", "topology", "reservation"}
VALID_PERMISSIONS = {"view", "manage"}


class GrantCreateRequest(BaseModel):
    group_id: uuid.UUID
    resource_type: str
    resource_id: uuid.UUID
    permission: str

    @field_validator("resource_type")
    @classmethod
    def validate_resource_type(cls, v: str) -> str:
        if v not in VALID_RESOURCE_TYPES:
            opts = ", ".join(sorted(VALID_RESOURCE_TYPES))
            raise ValueError(f"resource_type must be one of: {opts}")
        return v

    @field_validator("permission")
    @classmethod
    def validate_permission(cls, v: str) -> str:
        if v not in VALID_PERMISSIONS:
            raise ValueError(f"permission must be one of: {', '.join(sorted(VALID_PERMISSIONS))}")
        return v


class GrantResponse(BaseModel):
    id: uuid.UUID
    group_id: uuid.UUID
    resource_type: str
    resource_id: uuid.UUID
    permission: str
    granted_by: uuid.UUID | None
    granted_at: datetime

    model_config = {"from_attributes": True}


class CheckRequest(BaseModel):
    user_id: uuid.UUID
    resource_type: str
    resource_id: uuid.UUID
    permission: str

    @field_validator("resource_type")
    @classmethod
    def validate_resource_type(cls, v: str) -> str:
        if v not in VALID_RESOURCE_TYPES:
            opts = ", ".join(sorted(VALID_RESOURCE_TYPES))
            raise ValueError(f"resource_type must be one of: {opts}")
        return v

    @field_validator("permission")
    @classmethod
    def validate_permission(cls, v: str) -> str:
        if v not in VALID_PERMISSIONS:
            raise ValueError(f"permission must be one of: {', '.join(sorted(VALID_PERMISSIONS))}")
        return v


class CheckResponse(BaseModel):
    allowed: bool
    grants: list[GrantResponse] = []


class BatchCheckRequest(BaseModel):
    user_id: uuid.UUID
    resource_type: str
    resource_ids: list[uuid.UUID]
    permission: str

    @field_validator("resource_type")
    @classmethod
    def validate_resource_type(cls, v: str) -> str:
        if v not in VALID_RESOURCE_TYPES:
            opts = ", ".join(sorted(VALID_RESOURCE_TYPES))
            raise ValueError(f"resource_type must be one of: {opts}")
        return v

    @field_validator("permission")
    @classmethod
    def validate_permission(cls, v: str) -> str:
        if v not in VALID_PERMISSIONS:
            raise ValueError(f"permission must be one of: {', '.join(sorted(VALID_PERMISSIONS))}")
        return v


class BatchCheckResponse(BaseModel):
    results: dict[str, bool]


class ResourceRef(BaseModel):
    resource_type: str
    resource_id: uuid.UUID


class PaginatedGrantResponse(BaseModel):
    items: list[GrantResponse]
    total: int
    skip: int
    limit: int
