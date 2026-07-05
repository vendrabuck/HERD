import uuid
from datetime import datetime

from pydantic import BaseModel, field_validator


def _reject_blank(v: str | None, label: str) -> str | None:
    if v is None:
        return v
    if not v.strip():
        raise ValueError(f"{label} must not be blank or whitespace")
    return v


class HypervisorCreate(BaseModel):
    name: str
    description: str | None = None
    endpoint: str
    hypervisor_type: str
    secret_id: uuid.UUID
    enabled: bool = True

    @field_validator("name", "endpoint", "hypervisor_type")
    @classmethod
    def _not_blank(cls, v: str, info) -> str:
        if not v or not v.strip():
            raise ValueError(f"{info.field_name} must not be blank or whitespace")
        return v


class HypervisorUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    endpoint: str | None = None
    hypervisor_type: str | None = None
    secret_id: uuid.UUID | None = None
    enabled: bool | None = None

    @field_validator("name", "endpoint", "hypervisor_type")
    @classmethod
    def _not_blank(cls, v: str | None, info) -> str | None:
        return _reject_blank(v, info.field_name)


class HypervisorResponse(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None = None
    endpoint: str
    hypervisor_type: str
    secret_id: uuid.UUID
    enabled: bool
    created_at: datetime
    updated_at: datetime
    modified_by: uuid.UUID | None = None

    model_config = {"from_attributes": True}


class HypervisorInternalResponse(BaseModel):
    # Field set pinned by ADR 0004: exactly these six, consumed by execution.
    id: uuid.UUID
    name: str
    endpoint: str
    hypervisor_type: str
    secret_id: uuid.UUID
    enabled: bool

    model_config = {"from_attributes": True}


class PaginatedHypervisorResponse(BaseModel):
    items: list[HypervisorResponse]
    total: int
    skip: int
    limit: int
