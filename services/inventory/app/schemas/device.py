import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, field_validator

from app.models.device import DeviceStatus, TopologyType

# Minimum opt-in poll cadence. Matches the scheduler tick floor in the
# execution service: scheduling below this is meaningless because the tick
# rate would dominate.
MIN_POLL_INTERVAL_SECONDS = 30


def _validate_poll_interval(v: int | None) -> int | None:
    if v is None:
        return v
    if v < MIN_POLL_INTERVAL_SECONDS:
        raise ValueError(f"poll_interval_seconds must be >= {MIN_POLL_INTERVAL_SECONDS} or null")
    return v


class DeviceCreate(BaseModel):
    name: str
    template_id: uuid.UUID
    topology_type: TopologyType
    status: DeviceStatus = DeviceStatus.AVAILABLE
    field_data: dict[str, Any] = {}
    poll_interval_seconds: int | None = None

    @field_validator("poll_interval_seconds")
    @classmethod
    def _check_poll_interval(cls, v: int | None) -> int | None:
        return _validate_poll_interval(v)


class DeviceUpdate(BaseModel):
    name: str | None = None
    topology_type: TopologyType | None = None
    status: DeviceStatus | None = None
    field_data: dict[str, Any] | None = None
    poll_interval_seconds: int | None = None

    @field_validator("poll_interval_seconds")
    @classmethod
    def _check_poll_interval(cls, v: int | None) -> int | None:
        return _validate_poll_interval(v)


class DeviceResponse(BaseModel):
    id: uuid.UUID
    name: str
    template_id: uuid.UUID
    template_name: str | None = None
    template_icon: str | None = None
    template_vendor: str | None = None
    template_model: str | None = None
    template_part_number: str | None = None
    topology_type: TopologyType
    status: DeviceStatus
    field_data: dict[str, Any]
    driver_id: uuid.UUID | None = None
    driver_name: str | None = None
    connection_type: str | None = None
    exclusive: bool = True
    created_at: datetime
    updated_at: datetime
    created_by: uuid.UUID | None = None
    created_by_name: str | None = None
    modified_by: uuid.UUID | None = None
    modified_by_name: str | None = None
    poll_interval_seconds: int | None = None
    resolved_poll_interval_seconds: int | None = None

    model_config = {"from_attributes": True}


class PaginatedDeviceResponse(BaseModel):
    items: list[DeviceResponse]
    total: int
    skip: int
    limit: int
