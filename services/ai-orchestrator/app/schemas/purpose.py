"""Request/response schemas for lab purpose classification (issue #646
phase 2, ADR 0013 points 8-11).

The taxonomy itself is owned by the reservations service (ADR 0013 point
1); this service is deliberately taxonomy-agnostic and takes the category
list from the caller on every request (issue #646 refinement 4), so
`categories` here is always a plain caller-supplied list, never a fixed
enum.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class DynamicRequestItem(BaseModel):
    template_id: uuid.UUID
    count: int = Field(ge=1)


class PreviewClassifyRequest(BaseModel):
    """Body for POST /classify-purpose/preview (creation pass, user JWT)."""

    categories: list[str] = Field(min_length=1)
    purpose: str | None = None
    topology_id: uuid.UUID | None = None
    device_ids: list[uuid.UUID] | None = None
    dynamic_requests: list[DynamicRequestItem] | None = None


class InternalClassifyRequest(BaseModel):
    """Body for POST /internal/classify-purpose (end-of-reservation pass,
    X-Internal-Token). Carries everything reservations owns; this service
    enriches it with its own transcripts plus inventory/cabling signals."""

    reservation_id: uuid.UUID
    categories: list[str] = Field(min_length=1)
    purpose: str | None = None
    user_id: uuid.UUID
    device_ids: list[uuid.UUID]
    topology_id: uuid.UUID | None = None
    dynamic_requests: list[DynamicRequestItem] | None = None
    start_time: datetime
    end_time: datetime
    status: str


class PurposeCategoryProbability(BaseModel):
    category: str
    probability: float


class PurposeClassification(BaseModel):
    """Shared response shape for both classify endpoints.

    `pass_` is exposed on the wire as `pass` (a Python keyword, hence the
    alias); `populate_by_name=True` lets server code construct it as
    `pass_=...` while FastAPI's response serialization (by_alias=True by
    default) still emits `pass`. `signals_used` lists which signals were
    actually present in the prompt (issue #646 refinement 4): a
    signal-fetch failure never fails the request, it is simply absent here.
    """

    model_config = ConfigDict(populate_by_name=True)

    distribution: list[PurposeCategoryProbability]
    top_category: str
    pass_: Literal["creation", "end"] = Field(alias="pass")
    model: str
    rationale: str
    generated_at: datetime
    signals_used: list[str] = Field(default_factory=list)
