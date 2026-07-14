import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator

from app.models.device import DeviceStatus, TopologyType

# Minimum opt-in poll cadence. Matches the scheduler tick floor in the
# execution service: scheduling below this is meaningless because the tick
# rate would dominate.
MIN_POLL_INTERVAL_SECONDS = 30

# Matches the devices.name column width (String(255), app/models/device.py).
DEVICE_NAME_MAX_LENGTH = 255


def _validate_poll_interval(v: int | None) -> int | None:
    if v is None:
        return v
    if v < MIN_POLL_INTERVAL_SECONDS:
        raise ValueError(f"poll_interval_seconds must be >= {MIN_POLL_INTERVAL_SECONDS} or null")
    return v


class DeviceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=DEVICE_NAME_MAX_LENGTH)
    template_id: uuid.UUID
    topology_type: TopologyType
    status: DeviceStatus = DeviceStatus.AVAILABLE
    field_data: dict[str, Any] = {}
    poll_interval_seconds: int | None = None

    @field_validator("poll_interval_seconds")
    @classmethod
    def _check_poll_interval(cls, v: int | None) -> int | None:
        return _validate_poll_interval(v)


class InternalDeviceCreate(BaseModel):
    # Internal dynamic-instance create (X-Internal-Token). name is server-generated
    # when omitted; field_data tolerates unknown keys because the recipe's
    # create_instance result contributes non-template instance attributes.
    # request_id is the caller's idempotency key (issue #275): a redelivered
    # create carrying the same request_id converges on the existing device row
    # instead of materializing a duplicate. Omitting it preserves the prior,
    # always-create behavior.
    template_id: uuid.UUID
    reservation_id: uuid.UUID
    name: str | None = Field(default=None, min_length=1, max_length=DEVICE_NAME_MAX_LENGTH)
    field_data: dict[str, Any] = {}
    request_id: uuid.UUID | None = None


class DeviceUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=DEVICE_NAME_MAX_LENGTH)
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
    # The execution service keys its driver-package cache on driver_sha256, so it
    # must travel on the device payload; without it the cache key is a constant
    # ("unknown") and never invalidates when a driver package is replaced, so an
    # updated driver (e.g. one that newly publishes config_schema()) is never
    # re-extracted. driver_filename lets the execution side extract the package
    # with the right archive handler (.zip vs .tar.gz).
    driver_sha256: str | None = None
    driver_filename: str | None = None
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
