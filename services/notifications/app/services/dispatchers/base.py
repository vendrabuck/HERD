import uuid
from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class DispatchMessage:
    user_id: uuid.UUID
    event_type: str
    title: str
    body: str
    data: dict = field(default_factory=dict)
    # Source NATS stream+sequence for idempotency (e.g. "HERD_RESERVATIONS:42").
    # None when not dispatched from a NATS event; such rows are not deduplicated.
    dedupe_key: str | None = None


class Dispatcher(Protocol):
    channel: str

    async def send(self, session_factory, message: DispatchMessage) -> None: ...
