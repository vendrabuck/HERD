import uuid

from pydantic import BaseModel, field_serializer


class PathfindRequest(BaseModel):
    source_device_id: uuid.UUID
    target_device_id: uuid.UUID


class PathHop(BaseModel):
    device_id: uuid.UUID
    port_in: str | None = None
    port_out: str | None = None

    @field_serializer("device_id")
    def serialize_uuid(self, value: uuid.UUID) -> str:
        return str(value)


class PathfindResponse(BaseModel):
    reachable: bool
    hop_count: int
    paths: list[list[PathHop]]
