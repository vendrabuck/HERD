import uuid

from pydantic import BaseModel, field_serializer


class ForkCreate(BaseModel):
    """Body for POST /internal/forks (fork-on-activation)."""

    reservation_id: uuid.UUID
    parent_topology_id: uuid.UUID | None = None
    parent_version_id: uuid.UUID | None = None
    created_by: str | None = None


class ForkCreateResponse(BaseModel):
    fork_id: uuid.UUID
    version_number: int

    @field_serializer("fork_id")
    def serialize_fork_id(self, value: uuid.UUID) -> str:
        return str(value)
