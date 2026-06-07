import json
import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Bounds for preference payloads. These are self-scoped (a user can only set
# their own row), so the caps exist to keep a single row from holding an
# unbounded JSONB blob and to enforce that page_sizes really are positive page
# sizes, which downstream code (the frontend getPageSize) assumes.
_MAX_KEYS = 200
_MAX_PAGE_SIZE = 500
_MAX_BLOB_BYTES = 64 * 1024  # 64 KB serialized cap per dict field


def _validate_page_sizes(value: dict | None) -> dict | None:
    if value is None:
        return value
    if len(value) > _MAX_KEYS:
        raise ValueError(f"page_sizes has too many keys (max {_MAX_KEYS})")
    for key, size in value.items():
        # bool is a subclass of int; reject it so True/False is not a page size.
        if not isinstance(size, int) or isinstance(size, bool):
            raise ValueError(f"page_sizes[{key!r}] must be an integer")
        if not 1 <= size <= _MAX_PAGE_SIZE:
            raise ValueError(f"page_sizes[{key!r}] must be between 1 and {_MAX_PAGE_SIZE}")
    return value


def _validate_blob(value: dict | None, field_name: str) -> dict | None:
    if value is None:
        return value
    if len(value) > _MAX_KEYS:
        raise ValueError(f"{field_name} has too many keys (max {_MAX_KEYS})")
    try:
        size = len(json.dumps(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be JSON-serializable") from exc
    if size > _MAX_BLOB_BYTES:
        raise ValueError(f"{field_name} is too large (max {_MAX_BLOB_BYTES} bytes serialized)")
    return value


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

    @field_validator("page_sizes")
    @classmethod
    def _check_page_sizes(cls, value: dict) -> dict:
        return _validate_page_sizes(value)

    @field_validator("saved_filters", "extras")
    @classmethod
    def _check_blob(cls, value: dict, info: Any) -> dict:
        return _validate_blob(value, info.field_name)


class PreferencesPatchRequest(BaseModel):
    saved_filters: dict | None = None
    page_sizes: dict | None = None
    extras: dict | None = None

    @field_validator("page_sizes")
    @classmethod
    def _check_page_sizes(cls, value: dict | None) -> dict | None:
        return _validate_page_sizes(value)

    @field_validator("saved_filters", "extras")
    @classmethod
    def _check_blob(cls, value: dict | None, info: Any) -> dict | None:
        return _validate_blob(value, info.field_name)
