import uuid
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from app.config import settings
from app.models.reservation import ReservationStatus, TopologyType


def _as_utc(dt: datetime) -> datetime:
    """Treat a naive datetime as UTC; leave an aware one as-is.

    SQLite (test backend) drops tzinfo and Postgres preserves it, so request
    datetimes arrive either naive or aware. Normalizing before comparing to
    datetime.now(timezone.utc) keeps the comparison correct on both backends.
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _dedupe_preserve_order(values: list[uuid.UUID]) -> list[uuid.UUID]:
    """Drop duplicate device ids, keeping first-seen order.

    The reservation_devices join table has a unique (reservation_id, device_id)
    constraint, so a request listing the same device twice would otherwise raise
    an integrity error on flush. Order is not part of the API contract.
    """
    seen: set[uuid.UUID] = set()
    result: list[uuid.UUID] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


class ReservationCreate(BaseModel):
    device_ids: list[uuid.UUID] = Field(max_length=200)
    topology_id: uuid.UUID | None = None
    purpose: str | None = Field(default=None, max_length=2000)
    start_time: datetime
    end_time: datetime

    @field_validator("device_ids")
    @classmethod
    def device_ids_not_empty(cls, v: list[uuid.UUID]) -> list[uuid.UUID]:
        if not v:
            raise ValueError("At least one device must be specified")
        return _dedupe_preserve_order(v)

    @field_validator("end_time")
    @classmethod
    def end_after_start(cls, v: datetime, info: Any) -> datetime:
        start = info.data.get("start_time")
        if start and v <= start:
            raise ValueError("end_time must be after start_time")
        return v

    @model_validator(mode="after")
    def validate_window(self) -> "ReservationCreate":
        """Reject a start_time meaningfully in the past and an over-long window.

        The start grace tolerates clock skew and "start now"; only a start
        older than now minus the grace is rejected. The duration cap guards
        against runaway or typo'd windows; 0 disables it.
        """
        now = datetime.now(timezone.utc)
        start = _as_utc(self.start_time)
        end = _as_utc(self.end_time)

        grace = settings.reservation_start_grace_seconds
        if (now - start).total_seconds() > grace:
            raise ValueError(f"start_time is too far in the past (more than {grace}s before now)")

        max_duration = settings.reservation_max_duration_seconds
        if max_duration and (end - start).total_seconds() > max_duration:
            raise ValueError(f"reservation duration exceeds the maximum of {max_duration}s")
        return self


class ReservationUpdate(BaseModel):
    end_time: datetime | None = None
    purpose: str | None = Field(default=None, max_length=2000)
    device_ids: list[uuid.UUID] | None = Field(default=None, max_length=200)

    @field_validator("device_ids")
    @classmethod
    def device_ids_not_empty(cls, v: list[uuid.UUID] | None) -> list[uuid.UUID] | None:
        if v is None:
            return None
        if len(v) == 0:
            raise ValueError("device_ids cannot be empty")
        return _dedupe_preserve_order(v)


class ReservationResponse(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    owner_name: str = ""
    # Reconstructed from the reservation_devices join table via an association
    # proxy; the validator coerces defensively in case strings reach it.
    device_ids: list[uuid.UUID]
    topology_id: uuid.UUID | None
    topology_type: TopologyType
    purpose: str | None
    start_time: datetime
    end_time: datetime
    status: ReservationStatus
    created_at: datetime
    updated_at: datetime
    modified_by: uuid.UUID | None = None

    model_config = {"from_attributes": True}

    @field_validator("device_ids", mode="before")
    @classmethod
    def coerce_device_ids(cls, v: Any) -> list[uuid.UUID]:
        if isinstance(v, list):
            return [uuid.UUID(str(item)) for item in v]
        return v


class OwnsActiveResponse(BaseModel):
    """Yes/no whether a user owns a currently-active reservation containing a device.

    Used by inventory's widened ACL helper so a reservation owner with no
    explicit `manage` grant on a device can still author and schedule
    configs against devices in their own active reservation (the iter-2
    precedent extended to write paths).
    """

    owns_active: bool


class ReservationInternalStatus(BaseModel):
    """Minimal status payload for service-to-service callers (apply scheduler, etc.).

    Returns only what a background gate needs: status enum value, an `is_active`
    boolean that combines status with the time window, and the window itself.
    No PII; no device list.
    """

    id: uuid.UUID
    status: ReservationStatus
    is_active: bool
    start_time: datetime
    end_time: datetime

    model_config = {"from_attributes": True}


class PaginatedReservationResponse(BaseModel):
    items: list[ReservationResponse]
    total: int
    skip: int
    limit: int


class UserBucket(BaseModel):
    user_id: uuid.UUID
    owner_name: str
    reservation_count: int
    hours: float


class DeviceBucket(BaseModel):
    device_id: uuid.UUID
    reservation_count: int
    hours: float


class TopologyTypeBucket(BaseModel):
    topology_type: TopologyType
    reservation_count: int
    hours: float


class DayBucket(BaseModel):
    day: str  # ISO date, e.g. "2026-04-15"
    reservation_count: int
    hours: float


class GroupBucket(BaseModel):
    group_id: uuid.UUID | None  # None for users not in any group
    group_name: str
    reservation_count: int
    hours: float


class UtilizationReport(BaseModel):
    window_start: datetime
    window_end: datetime
    total_hours: float
    total_reservations: int
    by_user: list[UserBucket]
    by_device: list[DeviceBucket]
    by_topology_type: list[TopologyTypeBucket] = []
    by_day: list[DayBucket] = []
    by_group: list[GroupBucket] = []
    execution_run_count: int | None = None
