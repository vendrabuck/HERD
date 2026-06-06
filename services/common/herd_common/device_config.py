"""Validate per-device configs against an allowlist before they reach a driver
sandbox or get persisted as a config version.

Originally lived in services/ai-orchestrator/app/services/config_validator.py;
moved here so the inventory service can validate config-version writes without
depending on the AI orchestrator. The orchestrator re-exports these symbols
from its old module to keep import paths stable for tests.

The schemas here are HERD's view of safe driver inputs for the `configure`
action. A future follow-up can let each driver publish its own method schema
via the execution service; until then this registry is the single source of
truth.
"""

from __future__ import annotations

from typing import Any

import jsonschema

CONFIG_SCHEMAS: dict[str, dict[str, Any]] = {
    "Management": {
        "type": "object",
        "properties": {
            "vlan": {"type": "integer", "minimum": 1, "maximum": 4094},
            "ip": {"type": "string", "minLength": 1, "maxLength": 64},
            "hostname": {"type": "string", "minLength": 1, "maxLength": 128},
            "description": {"type": "string", "maxLength": 500},
        },
        "additionalProperties": False,
    },
    "Layer 2 Switch": {
        "type": "object",
        "properties": {
            "vlan_assignments": {
                "type": "object",
                "patternProperties": {
                    "^.+$": {"type": "integer", "minimum": 1, "maximum": 4094},
                },
                "additionalProperties": False,
            },
            "description": {"type": "string", "maxLength": 500},
        },
        "additionalProperties": False,
    },
    # Generic, vendor-neutral Layer 3 forwarding config. Models a routed device
    # as a set of zoned interfaces, one or more virtual routers that each list
    # their member interfaces, and a routing table. This is deliberately not
    # tied to any vendor's config syntax; it is HERD's own representation of the
    # L3 concepts (interface, zone, virtual router, route) so the assistant can
    # reason about connectivity, e.g. an interface that exists in a zone but is
    # not a member of any virtual router (and has no route out) cannot forward.
    "Layer 3 Switch": {
        "type": "object",
        "properties": {
            "interfaces": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "minLength": 1, "maxLength": 64},
                        "ip": {"type": "string", "minLength": 1, "maxLength": 64},
                        "zone": {
                            "type": "string",
                            "enum": ["trust", "untrust", "dmz"],
                        },
                    },
                    "required": ["name", "zone"],
                    "additionalProperties": False,
                },
            },
            "virtual_routers": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "minLength": 1, "maxLength": 64},
                        "interfaces": {
                            "type": "array",
                            "items": {"type": "string", "minLength": 1, "maxLength": 64},
                        },
                    },
                    "required": ["name", "interfaces"],
                    "additionalProperties": False,
                },
            },
            "routes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "destination": {"type": "string", "minLength": 1, "maxLength": 64},
                        "next_hop": {"type": "string", "minLength": 1, "maxLength": 64},
                        "interface": {"type": "string", "minLength": 1, "maxLength": 64},
                    },
                    "required": ["destination", "interface"],
                    "additionalProperties": False,
                },
            },
            "description": {"type": "string", "maxLength": 500},
        },
        "additionalProperties": False,
    },
}

ALLOWED_CONFIG_KEYS: list[str] = sorted(
    {key for schema in CONFIG_SCHEMAS.values() for key in schema.get("properties", {})}
)


class ConfigValidationError(ValueError):
    """Raised when a device's proposed config fails schema validation."""


def validate_device_config(
    connection_type: str | None,
    config: dict[str, Any] | None,
    *,
    role: str | None = None,
) -> None:
    """Validate a single device's config against the registry.

    No config (None or empty) is always allowed; it means no-op. Configs on
    a connection_type without a schema are rejected. Known connection types
    are validated via jsonschema.

    Raises ConfigValidationError with a role-prefixed message on failure so
    the caller can surface a useful 422 detail to the client.
    """
    if not config:
        return

    prefix = f"device {role!r}" if role else "device"

    if connection_type is None:
        raise ConfigValidationError(
            f"{prefix}: config was provided but connection_type is missing; "
            f"cannot validate against any driver schema"
        )

    schema = CONFIG_SCHEMAS.get(connection_type)
    if schema is None:
        raise ConfigValidationError(
            f"{prefix}: connection_type {connection_type!r} does not accept "
            f"a configure action; remove `config` from this device"
        )

    try:
        jsonschema.validate(instance=config, schema=schema)
    except jsonschema.ValidationError as exc:
        raise ConfigValidationError(
            f"{prefix}: config failed schema validation: {exc.message}"
        ) from exc
