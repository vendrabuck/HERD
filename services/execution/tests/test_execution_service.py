"""Tests for execution_service.py utility functions."""

import uuid

from app.services.execution_service import (
    build_context,
    extract_password_keys,
    redact_context_for_logging,
)


def test_build_context():
    device_data = {
        "name": "L1-Switch-01",
        "connection_type": "Layer 1 Switch",
        "field_data": {
            "ip_address": "10.0.1.50",
            "username": "admin",
            "password": "secret",
        },
    }
    device_id = uuid.uuid4()
    user_id = uuid.uuid4()
    reservation_id = uuid.uuid4()

    ctx = build_context(device_data, device_id, user_id, reservation_id)

    assert ctx["HERD_ip_address"] == "10.0.1.50"
    assert ctx["HERD_username"] == "admin"
    assert ctx["HERD_password"] == "secret"
    assert ctx["HERD_device_id"] == str(device_id)
    assert ctx["HERD_device_name"] == "L1-Switch-01"
    assert ctx["HERD_connection_type"] == "Layer 1 Switch"
    assert ctx["HERD_reservation_id"] == str(reservation_id)
    assert ctx["HERD_user_id"] == str(user_id)


def test_build_context_no_reservation():
    device_data = {
        "name": "Switch",
        "connection_type": "Layer 1 Switch",
        "field_data": {"ip_address": "10.0.1.50"},
    }
    ctx = build_context(device_data, uuid.uuid4(), uuid.uuid4(), None)
    assert ctx["HERD_reservation_id"] is None


def test_build_context_empty_field_data():
    device_data = {"name": "Switch", "connection_type": "Layer 1 Switch", "field_data": {}}
    ctx = build_context(device_data, uuid.uuid4(), uuid.uuid4(), None)
    assert ctx["HERD_device_name"] == "Switch"
    # No field_data keys beyond metadata
    herd_keys = [k for k in ctx if k.startswith("HERD_")]
    assert len(herd_keys) == 5  # device_id, device_name, connection_type, reservation_id, user_id


def test_extract_password_keys():
    template_data = {
        "sections": [
            {
                "name": "Credentials",
                "fields": [
                    {"key": "username", "type": "string"},
                    {"key": "password", "type": "password"},
                    {"key": "enable_password", "type": "password"},
                ],
            },
            {
                "name": "Network",
                "fields": [
                    {"key": "ip_address", "type": "string"},
                    {"key": "port", "type": "number"},
                ],
            },
        ]
    }
    keys = extract_password_keys(template_data)
    assert keys == {"HERD_password", "HERD_enable_password"}


def test_extract_password_keys_empty():
    assert extract_password_keys({}) == set()
    assert extract_password_keys({"sections": []}) == set()


def test_redact_context():
    context = {
        "HERD_ip_address": "10.0.1.50",
        "HERD_password": "secret123",
        "HERD_device_id": "abc",
    }
    password_keys = {"HERD_password"}
    redacted = redact_context_for_logging(context, password_keys)

    assert redacted["HERD_ip_address"] == "10.0.1.50"
    assert redacted["HERD_password"] == "***REDACTED***"
    assert redacted["HERD_device_id"] == "abc"
    # Original not modified
    assert context["HERD_password"] == "secret123"


def test_redact_context_no_passwords():
    context = {"HERD_ip_address": "10.0.1.50"}
    redacted = redact_context_for_logging(context, set())
    assert redacted == context
