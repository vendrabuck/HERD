import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, field_validator

from app.models.reservation import ReservationStatus, TopologyType


class ReservationCreate(BaseModel):
    device_ids: list[uuid.UUID]
    purpose: str | None = None
    start_time: datetime
    end_time: datetime

    @field_validator("device_ids")
    @classmethod
    def device_ids_not_empty(cls, v: list[uuid.UUID]) -> list[uuid.UUID]:
        if not v:
            raise ValueError("At least one device must be specified")
        return v

    @field_validator("end_time")
    @classmethod
    def end_after_start(cls, v: datetime, info: Any) -> datetime:
        start = info.data.get("start_time")
        if start and v <= start:
            raise ValueError("end_time must be after start_time")
        return v


class ReservationResponse(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    # device_ids stored as JSON strings, coerced to UUID on read
    device_ids: list[uuid.UUID]
    topology_type: TopologyType
    purpose: str | None
    start_time: datetime
    end_time: datetime
    status: ReservationStatus
    created_at: datetime

    model_config = {"from_attributes": True}

    @field_validator("device_ids", mode="before")
    @classmethod
    def coerce_device_ids(cls, v: Any) -> list[uuid.UUID]:
        if isinstance(v, list):
            return [uuid.UUID(str(item)) for item in v]
        return v
