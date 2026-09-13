import uuid

from app.schemas._types import OptionalUUIDStr, UUIDStr
from pydantic import BaseModel, Field

# Upper bound on pairs accepted by a single batch pathfind request. A full
# mesh over 63 devices is 1953 pairs, so 2000 covers the Routes tab's
# n(n-1)/2 fan-in for realistic reservations while bounding per-request work.
MAX_BATCH_PAIRS = 2000


class PathfindRequest(BaseModel):
    source_device_id: uuid.UUID
    target_device_id: uuid.UUID


class PathHop(BaseModel):
    """One device on a resolved physical path.

    ``device_id`` is nullable and ``hidden`` exists because of issue #763: for
    a non-admin caller, a transit hop through a device outside that caller's
    device-group visibility is redacted by the pathfind routes
    (``device_id`` null, ``hidden`` true, both port names dropped) instead of
    being removed from the path. Keeping the hop keeps ``hop_count`` and
    reachability exactly what they were, which is what the topology editor and
    the reservation Routes tab actually consume, while the identity and cabling
    of the hidden device stay unsaid. ``hidden`` defaults false and the
    pathfinder itself never sets it: redaction happens only at the route
    boundary, so every in-process caller (fork save, topology validation) still
    sees whole hops.
    """

    device_id: OptionalUUIDStr
    port_in: str | None = None
    port_out: str | None = None
    hidden: bool = False


class PathfindResponse(BaseModel):
    reachable: bool
    hop_count: int
    paths: list[list[PathHop]]


class PathfindBatchRequest(BaseModel):
    pairs: list[PathfindRequest] = Field(max_length=MAX_BATCH_PAIRS)


class PathfindBatchResult(PathfindResponse):
    """One per-pair result; identical shape to the single endpoint's response
    plus an echo of the requested pair so clients can correlate without
    relying on order alone.

    ``error`` (issue #763) is the per-pair analogue of the single endpoint's
    404: a pair naming a device outside a non-admin caller's visibility is
    reported as unreachable with ``error`` set to the same wording the single
    route uses, rather than failing the whole batch. It is null for every
    normal result, so a genuinely unreachable pair (reachable false, error
    null) stays distinguishable from a refused one.
    """

    source_device_id: UUIDStr
    target_device_id: UUIDStr
    error: str | None = None


class PathfindBatchResponse(BaseModel):
    results: list[PathfindBatchResult]
