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


class DynamicRequestSpec(BaseModel):
    """One requested dynamic instance (ADR 0004, issue #32).

    Listing the same template_id N times requests N instances of it; there is
    deliberately no dedupe.
    """

    template_id: uuid.UUID


class ReservationCreate(BaseModel):
    device_ids: list[uuid.UUID] = Field(max_length=200)
    topology_id: uuid.UUID | None = None
    purpose: str | None = Field(default=None, max_length=2000)
    start_time: datetime
    end_time: datetime
    # Dynamic instance requests (ADR 0004). Each entry materializes one
    # hypervisor-backed instance; a reservation carrying any of these books
    # through PENDING_PROVISION and activates only on the provision-result
    # callback. Instances are hypervisor-bound, so the cap is deliberately
    # tighter than the 200-device cap.
    dynamic_requests: list[DynamicRequestSpec] = Field(default_factory=list, max_length=50)
    # Lab purpose classification (issue #646 phase 1). Validated against the
    # configured taxonomy in the service layer (app.services.purpose_service),
    # not here: a schema-level field_validator raising ValueError would wrap
    # the pinned "Unknown purpose_category '<value>'; allowed: ..." message in
    # pydantic's request-validation envelope instead of returning it verbatim.
    purpose_category: str | None = None

    @field_validator("device_ids")
    @classmethod
    def dedupe_device_ids(cls, v: list[uuid.UUID]) -> list[uuid.UUID]:
        # device_ids may be empty for a dynamic-only booking (ADR 0004, issue
        # #274); the device-or-dynamic requirement is enforced cross-field in
        # require_device_or_dynamic below. This validator only dedupes.
        return _dedupe_preserve_order(v)

    @field_validator("end_time")
    @classmethod
    def end_after_start(cls, v: datetime, info: Any) -> datetime:
        start = info.data.get("start_time")
        if start and v <= start:
            raise ValueError("end_time must be after start_time")
        return v

    @model_validator(mode="after")
    def require_device_or_dynamic(self) -> "ReservationCreate":
        """Reject a booking that reserves nothing at all.

        device_ids may be empty only when the booking carries at least one
        dynamic request (ADR 0004, issue #274): a dynamic-only lab needs no
        pre-existing physical device. A request with neither is meaningless and
        is rejected here rather than deeper in the service, where an empty
        device fetch would otherwise derive no topology_type.
        """
        if not self.device_ids and not self.dynamic_requests:
            raise ValueError("A reservation must include at least one device or dynamic request")
        return self

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


class PurposeCategoryUpdate(BaseModel):
    """Body of PATCH /{reservation_id}/purpose-category (issue #646 phase 1).

    A null purpose_category clears the classification (and its set_by/set_at)
    rather than being rejected; taxonomy membership is validated in the
    service layer, not here, for the same pinned-detail-wording reason
    ReservationCreate.purpose_category is unvalidated at the schema level.
    """

    purpose_category: str | None = None


class PurposeCategoriesResponse(BaseModel):
    """Body of GET /purpose-categories: the configured taxonomy, in order."""

    categories: list[str]


class DynamicRequestResponse(BaseModel):
    """A booked dynamic instance request; `id` is the execution-side request_id."""

    id: uuid.UUID
    template_id: uuid.UUID

    model_config = {"from_attributes": True}


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
    # Non-null only when an admin cancelled a reservation they do not own (#340).
    cancelled_by: uuid.UUID | None = None
    dynamic_requests: list[DynamicRequestResponse] = []
    # Lab purpose classification (issue #646 phase 1). purpose_category_set_by
    # is deliberately not exposed here; the response carries only the value and
    # when it was set.
    purpose_category: str | None = None
    purpose_category_set_at: datetime | None = None

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


class ProvisionResultRequest(BaseModel):
    """Body of the execution service's provision-result callback (ADR 0004).

    device_ids are the inventory devices the dynamic instances materialized as;
    on success they are attached to the reservation before it activates.
    """

    succeeded: bool
    device_ids: list[str] = Field(default_factory=list)
    error: str | None = None


class ProvisionResultResponse(BaseModel):
    """Outcome of a provision-result post.

    applied=False means the callback was a no-op: the reservation had already
    left PENDING_PROVISION (duplicate callback, timeout backstop, or a user
    cancel won the race) and was not re-transitioned.
    """

    reservation_id: uuid.UUID
    status: ReservationStatus
    applied: bool


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


class PurposeBucket(BaseModel):
    """One row of the report's by_purpose breakdown (issue #646 phase 1).

    purpose_category is the literal string "unclassified" for reservations
    with a null purpose_category, never a null value, so a client can group
    on the field without a null-handling special case.
    """

    purpose_category: str
    reservations: int
    device_hours: float


class UserPurposeBucket(BaseModel):
    """One (user, purpose_category) row of the report's by_user_purpose breakdown."""

    user_id: uuid.UUID
    purpose_category: str
    reservations: int
    device_hours: float


class DevicePurposeBucket(BaseModel):
    """One (device, purpose_category) row of the report's by_device_purpose breakdown.

    Reserved devices only (issue #646 phase 1 scope); transit-gear inheritance
    is deferred to phase 3 alongside the device rollups.
    """

    device_id: uuid.UUID
    purpose_category: str
    reservations: int
    device_hours: float


class FleetDeviceBucket(BaseModel):
    device_id: uuid.UUID
    name: str
    # Inventory's current DeviceStatus (AVAILABLE/RESERVED/OFFLINE/MAINTENANCE),
    # carried as a plain string: the enum is owned by the inventory service and
    # reservations must not import another service's model.
    status: str
    reservation_count: int
    hours: float
    utilization_pct: float


class FleetSection(BaseModel):
    device_count: int
    idle_device_count: int
    window_hours: float
    total_reserved_hours: float
    utilization_pct: float
    devices: list[FleetDeviceBucket]


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
    # None when the inventory service could not be reached; the rest of the
    # report is still served (same degrade contract as execution_run_count).
    fleet: FleetSection | None = None
    # Lab purpose classification breakdowns (issue #646 phase 1). Honor the
    # same status_filter as by_user/by_device/by_topology_type/by_day; count
    # reserved devices only (transit gear is phase 3).
    by_purpose: list[PurposeBucket] = []
    by_user_purpose: list[UserPurposeBucket] = []
    by_device_purpose: list[DevicePurposeBucket] = []
