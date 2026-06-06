import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class NotificationResponse(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    event_type: str
    title: str
    body: str
    data: dict
    read_at: datetime | None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class NotificationList(BaseModel):
    items: list[NotificationResponse]
    total: int
    unread: int


class UnreadCount(BaseModel):
    count: int


class MarkAllReadResponse(BaseModel):
    updated: int
