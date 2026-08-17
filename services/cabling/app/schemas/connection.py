import uuid
from datetime import datetime

from pydantic import BaseModel, Field, field_serializer


class ConnectionCreate(BaseModel):
    device_a_id: uuid.UUID
    port_a: str = Field(min_length=1, max_length=255)
    device_b_id: uuid.UUID
    port_b: str = Field(min_length=1, max_length=255)
    # connection_type is bounded but not vocabulary-locked here; constraining it
    # to an enum is a separate behavior change (see #130).
    connection_type: str = Field(default="ethernet", max_length=50)
    notes: str | None = Field(default=None, max_length=2000)


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


class ConnectionBulkCreate(BaseModel):
    # The 200 cap mirrors inventory's BulkPortCreate.instances.
    items: list[ConnectionCreate] = Field(min_length=1, max_length=200)


class ConnectionBulkRowResult(BaseModel):
    index: int  # position in the submitted items list
    status: str  # "created" or "rejected"
    connection_id: uuid.UUID | None
    error: str | None  # human-readable reason when rejected

    @field_serializer("connection_id")
    def serialize_connection_id(self, value: uuid.UUID | None) -> str | None:
        return str(value) if value is not None else None


class ConnectionBulkReport(BaseModel):
    created: int
    rejected: int
    rows: list[ConnectionBulkRowResult]
