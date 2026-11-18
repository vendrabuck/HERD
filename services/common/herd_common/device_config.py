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

import re
from collections.abc import Iterable
from typing import Any

import jsonschema

from herd_common.config_redaction import is_keyword_token, redact_command_text

# Connection types whose driver CONTRACT includes `configure` (issue #839).
# Capability comes from the contract, not a declared flag and not code
# inspection: services/execution/app/services/driver_loader.py's
# REQUIRED_METHODS lists the methods a connection type's driver must implement,
# enforced at driver load, and only "Management" requires `configure`. A
# config APPLY is therefore only ever attempted against a Management driver;
# see connection_type_supports_configure below and the parity test in
# services/execution/tests that pins this set against REQUIRED_METHODS
# directly, so the two cannot silently drift apart.
CONFIGURE_CONNECTION_TYPES: frozenset[str] = frozenset({"Management"})


def connection_type_supports_configure(connection_type: str | None) -> bool:
    """Whether a driver's connection type contract includes `configure`.

    True only for a connection type in CONFIGURE_CONNECTION_TYPES (today just
    "Management"); False for every other known type, an unknown string, and
    None. This governs the config-apply GATE only: config VERSIONS remain
    creatable for every connection type CONFIG_SCHEMAS below covers, since a
    Layer 2 or Layer 3 Switch config version stores routing/VLAN intent
    (ADR 0014) that nothing ever applies through this generic path.
    """
    return connection_type in CONFIGURE_CONNECTION_TYPES


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
                        # ADR 0014 addendum X-J (issue #756). "kind" says whether
                        # this interface is a physical port of the device
                        # (the default) or a logical construct (a loopback, an
                        # SVI, a dummy device enslaved to a VRF), which decides
                        # whether the X-K interface-level attachment check applies
                        # to a route naming it. "port" is the HERD inventory port
                        # name a physical interface corresponds to, for a device
                        # whose OS names its interfaces differently from the port
                        # names its inventory record carries; it defaults to
                        # "name", which is what a config written before X-J, and
                        # the repo's own eth0-to-eth0 fixtures, rely on.
                        "kind": {
                            "type": "string",
                            "enum": ["physical", "logical"],
                        },
                        "port": {"type": "string", "minLength": 1, "maxLength": 64},
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
    """Raised when a device's proposed config fails schema validation.

    Its text never holds a value taken from the config (issue #1457), so a
    caller may return it as a 422 detail and store it on a row as is. The text
    is `<prefix>: <reason>`: `prefix` is `device` or `device '<name>'` (the
    device's name, inventory data, quoted with `repr`) and `reason` is the rest.
    They are kept apart so a caller that masks the text masks only `reason`
    and a device name can never eat the refusal (execution, CFG-EXEC-10).
    """

    def __init__(self, prefix: str, reason: str) -> None:
        super().__init__(f"{prefix}: {reason}")
        self.prefix = prefix
        self.reason = reason


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


# --- Refusal text (issue #1457) -----------------------------------------------
#
# jsonschema's own `message` quotes the part of the submitted config that failed
# (`'<line>' is too long`, `[<the whole list>] is too long`), and a config can
# carry a credential: the FRR management driver's `commands` are free-text vtysh
# lines (`neighbor ... password ...`). Execution stores the refusal on the run
# row and every caller returns it as a 422, so the refusal is built here from
# the error's location and keyword and the schema's own data only, never from
# `error.message` or `error.instance`.
#
# What a refusal may name:
# - the JSON path to the failing part, built from the config's KEYS and list
#   indexes. Keys are names and are shown: each one is masked with
#   `config_redaction.redact_command_text` (a key that reads like a command line
#   keeps nothing after a credential keyword), cut to _MAX_NAME_CHARS, and
#   quoted with `repr` (so a control character or a lone surrogate is written
#   as its escape) unless it is a plain identifier;
# - the failed keyword, as fixed words (`must be at most 512 characters long`).
#   The words call a key a "field" and never use a credential keyword of
#   `config_redaction` (`key`, `secret`, `password`, ...), so execution's second
#   masking pass over the stored refusal leaves a clean refusal unchanged;
# - the schema's numbers (bounds, counts) and JSON type names, which cannot
#   carry free text, and the key names a schema requires or forbids;
# - the schema's free text (an `enum` or `const` value, a `pattern`) only when
#   `show_schema_values` is true, which the registry path sets (HERD wrote that
#   text) and the published-schema path does not (a driver wrote it; a user
#   reads it from `GET /drivers/{id}/config-schema` instead).
#
# A value from the config never appears: not a string, not a number, not an
# item of a list, not a key's value.

_JSON_TYPES = frozenset({"array", "boolean", "integer", "null", "number", "object", "string"})
_PLAIN_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_SCHEMA_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{0,63}")
_FORMAT_NAME = re.compile(r"[A-Za-z0-9-]{1,32}")
_MAX_NAME_CHARS = 64
_MAX_PATH_PARTS = 16
_MAX_LISTED = 10
_MAX_SCHEMA_TEXT_CHARS = 128


def _name_text(key: Any) -> str:
    """A key as a refusal shows it: masked, cut, and quoted with `repr`."""
    text = key if isinstance(key, str) else str(key)
    text, _ = redact_command_text(text)
    if len(text) > _MAX_NAME_CHARS:
        text = text[:_MAX_NAME_CHARS] + "..."
    return repr(text)


def _path_text(path: Iterable[Any]) -> str:
    """`commands[0]`, `interfaces[2].zone`, `vlan_assignments['ge-0/0/1']`."""
    parts = list(path)
    if not parts:
        return "the top level"
    out = ""
    for part in parts[:_MAX_PATH_PARTS]:
        if isinstance(part, int) and not isinstance(part, bool):
            out += f"[{part}]"
        elif (
            isinstance(part, str)
            and len(part) <= _MAX_NAME_CHARS
            and _PLAIN_KEY.fullmatch(part)
            and (out or not is_keyword_token(part))
        ):
            # A path that STARTS with a bare credential keyword (`password: ...`)
            # would read to the stored-error mask as a credential line; it is
            # written quoted (`['password']`) instead (issue #1464).
            out += f".{part}" if out else part
        else:
            out += f"[{_name_text(part)}]"
    if len(parts) > _MAX_PATH_PARTS:
        out += "[...]"
    return out


def _names_text(names: list[Any]) -> str:
    shown = ", ".join(_name_text(name) for name in names[:_MAX_LISTED])
    if len(names) > _MAX_LISTED:
        shown += f", and {len(names) - _MAX_LISTED} more"
    return shown


def _is_schema_name(name: Any) -> bool:
    """Whether a field name a published schema defines may be shown: a plain
    name of the kind the registry's own fields have (`vlan_assignments`,
    `next_hop`), with `-` and `.` also allowed, at most _MAX_NAME_CHARS long."""
    return isinstance(name, str) and _SCHEMA_NAME.fullmatch(name) is not None


def _schema_names_text(names: list[Any], show_all: bool) -> str:
    """`the field 'a'`, `the fields 'a', 'b'`, or, for names a published schema
    defines that are not plain, `a required field` or `2 required fields`:
    `the field 'a' and 1 other required field` when some are plain."""
    shown = names if show_all else [n for n in names if _is_schema_name(n)]
    hidden = len(names) - len(shown)
    if not shown:
        return "a required field" if hidden == 1 else f"{hidden} required fields"
    text = f"the {_plural(len(shown), 'field', 'fields')} {_names_text(shown)}"
    if hidden:
        text += f" and {hidden} other required {_plural(hidden, 'field', 'fields')}"
    return text


def _number_text(value: Any) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return str(value)


def _schema_value_text(value: Any) -> str | None:
    """An `enum` or `const` value from the schema, or None when it is not a
    short scalar (a refusal then names the rule without the value)."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str) and len(value) <= _MAX_SCHEMA_TEXT_CHARS:
        return repr(value)
    return None


def _plural(count: Any, one: str, many: str) -> str:
    return one if count == 1 else many


def _bound_rule(keyword: str, bound: Any) -> str | None:
    """The rule for a keyword whose schema value is a number."""
    number = _number_text(bound)
    if number is None:
        return None
    rules = {
        "minLength": f"must be at least {number} {_plural(bound, 'character', 'characters')} long",
        "maxLength": f"must be at most {number} {_plural(bound, 'character', 'characters')} long",
        "minItems": f"must have at least {number} {_plural(bound, 'item', 'items')}",
        "maxItems": f"must have at most {number} {_plural(bound, 'item', 'items')}",
        "minProperties": f"must have at least {number} {_plural(bound, 'field', 'fields')}",
        "maxProperties": f"must have at most {number} {_plural(bound, 'field', 'fields')}",
        "minimum": f"must be at least {number}",
        "maximum": f"must be at most {number}",
        "exclusiveMinimum": f"must be greater than {number}",
        "exclusiveMaximum": f"must be less than {number}",
        "multipleOf": f"must be a multiple of {number}",
        "minContains": (
            f"must have at least {number} {_plural(bound, 'item', 'items')} "
            "that match the schema's contains rule"
        ),
        "maxContains": (
            f"must have at most {number} {_plural(bound, 'item', 'items')} "
            "that match the schema's contains rule"
        ),
    }
    return rules.get(keyword)


def _disallowed_keys(error: jsonschema.ValidationError) -> list[Any]:
    """The keys an `additionalProperties: false` refused, computed the way
    jsonschema does: not in `properties`, matching no `patternProperties`."""
    instance = error.instance
    schema = error.schema if isinstance(error.schema, dict) else {}
    if not isinstance(instance, dict):
        return []
    declared = schema.get("properties") or {}
    patterns = list((schema.get("patternProperties") or {}).keys())
    extras = []
    for key in instance:
        if key in declared:
            continue
        matched = False
        for pattern in patterns:
            try:
                if re.search(pattern, key):
                    matched = True
                    break
            except (re.error, TypeError):
                continue
        if not matched:
            extras.append(key)
    return extras


def _rule_text(error: jsonschema.ValidationError, *, show_schema_values: bool) -> str:
    """The failed rule in fixed words, from the keyword and the schema only."""
    keyword = error.validator
    expected = error.validator_value

    bound = _bound_rule(keyword, expected) if isinstance(keyword, str) else None
    if bound is not None:
        return bound

    if keyword == "type":
        types = [expected] if isinstance(expected, str) else list(expected or [])
        shown = [t for t in types if isinstance(t, str) and t in _JSON_TYPES]
        if shown:
            return f"must be of type {' or '.join(shown)}"
        return "has the wrong type"
    if keyword == "enum":
        if show_schema_values and isinstance(expected, list) and len(expected) <= _MAX_LISTED:
            values = [_schema_value_text(v) for v in expected]
            if values and all(v is not None for v in values):
                return f"must be one of {', '.join(values)}"
        return "must be one of the values the schema allows"
    if keyword == "const":
        value = _schema_value_text(expected) if show_schema_values else None
        if value is not None:
            return f"must equal {value}"
        return "must equal the value the schema fixes"
    if keyword == "pattern":
        if (
            show_schema_values
            and isinstance(expected, str)
            and len(expected) <= _MAX_SCHEMA_TEXT_CHARS
        ):
            return f"must match the pattern {expected!r}"
        return "must match the schema's pattern"
    if keyword == "format":
        if isinstance(expected, str) and _FORMAT_NAME.fullmatch(expected):
            return f"must be a valid {expected}"
        return "must have the format the schema names"
    if keyword == "required":
        instance = error.instance if isinstance(error.instance, dict) else {}
        missing = [n for n in (expected or []) if isinstance(n, str) and n not in instance]
        if missing:
            return f"needs {_schema_names_text(missing, show_schema_values)}"
        return "is missing a required field"
    if keyword == "dependentRequired":
        instance = error.instance if isinstance(error.instance, dict) else {}
        for present, needed in (expected or {}).items():
            if present not in instance or not isinstance(needed, list):
                continue
            missing = [n for n in needed if isinstance(n, str) and n not in instance]
            if missing:
                if show_schema_values or _is_schema_name(present):
                    subject = f"field {_name_text(present)}"
                else:
                    subject = "a field"
                return f"{subject} needs {_schema_names_text(missing, show_schema_values)}"
        return "is missing a field another field depends on"
    if keyword == "additionalProperties":
        extras = _disallowed_keys(error)
        if extras:
            noun = _plural(len(extras), "field", "fields")
            return f"does not allow the {noun} {_names_text(extras)}"
        return "has a field the schema does not allow"
    if keyword == "unevaluatedProperties":
        return "has a field the schema does not allow"
    if keyword in ("items", "unevaluatedItems"):
        return "has more items than the schema allows"
    if keyword == "contains":
        return "must have an item that matches the schema's contains rule"
    if keyword == "uniqueItems":
        return "must not repeat an item"
    if keyword == "oneOf":
        return "must match exactly one of the shapes the schema allows"
    if keyword == "anyOf":
        return "must match at least one of the shapes the schema allows"
    if keyword == "allOf":
        return "must match every shape the schema requires"
    if keyword == "not":
        return "must not match the shape the schema excludes"
    if keyword is None:
        # A `false` subschema (`"properties": {"x": false}`, `"items": false`).
        # jsonschema records no path step for it, so the location is its parent.
        return "holds a value the schema does not allow here"
    return "does not satisfy the schema"


def schema_error_text(error: jsonschema.ValidationError, *, show_schema_values: bool) -> str:
    """Where a config failed and which rule it broke, never what it held.

    `at <path>: <rule>`, for example `at commands[0]: must be at most 512
    characters long`. An error from inside `propertyNames` is about a key's
    name, so its rule is prefixed `a field name`. See the block comment above for
    what may appear.
    """
    rule = _rule_text(error, show_schema_values=show_schema_values)
    if "propertyNames" in error.absolute_schema_path:
        rule = f"a field name {rule}"
    return f"at {_path_text(error.absolute_path)}: {rule}"


# The refusal text before issue #1457: `<prefix>: config failed schema
# validation: <jsonschema message>`, whose message quotes the config. The prefix
# is `device` or `device ` and a Python `repr` string (single or double quoted,
# with backslash escapes), so this anchored pattern reads the prefix exactly and
# then needs `: ` right after `validation`; today's text always has ` at `
# there, so it never matches one.
_LEGACY_REFUSAL = re.compile(
    r"""(device(?: (?:'(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*"))?)"""
    r": config failed schema validation: ",
    re.DOTALL,
)
LEGACY_REFUSAL_WITHHELD = (
    "config failed schema validation "
    "(details withheld; recorded before values were removed from these messages)"
)


def withhold_legacy_refusal_text(text: str | None) -> str | None:
    """A stored refusal as a reader may see it.

    A run refused before issue #1457 stored jsonschema's message, which can
    quote the refused config, credentials included. Such text keeps its prefix
    and gets LEGACY_REFUSAL_WITHHELD in place of the rest; any other text,
    today's refusals included, is returned unchanged. Execution applies it to
    every run it answers (`ExecutionRunResponse`), so the stored row is left as
    it is and the API and the assistant never see the old message.
    """
    if not text:
        return text
    match = _LEGACY_REFUSAL.match(text)
    if match is None:
        return text
    return f"{match.group(1)}: {LEGACY_REFUSAL_WITHHELD}"


def _refusal(
    prefix: str,
    config: dict[str, Any],
    schema: dict[str, Any],
    *,
    show_schema_values: bool,
) -> ConfigValidationError | None:
    """The refusal for `config`, or None when it satisfies `schema`.

    The error is BUILT, not raised from inside an `except` block, so the
    jsonschema error (whose message quotes the config) is neither the cause nor
    the context of the ConfigValidationError, and no traceback a logger prints
    can carry it.
    """
    validator = jsonschema.Draft202012Validator(schema)
    error = jsonschema.exceptions.best_match(validator.iter_errors(config))
    if error is None:
        return None
    text = schema_error_text(error, show_schema_values=show_schema_values)
    return ConfigValidationError(prefix, f"config failed schema validation {text}")


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
    the caller can surface a useful 422 detail to the client. A schema refusal
    reads `<prefix>: config failed schema validation at <path>: <rule>`
    (`schema_error_text`): it names where the config is wrong and which rule it
    broke, with the registry's own allowed values, and never a value taken from
    the config.
    """
    if not config:
        return

    prefix = f"device {role!r}" if role else "device"

    if connection_type is None:
        raise ConfigValidationError(
            prefix,
            "config was provided but connection_type is missing; "
            "cannot validate against any driver schema",
        )

    schema = CONFIG_SCHEMAS.get(connection_type)
    if schema is None:
        raise ConfigValidationError(
            prefix,
            f"connection_type {connection_type!r} does not accept "
            "a configure action; remove `config` from this device",
        )

    refusal = _refusal(prefix, config, schema, show_schema_values=True)
    if refusal is not None:
        raise refusal


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
    role-prefixed error wording as the registry path apply, except that a
    refusal does not quote the driver's own free text (an `enum` or `const`
    value, a `pattern`); numbers, type names, and key names are shown on both
    paths. If the published
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

    refusal = _refusal(prefix, config, cleaned, show_schema_values=False)
    if refusal is not None:
        raise refusal
