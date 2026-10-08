"""Masking of password-typed values on non-admin inventory reads.

One module owns the rule so device, port, and template reads cannot drift:
a field the template declares as type "password" never leaves the service in
clear to a non-admin caller. Device and port reads mask the stored
``field_data`` value; template reads mask the field's ``default``, which a
device created without a value for the field inherits. Admin reads and the
internal token-gated reads return values in clear.
"""

import copy
from typing import Any

from app.models.template import DeviceTemplate

# Sentinel written over password-typed values on non-admin reads. Matches the
# config service's secret masking so the frontend renders a stable, obviously
# redacted value. The key is retained (only its value is replaced) so the
# response shape is unchanged for the UI.
REDACTED_VALUE = "********"


def password_field_keys(template: DeviceTemplate | None) -> set[str]:
    """Bare field_data keys whose template field is type "password".

    Mirrors the execution service's extract_password_keys, but over the bare keys
    used in a device's field_data (execution prefixes them with HERD_ for the run
    context env; inventory stores them unprefixed). Defensive against malformed
    sections so a bad template never breaks a read.
    """
    if template is None or not template.sections:
        return set()
    keys: set[str] = set()
    for section in template.sections:
        if not isinstance(section, dict):
            continue
        for field in section.get("fields", []):
            if isinstance(field, dict) and field.get("type") == "password":
                key = field.get("key")
                if key:
                    keys.add(key)
    return keys


def redact_field_data(field_data: dict, password_keys: set[str]) -> dict:
    """Replace truthy password-typed values with the redaction sentinel.

    Empty/None values are left untouched so a redacted response never implies a
    secret exists where none was set.
    """
    if not password_keys:
        return field_data
    return {k: (REDACTED_VALUE if k in password_keys and v else v) for k, v in field_data.items()}


def redact_template_sections(sections: list[Any] | None) -> list[Any]:
    """Return a deep copy of ``sections`` with password-field defaults masked.

    Every field of type "password" whose ``default`` is a non-empty string gets
    the sentinel in its place; an empty or null default is left as it is, so a
    masked default always means a default is set. Other fields are untouched.
    The input, which is the stored ORM value, is never mutated. Malformed
    sections and fields pass through unchanged, as in password_field_keys.
    """
    redacted = copy.deepcopy(sections) if sections else []
    for section in redacted:
        if not isinstance(section, dict):
            continue
        fields = section.get("fields")
        if not isinstance(fields, list):
            continue
        for field in fields:
            if not isinstance(field, dict) or field.get("type") != "password":
                continue
            default = field.get("default")
            if isinstance(default, str) and default:
                field["default"] = REDACTED_VALUE
    return redacted
