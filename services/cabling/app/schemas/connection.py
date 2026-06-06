import uuid
from datetime import datetime

from pydantic import BaseModel, field_serializer


class ConnectionCreate(BaseModel):
    device_a_id: uuid.UUID
    port_a: str
    device_b_id: uuid.UUID
    port_b: str
    connection_type: str = "ethernet"
    notes: str | None = None


class ConnectionResponse(BaseModel):
    id: uuid.UUID
    device_a_id: uuid.UUID
    port_a: str
    device_b_id: uuid.UUID
    port_b: str
    connection_type: str
    notes: str | None
    created_by: str
    created_at: datetime
    modified_by: str | None = None
    updated_at: datetime | None = None

    model_config = {"from_attributes": True}

    @field_serializer("id", "device_a_id", "device_b_id")
    def serialize_uuid(self, value: uuid.UUID) -> str:
        return str(value)


class PaginatedConnectionResponse(BaseModel):
    items: list[ConnectionResponse]
    total: int
    skip: int
    limit: int
