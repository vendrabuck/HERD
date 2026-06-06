import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, field_serializer


class DeviceConfigVersionCreate(BaseModel):
    config: dict[str, Any]
    description: str | None = None


class DeviceConfigVersionResponse(BaseModel):
    id: uuid.UUID
    device_id: uuid.UUID
    version_number: int
    connection_type: str
    description: str | None = None
    created_by: uuid.UUID
    author_name: str
    created_at: datetime
    restored_from_id: uuid.UUID | None = None
    last_apply_run_id: uuid.UUID | None = None

    model_config = {"from_attributes": True}

    @field_serializer("id", "device_id", "created_by")
    def _ser_uuid(self, value: uuid.UUID) -> str:
        return str(value)

    @field_serializer("restored_from_id", "last_apply_run_id")
    def _ser_optional_uuid(self, value: uuid.UUID | None) -> str | None:
        return str(value) if value else None


class DeviceConfigVersionDetail(DeviceConfigVersionResponse):
    config: dict[str, Any]


class PaginatedDeviceConfigVersions(BaseModel):
    items: list[DeviceConfigVersionResponse]
    total: int
    skip: int
    limit: int


class DeviceConfigDiff(BaseModel):
    version_a: uuid.UUID
    version_b: uuid.UUID
    diff: str

    @field_serializer("version_a", "version_b")
    def _ser_uuid(self, value: uuid.UUID) -> str:
        return str(value)


class DeviceConfigRestoreRequest(BaseModel):
    description: str | None = None


class DeviceConfigApplyResponse(BaseModel):
    version_id: uuid.UUID
    run_id: uuid.UUID | None = None
    status: str
    error: str | None = None

    @field_serializer("version_id")
    def _ser_uuid(self, value: uuid.UUID) -> str:
        return str(value)

    @field_serializer("run_id")
    def _ser_run_id(self, value: uuid.UUID | None) -> str | None:
        return str(value) if value else None


class ApplyJobScheduleRequest(BaseModel):
    scheduled_for: datetime
    reservation_id: uuid.UUID | None = None
    dry_run: bool = False


class ApplyJobResponse(BaseModel):
    id: uuid.UUID
    device_id: uuid.UUID
    version_id: uuid.UUID
    scheduled_for: datetime
    reservation_id: uuid.UUID | None = None
    dry_run: bool = False
    status: str
    run_id: uuid.UUID | None = None
    error: str | None = None
    created_by: uuid.UUID
    author_name: str
    created_at: datetime
    fired_at: datetime | None = None

    model_config = {"from_attributes": True}

    @field_serializer("id", "device_id", "version_id", "created_by")
    def _ser_uuid(self, value: uuid.UUID) -> str:
        return str(value)

    @field_serializer("reservation_id", "run_id")
    def _ser_optional(self, value: uuid.UUID | None) -> str | None:
        return str(value) if value else None


class PaginatedApplyJobs(BaseModel):
    items: list[ApplyJobResponse]
    total: int
    skip: int
    limit: int
