import re
import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, field_validator, model_validator

from app.schemas.device import _validate_poll_interval


class FieldDefinition(BaseModel):
    key: str
    label: str
    type: Literal["string", "number", "boolean", "dropdown", "password"]
    required: bool = False
    default: str | float | bool | None = None
    options: list[str] | None = None

    @field_validator("key")
    @classmethod
    def validate_key_format(cls, v: str) -> str:
        if not v:
            raise ValueError("Field key must not be empty")
        if not re.match(r"^[a-zA-Z0-9_]+$", v):
            raise ValueError("Field key must contain only alphanumeric characters and underscores")
        return v

    @model_validator(mode="after")
    def validate_options_and_default(self):
        if self.type == "dropdown":
            if not self.options or len(self.options) == 0:
                raise ValueError("Dropdown fields must have an options list")
            for opt in self.options:
                if not opt or not opt.strip():
                    raise ValueError("Dropdown options must not contain empty strings")
        else:
            if self.options is not None:
                raise ValueError("Only dropdown fields may have options")

        if self.default is not None:
            if self.type in ("string", "password") and not isinstance(self.default, str):
                raise ValueError(f"Default for {self.type} field must be a string")
            if self.type == "number" and not isinstance(self.default, (int, float)):
                raise ValueError("Default for number field must be a number")
            if self.type == "boolean" and not isinstance(self.default, bool):
                raise ValueError("Default for boolean field must be a boolean")
            if self.type == "dropdown":
                if not isinstance(self.default, str):
                    raise ValueError("Default for dropdown field must be a string")
                if self.default not in self.options:
                    raise ValueError(
                        f"Default '{self.default}' is not in options: {', '.join(self.options)}"
                    )
        return self


class SectionDefinition(BaseModel):
    name: str
    fields: list[FieldDefinition]

    @field_validator("name")
    @classmethod
    def validate_name_not_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("Section name must not be empty")
        return v

    @model_validator(mode="after")
    def validate_unique_field_keys(self):
        keys = [f.key for f in self.fields]
        if len(keys) != len(set(keys)):
            seen = set()
            dupes = [k for k in keys if k in seen or seen.add(k)]
            raise ValueError(f"Duplicate field keys in section: {', '.join(dupes)}")
        return self


class TemplateCreate(BaseModel):
    name: str
    template_type: Literal["device", "port", "dynamic"] = "device"
    driver_id: uuid.UUID | None = None
    hypervisor_id: uuid.UUID | None = None
    exclusive: bool = True
    icon: str | None = None
    description: str | None = None
    vendor: str | None = None
    model: str | None = None
    part_number: str | None = None
    sections: list[SectionDefinition]
    poll_interval_seconds: int | None = None

    @field_validator("vendor", "model")
    @classmethod
    def validate_identity_not_whitespace(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if not v.strip():
            raise ValueError("Value must not be blank or whitespace")
        return v

    @field_validator("part_number")
    @classmethod
    def validate_part_number_not_whitespace(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if not v.strip():
            raise ValueError("part_number must not be blank or whitespace")
        return v

    @field_validator("poll_interval_seconds")
    @classmethod
    def _check_poll_interval(cls, v: int | None) -> int | None:
        return _validate_poll_interval(v)

    @model_validator(mode="after")
    def validate_sections(self):
        if len(self.sections) == 0:
            raise ValueError("At least one section is required")
        # hypervisor_id is meaningful only on dynamic templates.
        if self.hypervisor_id is not None and self.template_type != "dynamic":
            raise ValueError("hypervisor_id is only valid on dynamic templates")
        # driver_id rides both device and dynamic templates (device driver, recipe).
        if self.driver_id is not None and self.template_type not in ("device", "dynamic"):
            raise ValueError("driver_id is only valid on device or dynamic templates")
        if self.template_type == "device" and self.driver_id is None:
            raise ValueError("Device templates must have a driver")
        if self.template_type == "dynamic":
            # A dynamic template needs BOTH a recipe driver and a hypervisor.
            if self.driver_id is None:
                raise ValueError("Dynamic templates must have a driver")
            if self.hypervisor_id is None:
                raise ValueError("Dynamic templates must have a hypervisor")
        # vendor/model identity is required only for device templates; dynamic
        # templates are exempt from the "unknown" identity rule.
        if self.template_type == "device":
            if not self.vendor or not self.vendor.strip():
                raise ValueError("vendor is required for device templates")
            if not self.model or not self.model.strip():
                raise ValueError("model is required for device templates")
        return self


class TemplateUpdate(BaseModel):
    name: str | None = None
    driver_id: uuid.UUID | None = None
    hypervisor_id: uuid.UUID | None = None
    exclusive: bool | None = None
    icon: str | None = None
    description: str | None = None
    vendor: str | None = None
    model: str | None = None
    part_number: str | None = None
    sections: list[SectionDefinition] | None = None
    poll_interval_seconds: int | None = None

    @field_validator("vendor", "model")
    @classmethod
    def validate_identity_not_whitespace(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if not v.strip():
            raise ValueError("Value must not be blank or whitespace")
        return v

    @field_validator("poll_interval_seconds")
    @classmethod
    def _check_poll_interval(cls, v: int | None) -> int | None:
        return _validate_poll_interval(v)

    @model_validator(mode="after")
    def validate_sections(self):
        if self.sections is not None and len(self.sections) == 0:
            raise ValueError("At least one section is required")
        return self


class TemplateResponse(BaseModel):
    id: uuid.UUID
    name: str
    template_type: str
    driver_id: uuid.UUID | None = None
    driver_name: str | None = None
    hypervisor_id: uuid.UUID | None = None
    connection_type: str | None = None
    exclusive: bool
    icon: str | None
    description: str | None
    vendor: str
    model: str
    part_number: str | None = None
    sections: list[dict[str, Any]]
    created_at: datetime
    updated_at: datetime
    modified_by: uuid.UUID | None = None
    poll_interval_seconds: int | None = None

    model_config = {"from_attributes": True}


class PaginatedTemplateResponse(BaseModel):
    items: list[TemplateResponse]
    total: int
    skip: int
    limit: int
