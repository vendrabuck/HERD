import uuid
from datetime import datetime

from pydantic import BaseModel, field_serializer


class CommandLogEntry(BaseModel):
    id: uuid.UUID
    run_id: uuid.UUID
    seq: int
    command: str
    response: str | None = None
    duration_ms: int | None = None
    exit_status: str
    created_at: datetime

    model_config = {"from_attributes": True}

    @field_serializer("id", "run_id")
    def serialize_uuid(self, value: uuid.UUID) -> str:
        return str(value)
