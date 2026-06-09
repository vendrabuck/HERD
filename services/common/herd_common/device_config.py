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


class PublishedSchemaError(ValueError):
    """Raised when a driver-published schema is unsafe or not a valid schema.

    Callers treat this as "no usable published schema" and fall back to the
    registry; a driver must never be able to break validation by shipping a
    malformed or hostile config_schema().
    """


def _is_local_ref(ref: str) -> bool:
    """A JSON Schema $ref is local iff it is an in-document JSON Pointer.

    Anything with a scheme/authority (http://, https://, file://, a bare host)
    can cause the validator to fetch a URL, which is SSRF-shaped, so only
    fragment-only pointers like "#/$defs/x" are allowed. An empty string and a
    plain "#" are also local.
    """
    if not isinstance(ref, str):
        return False
    return ref == "" or ref.startswith("#")


def _sanitize_published_schema(schema: Any) -> dict[str, Any]:
    """Validate-and-clean a driver-published schema before it is ever used.

    Security posture (SSRF): a driver could point `$ref` or `$id` at an internal
    URL and the validator would try to FETCH it. So:
      - reject the whole schema if any `$ref` is non-local (raises
        PublishedSchemaError; the caller falls back to the registry), and
      - strip every `$id`/`$anchor` so no remote base URI can be established.
    The schema is pinned to JSON Schema draft 2020-12; the cleaned schema is
    checked with Draft202012Validator.check_schema and rejected if invalid.

    Returns a NEW sanitized dict; the input is not mutated.
    """
    if not isinstance(schema, dict):
        raise PublishedSchemaError("published schema is not a JSON object")

    def _clean(node: Any) -> Any:
        if isinstance(node, dict):
            cleaned: dict[str, Any] = {}
            for key, value in node.items():
                if key == "$ref":
                    if not _is_local_ref(value):
                        raise PublishedSchemaError(
                            f"published schema uses a non-local $ref ({value!r}); "
                            f"remote references are forbidden"
                        )
                    cleaned[key] = value
                    continue
                # Drop base-URI-establishing keywords entirely so no remote base
                # can be resolved against a local $ref or by a $dynamicRef.
                if key in ("$id", "$anchor", "$dynamicAnchor", "$dynamicRef", "$schema"):
                    continue
                cleaned[key] = _clean(value)
            return cleaned
        if isinstance(node, list):
            return [_clean(item) for item in node]
        return node

    cleaned = _clean(schema)
    try:
        jsonschema.Draft202012Validator.check_schema(cleaned)
    except jsonschema.exceptions.SchemaError as exc:
        raise PublishedSchemaError(
            f"published schema is not a valid JSON Schema (draft 2020-12): {exc.message}"
        ) from exc
    return cleaned


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


def validate_device_config_with_schema(
    connection_type: str | None,
    config: dict[str, Any] | None,
    *,
    schema: dict[str, Any] | None = None,
    role: str | None = None,
) -> None:
    """Validate a device config, preferring a driver-published schema.

    When `schema` is None this is byte-for-byte `validate_device_config`
    (the registry path), so every existing driver that omits config_schema()
    behaves identically to today.

    When `schema` is provided it is the driver-published schema. It is
    sanitized (SSRF guard: non-local $ref rejected, $id/$anchor stripped) and
    pinned to draft 2020-12 before use. The same no-op fast path and the same
    role-prefixed error wording as the registry path apply. If the published
    schema is unsafe or invalid (PublishedSchemaError), this raises so the
    CALLER can decide to fall back to the registry; sanitizing here, not at the
    HTTP boundary, keeps the SSRF guard right next to where the schema is used.
    """
    if schema is None:
        validate_device_config(connection_type, config, role=role)
        return

    # Same no-op fast path as the registry validator.
    if not config:
        return

    prefix = f"device {role!r}" if role else "device"
    cleaned = _sanitize_published_schema(schema)

    validator = jsonschema.Draft202012Validator(cleaned)
    error = jsonschema.exceptions.best_match(validator.iter_errors(config))
    if error is not None:
        raise ConfigValidationError(
            f"{prefix}: config failed schema validation: {error.message}"
        ) from error
