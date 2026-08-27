import uuid

from app.schemas._types import UUIDStr
from pydantic import BaseModel, Field

# Upper bound on pairs accepted by a single batch pathfind request. A full
# mesh over 63 devices is 1953 pairs, so 2000 covers the Routes tab's
# n(n-1)/2 fan-in for realistic reservations while bounding per-request work.
MAX_BATCH_PAIRS = 2000


class PathfindRequest(BaseModel):
    source_device_id: uuid.UUID
    target_device_id: uuid.UUID


class PathHop(BaseModel):
    device_id: UUIDStr
    port_in: str | None = None
    port_out: str | None = None


class PathfindResponse(BaseModel):
    reachable: bool
    hop_count: int
    paths: list[list[PathHop]]


class PathfindBatchRequest(BaseModel):
    pairs: list[PathfindRequest] = Field(max_length=MAX_BATCH_PAIRS)


class PathfindBatchResult(PathfindResponse):
    """One per-pair result; identical shape to the single endpoint's response
    plus an echo of the requested pair so clients can correlate without
    relying on order alone."""

    source_device_id: UUIDStr
    target_device_id: UUIDStr


class PathfindBatchResponse(BaseModel):
    results: list[PathfindBatchResult]
