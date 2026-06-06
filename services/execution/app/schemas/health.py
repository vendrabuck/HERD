import uuid
from datetime import datetime

from pydantic import BaseModel, field_serializer


class DeviceHealthStatusResponse(BaseModel):
    device_id: uuid.UUID
    last_polled_at: datetime | None = None
    last_status: str
    last_run_id: uuid.UUID | None = None
    consecutive_failures: int
    next_poll_at: datetime | None = None

    model_config = {"from_attributes": True}

    @field_serializer("device_id")
    def _ser_device_id(self, value: uuid.UUID) -> str:
        return str(value)

    @field_serializer("last_run_id")
    def _ser_last_run(self, value: uuid.UUID | None) -> str | None:
        return str(value) if value else None


class PaginatedDeviceHealthResponse(BaseModel):
    items: list[DeviceHealthStatusResponse]
    total: int
    skip: int
    limit: int
