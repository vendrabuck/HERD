import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class PreferencesResponse(BaseModel):
    user_id: uuid.UUID
    saved_filters: dict
    page_sizes: dict
    extras: dict
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class PreferencesReplaceRequest(BaseModel):
    saved_filters: dict = Field(default_factory=dict)
    page_sizes: dict = Field(default_factory=dict)
    extras: dict = Field(default_factory=dict)


class PreferencesPatchRequest(BaseModel):
    saved_filters: dict | None = None
    page_sizes: dict | None = None
    extras: dict | None = None
