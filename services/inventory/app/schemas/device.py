import uuid
from datetime import datetime
from typing import Annotated, Any

from pydantic import BaseModel, Field, ValidationInfo, field_validator

from app.models.device import DeviceStatus, TopologyType
from app.schemas.column_bounds import INT32_MAX, JsonObject, column_string

# Minimum opt-in poll cadence. The execution scheduler only claims due rows
# once per tick (HEALTH_POLL_SCHEDULER_TICK_SECONDS, default 30 seconds), so
# a device's effective poll period is always at least one tick regardless of
# a lower configured interval. This constant must equal execution's default
# tick length so the API refuses cadences the scheduler cannot honor at the
# default; tests/unit/test_poll_floor_parity.py enforces that equality.
MIN_POLL_INTERVAL_SECONDS = 30

# Matches the devices.name column width (String(255), app/models/device.py).
DEVICE_NAME_MAX_LENGTH = 255
DeviceName = column_string(DEVICE_NAME_MAX_LENGTH, min_length=1)

# A poll interval is written to an `Integer` column (devices and device_templates),
# so it is at most the column's largest value (INV-POLL-5).
PollInterval = Annotated[int, Field(le=INT32_MAX)]


def _validate_poll_interval(v: int | None) -> int | None:
    if v is None:
        return v
    if v < MIN_POLL_INTERVAL_SECONDS:
        raise ValueError(f"poll_interval_seconds must be >= {MIN_POLL_INTERVAL_SECONDS} or null")
    return v


class DeviceCreate(BaseModel):
    name: DeviceName
    template_id: uuid.UUID
    topology_type: TopologyType
    status: DeviceStatus = DeviceStatus.AVAILABLE
    field_data: JsonObject = {}
    poll_interval_seconds: PollInterval | None = None

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
    name: DeviceName | None = None
    field_data: JsonObject = {}
    request_id: uuid.UUID | None = None


class DeviceUpdate(BaseModel):
    name: DeviceName | None = None
    topology_type: TopologyType | None = None
    status: DeviceStatus | None = None
    field_data: JsonObject | None = None
    poll_interval_seconds: PollInterval | None = None

    @field_validator("name", "topology_type", "status", "field_data", mode="before")
    @classmethod
    def _refuse_explicit_null(cls, v: Any, info: ValidationInfo) -> Any:
        # These four map to NOT NULL columns. An omitted field is left alone
        # (update_device applies exclude_unset); an explicit null used to reach
        # the commit and fail as a NOT NULL violation that was reported as a
        # name clash (issue #1016). Refuse it here, by field name, as a 422.
        if v is None:
            raise ValueError(
                f"{info.field_name} cannot be null; omit the field to leave it unchanged"
            )
        return v

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
