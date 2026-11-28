import uuid
from datetime import datetime

from pydantic import BaseModel, field_validator

from app.schemas.column_bounds import ColumnText, column_string

# The hypervisors columns' widths (app/models/hypervisor.py).
HYPERVISOR_NAME_MAX_LENGTH = 255
HYPERVISOR_ENDPOINT_MAX_LENGTH = 512
HYPERVISOR_TYPE_MAX_LENGTH = 50
HypervisorName = column_string(HYPERVISOR_NAME_MAX_LENGTH)
HypervisorEndpoint = column_string(HYPERVISOR_ENDPOINT_MAX_LENGTH)
HypervisorType = column_string(HYPERVISOR_TYPE_MAX_LENGTH)


def _reject_blank(v: str | None, label: str) -> str | None:
    if v is None:
        return v
    if not v.strip():
        raise ValueError(f"{label} must not be blank or whitespace")
    return v


class HypervisorCreate(BaseModel):
    name: HypervisorName
    description: ColumnText | None = None
    endpoint: HypervisorEndpoint
    hypervisor_type: HypervisorType
    secret_id: uuid.UUID
    enabled: bool = True
    device_group_id: uuid.UUID | None = None

    @field_validator("name", "endpoint", "hypervisor_type")
    @classmethod
    def _not_blank(cls, v: str, info) -> str:
        if not v or not v.strip():
            raise ValueError(f"{info.field_name} must not be blank or whitespace")
        return v


class HypervisorUpdate(BaseModel):
    name: HypervisorName | None = None
    description: ColumnText | None = None
    endpoint: HypervisorEndpoint | None = None
    hypervisor_type: HypervisorType | None = None
    secret_id: uuid.UUID | None = None
    enabled: bool | None = None
    device_group_id: uuid.UUID | None = None

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
    device_group_id: uuid.UUID | None = None
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


class HypervisorSecretRefResponse(BaseModel):
    # Issue #456: the by-secret reverse lookup returns exactly this pair; the
    # secrets delete guard needs the ids, the names make its 409 actionable.
    id: uuid.UUID
    name: str

    model_config = {"from_attributes": True}


class PaginatedHypervisorResponse(BaseModel):
    items: list[HypervisorResponse]
    total: int
    skip: int
    limit: int
