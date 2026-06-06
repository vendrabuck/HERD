import uuid
from datetime import datetime

from pydantic import BaseModel, field_serializer


class ExecutionRunResponse(BaseModel):
    id: uuid.UUID
    device_id: uuid.UUID
    driver_id: uuid.UUID
    driver_sha256: str
    action: str
    status: str
    reservation_id: uuid.UUID | None = None
    user_id: uuid.UUID
    input_params: dict
    output: str | None = None
    error: str | None = None
    port_a: str | None = None
    port_b: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    duration_ms: int | None = None
    created_at: datetime

    model_config = {"from_attributes": True}

    @field_serializer("id", "device_id", "driver_id", "user_id")
    def serialize_uuid(self, value: uuid.UUID) -> str:
        return str(value)

    @field_serializer("reservation_id")
    def serialize_optional_uuid(self, value: uuid.UUID | None) -> str | None:
        return str(value) if value else None


class PaginatedExecutionRunResponse(BaseModel):
    items: list[ExecutionRunResponse]
    total: int
    skip: int
    limit: int


class DeviceCheckRequest(BaseModel):
    device_id: uuid.UUID
    user_id: uuid.UUID


class DeviceCheckResponse(BaseModel):
    run_id: uuid.UUID
    device_id: uuid.UUID
    status: str
    output: str | None = None
    error: str | None = None

    @field_serializer("run_id", "device_id")
    def serialize_uuid(self, value: uuid.UUID) -> str:
        return str(value)


class ManualExecuteRequest(BaseModel):
    device_id: uuid.UUID
    action: str
    user_id: uuid.UUID
    reservation_id: uuid.UUID | None = None
    port_a: str | None = None
    port_b: str | None = None
    method_kwargs: dict | None = None
    dry_run: bool = False
