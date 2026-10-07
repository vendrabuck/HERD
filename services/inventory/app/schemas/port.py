import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, model_validator

PORT_NAME_MAX_LENGTH = 255


class PortCreate(BaseModel):
    name: str = Field(min_length=1, max_length=PORT_NAME_MAX_LENGTH)
    template_id: uuid.UUID
    field_data: dict[str, Any] = {}


class BulkPortCreate(BaseModel):
    # The final port name is name_prefix + index, so cap the prefix below the
    # 255 column width to leave room for the suffix.
    name_prefix: str = Field(..., min_length=1, max_length=200)
    starting_index: int = Field(..., ge=0)
    instances: int = Field(..., ge=1, le=200)
    template_id: uuid.UUID
    field_data: dict[str, Any] = {}

    @model_validator(mode="after")
    def _final_names_fit_column(self) -> "BulkPortCreate":
        # The prefix cap alone does not bound the suffix: starting_index has no
        # upper bound, so the LAST generated name (the longest) is checked
        # against the column width here, making an oversize request a 422
        # instead of a database error (issue #1022).
        last_name = f"{self.name_prefix}{self.starting_index + self.instances - 1}"
        if len(last_name) > PORT_NAME_MAX_LENGTH:
            raise ValueError(
                f"generated port names would exceed {PORT_NAME_MAX_LENGTH} characters; "
                "use a shorter name_prefix or a smaller starting_index"
            )
        return self


class PortUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=PORT_NAME_MAX_LENGTH)
    field_data: dict[str, Any] | None = None


class PortResponse(BaseModel):
    id: uuid.UUID
    name: str
    device_id: uuid.UUID
    template_id: uuid.UUID
    template_name: str | None = None
    template_icon: str | None = None
    exclusive: bool = True
    field_data: dict[str, Any]
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
