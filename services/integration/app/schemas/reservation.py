"""Frozen v1 request/response contract for the reservation facade.

These models are deliberately decoupled from the reservations service's internal
schemas. The point of the facade is a stable external contract: internal refactors
to reservations' own request/response shapes must not break v1 clients, so v1 fields
are declared here explicitly and the router translates to and from the internal
payloads. Do NOT import reservations' internal schemas here.
"""

import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class V1ReservationRequest(BaseModel):
    """v1 reserve request. Translated to the reservations service create payload."""

    device_ids: list[uuid.UUID] = Field(min_length=1, max_length=200)
    start_time: datetime
    end_time: datetime
    purpose: str | None = Field(default=None, max_length=2000)
    topology_id: uuid.UUID | None = None
    # Lab purpose classification (issue #646 phase 1), additive. Validated
    # downstream by the reservations service against its configured taxonomy;
    # an unknown value is relayed as the same 422 an interactive create gets.
    purpose_category: str | None = None


class V1ReservationResponse(BaseModel):
    """v1 reservation view. A stable subset of the reservations service response.

    `status` is a plain string on purpose so the v1 contract does not couple to the
    internal ReservationStatus enum.
    """

    id: uuid.UUID
    status: str
    device_ids: list[uuid.UUID]
    topology_id: uuid.UUID | None = None
    start_time: datetime
    end_time: datetime
    created_at: datetime
    # Additive (issue #646 phase 1); None when unclassified.
    purpose_category: str | None = None


class V1ReservationList(BaseModel):
    """v1 paginated list of reservations."""

    items: list[V1ReservationResponse]
    total: int
    skip: int
    limit: int
