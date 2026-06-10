import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class PortCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    template_id: uuid.UUID
    field_data: dict[str, Any] = {}


class BulkPortCreate(BaseModel):
    # The final port name is name_prefix + index, so cap the prefix below the
    # 255 column width to leave room for the suffix.
    name_prefix: str = Field(..., min_length=1, max_length=200)
    starting_index: int = Field(..., ge=0)
    instances: int = Field(..., ge=1, le=200)
    template_id: uuid.UUID
    field_data: dict[str, Any] = {}


class PortUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    field_data: dict[str, Any] | None = None


class PortResponse(BaseModel):
    id: uuid.UUID
    name: str
    device_id: uuid.UUID
    template_id: uuid.UUID
    template_name: str | None = None
    template_icon: str | None = None
    exclusive: bool = True
    field_data: dict[str, Any]
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
