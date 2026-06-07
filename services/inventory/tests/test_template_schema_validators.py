"""Validator-branch tests for app.schemas.template.

Pydantic validators are pure and synchronous, so these exercise the error
branches directly via model construction without any DB or HTTP plumbing.
"""

import pytest
from app.schemas.template import (
    FieldDefinition,
    SectionDefinition,
    TemplateCreate,
    TemplateUpdate,
)
from pydantic import ValidationError

_SECTION = {"name": "General", "fields": [{"key": "k", "label": "K", "type": "string"}]}


def _field(**overrides):
    base = {"key": "k", "label": "K", "type": "string"}
    base.update(overrides)
    return FieldDefinition(**base)


# --- FieldDefinition.validate_key_format ------------------------------------


def test_field_key_empty_rejected():
    with pytest.raises(ValidationError, match="must not be empty"):
        _field(key="")


def test_field_key_non_alphanumeric_rejected():
    with pytest.raises(ValidationError, match="alphanumeric"):
        _field(key="has space")


# --- FieldDefinition default-type matching ----------------------------------


def test_string_default_must_be_string():
    with pytest.raises(ValidationError, match="must be a string"):
        _field(type="string", default=5)


def test_number_default_must_be_number():
    with pytest.raises(ValidationError, match="must be a number"):
        _field(type="number", default="nope")


def test_boolean_default_must_be_boolean():
    with pytest.raises(ValidationError, match="must be a boolean"):
        _field(type="boolean", default="true")


def test_dropdown_default_must_be_string():
    with pytest.raises(ValidationError, match="must be a string"):
        _field(type="dropdown", options=["a", "b"], default=1)


def test_dropdown_default_must_be_in_options():
    with pytest.raises(ValidationError, match="not in options"):
        _field(type="dropdown", options=["a", "b"], default="c")


def test_dropdown_requires_options():
    with pytest.raises(ValidationError, match="options list"):
        _field(type="dropdown")


def test_non_dropdown_may_not_have_options():
    with pytest.raises(ValidationError, match="Only dropdown"):
        _field(type="string", options=["a"])


# --- SectionDefinition ------------------------------------------------------


def test_section_name_blank_rejected():
    with pytest.raises(ValidationError, match="must not be empty"):
        SectionDefinition(name="   ", fields=[])


def test_section_duplicate_field_keys_rejected():
    with pytest.raises(ValidationError, match="Duplicate field keys"):
        SectionDefinition(
            name="S",
            fields=[
                {"key": "dup", "label": "A", "type": "string"},
                {"key": "dup", "label": "B", "type": "string"},
            ],
        )


# --- TemplateCreate identity validators -------------------------------------


def test_template_vendor_whitespace_rejected():
    with pytest.raises(ValidationError, match="not be blank"):
        TemplateCreate(
            name="T", driver_id=None, template_type="port", vendor="  ", sections=[_SECTION]
        )


def test_template_part_number_whitespace_rejected():
    with pytest.raises(ValidationError, match="part_number must not be blank"):
        TemplateCreate(
            name="T",
            template_type="port",
            part_number="   ",
            sections=[_SECTION],
        )


def test_template_driver_on_port_template_rejected():
    import uuid

    with pytest.raises(ValidationError, match="driver_id is only valid on device"):
        TemplateCreate(
            name="T",
            template_type="port",
            driver_id=uuid.uuid4(),
            sections=[_SECTION],
        )


def test_device_template_requires_model():
    import uuid

    with pytest.raises(ValidationError, match="model is required"):
        TemplateCreate(
            name="T",
            template_type="device",
            driver_id=uuid.uuid4(),
            vendor="V",
            model=None,
            sections=[_SECTION],
        )


# --- TemplateUpdate validators ----------------------------------------------


def test_template_update_vendor_whitespace_rejected():
    with pytest.raises(ValidationError, match="not be blank"):
        TemplateUpdate(vendor="  ")


def test_template_update_empty_sections_rejected():
    with pytest.raises(ValidationError, match="At least one section"):
        TemplateUpdate(sections=[])


def test_template_update_explicit_none_vendor_passthrough():
    """Explicit None vendor/model hit the None-passthrough branch of the
    TemplateUpdate identity validator."""
    upd = TemplateUpdate(vendor=None, model=None)
    assert upd.vendor is None
    assert upd.model is None


def test_template_update_valid_passthrough():
    """A well-formed update with vendor/model/part_number/poll set hits the
    happy-path returns of the field and model validators."""
    upd = TemplateUpdate(
        vendor="Acme",
        model="M1",
        part_number="PN-1",
        poll_interval_seconds=60,
        sections=[_SECTION],
    )
    assert upd.vendor == "Acme"
    assert upd.poll_interval_seconds == 60


def test_dropdown_option_blank_rejected():
    with pytest.raises(ValidationError, match="empty strings"):
        _field(type="dropdown", options=["a", "  "])


def test_template_create_empty_sections_rejected():
    import uuid

    with pytest.raises(ValidationError, match="At least one section"):
        TemplateCreate(
            name="T",
            template_type="device",
            driver_id=uuid.uuid4(),
            vendor="V",
            model="M",
            sections=[],
        )


def test_device_template_requires_driver():
    with pytest.raises(ValidationError, match="must have a driver"):
        TemplateCreate(
            name="T",
            template_type="device",
            driver_id=None,
            vendor="V",
            model="M",
            sections=[_SECTION],
        )


def test_device_template_requires_vendor():
    import uuid

    with pytest.raises(ValidationError, match="vendor is required"):
        TemplateCreate(
            name="T",
            template_type="device",
            driver_id=uuid.uuid4(),
            vendor=None,
            model="M",
            sections=[_SECTION],
        )


def test_template_create_valid_device_passthrough():
    import uuid

    tc = TemplateCreate(
        name="T",
        template_type="device",
        driver_id=uuid.uuid4(),
        vendor="Acme",
        model="M1",
        part_number="PN-9",
        poll_interval_seconds=120,
        sections=[_SECTION],
    )
    assert tc.part_number == "PN-9"
    assert tc.poll_interval_seconds == 120
