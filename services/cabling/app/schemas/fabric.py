import uuid

from pydantic import BaseModel, field_serializer


class FabricResponse(BaseModel):
    device_id: uuid.UUID
    fabric_id: uuid.UUID
    component_size: int

    @field_serializer("device_id", "fabric_id")
    def serialize_uuid(self, value: uuid.UUID) -> str:
        return str(value)
