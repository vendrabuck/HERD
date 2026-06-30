"""Pydantic contracts for webhook subscription CRUD and the delivery ledger."""

import uuid
from datetime import datetime

from pydantic import BaseModel, Field, field_validator

# The reservation lifecycle event names a subscription may subscribe to. These
# mirror the `event` field on HERD_RESERVATIONS payloads (subjects
# herd.reservations.{created,updated,cancelled,completed,failed,expiring_soon}).
KNOWN_EVENT_TYPES = {
    "reservation.created",
    "reservation.updated",
    "reservation.cancelled",
    "reservation.completed",
    "reservation.failed",
    "reservation.expiring_soon",
}


class WebhookCreate(BaseModel):
    target_url: str = Field(min_length=1, max_length=2048)
    event_types: list[str] = Field(min_length=1)
    secret: str | None = Field(default=None, max_length=512)
    description: str | None = Field(default=None, max_length=1024)

    @field_validator("target_url")
    @classmethod
    def _validate_url(cls, v: str) -> str:
        if not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError("target_url must be an http or https URL")
        return v

    @field_validator("event_types")
    @classmethod
    def _validate_events(cls, v: list[str]) -> list[str]:
        unknown = [e for e in v if e not in KNOWN_EVENT_TYPES]
        if unknown:
            raise ValueError(f"unknown event_types {unknown}; allowed: {sorted(KNOWN_EVENT_TYPES)}")
        # De-duplicate while preserving order.
        return list(dict.fromkeys(v))


class WebhookResponse(BaseModel):
    """Subscription metadata. Secret omitted for hygiene (see WebhookCreated)."""

    id: uuid.UUID
    target_url: str
    event_types: list[str]
    is_active: bool
    description: str | None = None
    created_by: uuid.UUID | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class WebhookCreated(WebhookResponse):
    """Creation response: includes the secret once so the registrant can verify
    signatures. The secret is never returned again on list or get."""

    secret: str


class WebhookDeliveryResponse(BaseModel):
    id: uuid.UUID
    subscription_id: uuid.UUID
    event_id: str
    event_type: str
    status: str
    attempts: int
    response_status: int | None = None
    last_error: str | None = None
    created_at: datetime
    delivered_at: datetime | None = None

    model_config = {"from_attributes": True}
