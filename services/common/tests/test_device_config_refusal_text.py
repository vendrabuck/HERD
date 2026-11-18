"""A config refusal names where and which rule, never what was submitted (issue #1457).

`herd_common.device_config` used to put jsonschema's own `message` into the
refusal, and that message quotes the failing part of the config
(`'<line>' is too long`, `[<the whole list>] is too long`). A credential in an FRR
vtysh line therefore reached the 422 and the execution run's stored `error`.

Every case below puts a canary in the submitted VALUE and checks that the
refusal holds the path and the rule and not the canary. The table covers every
keyword the draft 2020-12 validator knows (`test_every_validator_keyword_has_a_case`
fails when a jsonschema upgrade adds one), on the published-schema path, plus
the registry path, the FRR management driver's real schema, nested combinators,
arrays, deep paths, and the key-name rules.

Mutation proof: putting `error.message` back into `_refusal` fails every case.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import jsonschema
import pytest
from herd_common.config_redaction import redact_command_text
from herd_common.device_config import (
    ConfigValidationError,
    schema_error_text,
    validate_device_config,
    validate_device_config_with_schema,
)

CANARY = "S3cr3t-Canary-9d1e"
# A number is a value too: a refusal must not quote it either.
NUMBER_CANARY = 918273645

_REPO_ROOT = Path(__file__).resolve().parents[3]


def _frr_published_schema() -> dict:
    """The schema drivers/frr_mgmt publishes, read from the driver as shipped."""
    path = _REPO_ROOT / "drivers" / "frr_mgmt" / "driver.py"
    spec = importlib.util.spec_from_file_location("frr_mgmt_schema_source", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.Driver.config_schema()


FRR_SCHEMA = _frr_published_schema()


def _refusal(config: dict, schema: dict | None, *, connection_type="Management") -> str:
    with pytest.raises(ConfigValidationError) as exc:
        validate_device_config_with_schema(connection_type, config, schema=schema, role="r1")
    return str(exc.value)


def _obj(**properties) -> dict:
    return {"type": "object", "properties": properties}


# keyword: (published schema, config, the exact text after the prefix)
KEYWORD_CASES: dict[str, tuple[dict, dict, str]] = {
    "type": (
        _obj(command={"type": "string"}),
        {"command": [f"enable password {CANARY}"]},
        "at command: must be of type string",
    ),
    "enum": (
        _obj(zone={"enum": ["trust", "dmz"]}),
        {"zone": CANARY},
        "at zone: must be one of the values the schema allows",
    ),
    "const": (
        _obj(mode={"const": "fixed"}),
        {"mode": CANARY},
        "at mode: must equal the value the schema fixes",
    ),
    "pattern": (
        _obj(password={"type": "string", "pattern": "^[a-z]+$"}),
        {"password": CANARY},
        "at password: must match the schema's pattern",
    ),
    "minLength": (
        _obj(line={"type": "string", "minLength": 50}),
        {"line": CANARY},
        "at line: must be at least 50 characters long",
    ),
    "maxLength": (
        _obj(line={"type": "string", "maxLength": 1}),
        {"line": CANARY},
        "at line: must be at most 1 character long",
    ),
    "minItems": (
        _obj(lines={"type": "array", "minItems": 3}),
        {"lines": [CANARY]},
        "at lines: must have at least 3 items",
    ),
    "maxItems": (
        _obj(lines={"type": "array", "maxItems": 1}),
        {"lines": [CANARY, CANARY + "-2"]},
        "at lines: must have at most 1 item",
    ),
    "minProperties": (
        _obj(block={"type": "object", "minProperties": 2}),
        {"block": {"a": CANARY}},
        "at block: must have at least 2 fields",
    ),
    "maxProperties": (
        _obj(block={"type": "object", "maxProperties": 1}),
        {"block": {"a": CANARY, "b": CANARY}},
        "at block: must have at most 1 field",
    ),
    "minimum": (
        _obj(n={"minimum": 10}),
        {"n": -NUMBER_CANARY},
        "at n: must be at least 10",
    ),
    "maximum": (
        _obj(n={"maximum": 4094}),
        {"n": NUMBER_CANARY},
        "at n: must be at most 4094",
    ),
    "exclusiveMinimum": (
        _obj(n={"exclusiveMinimum": 0}),
        {"n": -NUMBER_CANARY},
        "at n: must be greater than 0",
    ),
    "exclusiveMaximum": (
        _obj(n={"exclusiveMaximum": 100}),
        {"n": NUMBER_CANARY},
        "at n: must be less than 100",
    ),
    "multipleOf": (
        _obj(n={"multipleOf": 7}),
        {"n": NUMBER_CANARY},
        "at n: must be a multiple of 7",
    ),
    "required": (
        _obj(neighbor={"type": "object", "required": ["address", "remote_as"]}),
        {"neighbor": {"password": CANARY}},
        "at neighbor: needs the fields 'address', 'remote_as'",
    ),
    "dependentRequired": (
        _obj(neighbor={"type": "object", "dependentRequired": {"password": ["address"]}}),
        {"neighbor": {"password": CANARY}},
        "at neighbor: field 'password' needs the field 'address'",
    ),
    "dependentSchemas": (
        _obj(neighbor={"type": "object", "dependentSchemas": {"password": {"required": ["md5"]}}}),
        {"neighbor": {"password": CANARY}},
        "at neighbor: needs the field 'md5'",
    ),
    "additionalProperties": (
        {"type": "object", "properties": {"commands": {}}, "additionalProperties": False},
        {"commands": [], "password": CANARY},
        "at the top level: does not allow the field 'password'",
    ),
    "unevaluatedProperties": (
        {"type": "object", "properties": {"commands": {}}, "unevaluatedProperties": False},
        {"commands": [], "password": CANARY},
        "at the top level: has a field the schema does not allow",
    ),
    "patternProperties": (
        {"type": "object", "patternProperties": {"^ge-": {"type": "integer"}}},
        {"ge-0/0/1": CANARY},
        "at ['ge-0/0/1']: must be of type integer",
    ),
    "properties": (
        _obj(hostname={"type": "string", "maxLength": 4}),
        {"hostname": CANARY},
        "at hostname: must be at most 4 characters long",
    ),
    "propertyNames": (
        _obj(block={"type": "object", "propertyNames": {"maxLength": 3}}),
        {"block": {"abcd": CANARY}},
        "at block: a field name must be at most 3 characters long",
    ),
    "items": (
        {
            "type": "object",
            "properties": {"pair": {"type": "array", "prefixItems": [{}], "items": False}},
        },
        {"pair": ["ok", CANARY]},
        "at pair: has more items than the schema allows",
    ),
    "prefixItems": (
        _obj(pair={"type": "array", "prefixItems": [{"type": "integer"}]}),
        {"pair": [CANARY]},
        "at pair[0]: must be of type integer",
    ),
    "unevaluatedItems": (
        _obj(pair={"type": "array", "prefixItems": [{}], "unevaluatedItems": False}),
        {"pair": ["ok", CANARY]},
        "at pair: has more items than the schema allows",
    ),
    "contains": (
        _obj(lines={"type": "array", "contains": {"const": "end"}}),
        {"lines": [CANARY]},
        "at lines: must have an item that matches the schema's contains rule",
    ),
    "uniqueItems": (
        _obj(lines={"type": "array", "uniqueItems": True}),
        {"lines": [CANARY, CANARY]},
        "at lines: must not repeat an item",
    ),
    "oneOf": (
        _obj(x={"oneOf": [{"type": "string"}, {"maxLength": 100}]}),
        {"x": CANARY},
        "at x: must match exactly one of the shapes the schema allows",
    ),
    "anyOf": (
        _obj(x={"anyOf": [{"type": "integer"}, {"type": "null"}]}),
        {"x": CANARY},
        "at x: must match at least one of the shapes the schema allows",
    ),
    "allOf": (
        _obj(x={"allOf": [{"type": "string"}, {"maxLength": 3}]}),
        {"x": CANARY},
        "at x: must be at most 3 characters long",
    ),
    "not": (
        _obj(x={"not": {"type": "string"}}),
        {"x": CANARY},
        "at x: must not match the shape the schema excludes",
    ),
    "if": (
        {
            "type": "object",
            "if": {"properties": {"auth": {"const": "md5"}}},
            "then": {"required": ["key_id"]},
        },
        {"auth": "md5", "password": CANARY},
        "at the top level: needs the field 'key_id'",
    ),
    "$ref": (
        {
            "type": "object",
            "$defs": {"line": {"type": "string", "maxLength": 5}},
            "properties": {"x": {"$ref": "#/$defs/line"}},
        },
        {"x": CANARY},
        "at x: must be at most 5 characters long",
    ),
}

# Keywords with no case above, each with the reason.
EXEMPT_KEYWORDS = {
    # Removed from every published schema before use (CFG-SCHEMA-5), and the
    # registry does not use it.
    "$dynamicRef": "stripped by _sanitize_published_schema",
    # Neither validator is built with a format checker, so `format` never
    # refuses; `test_format_rule_names_the_format_only` covers the builder.
    "format": "not asserted by the validators",
}


def test_every_validator_keyword_has_a_case():
    """A jsonschema upgrade that adds a keyword fails here until it gets a case."""
    known = set(jsonschema.Draft202012Validator.VALIDATORS)
    assert set(KEYWORD_CASES) | set(EXEMPT_KEYWORDS) == known
    assert not set(KEYWORD_CASES) & set(EXEMPT_KEYWORDS)


@pytest.mark.parametrize("keyword", sorted(KEYWORD_CASES))
def test_refusal_names_path_and_rule_never_the_value(keyword):
    schema, config, expected = KEYWORD_CASES[keyword]
    message = _refusal(config, schema)
    assert message == f"device 'r1': config failed schema validation {expected}"
    assert CANARY not in message
    assert str(NUMBER_CANARY) not in message


@pytest.mark.parametrize("keyword", sorted(KEYWORD_CASES))
def test_refusal_words_survive_the_stored_error_mask_unchanged(keyword):
    """Execution masks the refusal with redact_command_text before it stores it
    (CFG-EXEC-8's rule). The builder's fixed words must not trip that mask, or a
    clean refusal would lose its rule (`does not allow the key ...` once became
    `does not allow the key [redacted]`, `key` being a credential keyword)."""
    schema, config, _ = KEYWORD_CASES[keyword]
    message = _refusal(config, schema)
    assert redact_command_text(message) == (message, False)


@pytest.mark.parametrize("keyword", ["minContains", "maxContains"])
def test_contains_counts_name_the_bound(keyword):
    bound = 2 if keyword == "minContains" else 1
    schema = _obj(lines={"type": "array", "contains": {"type": "string"}, keyword: bound})
    config = {"lines": [CANARY, 1] if keyword == "minContains" else [CANARY, CANARY + "x"]}
    message = _refusal(config, schema)
    word = "least" if keyword == "minContains" else "most"
    noun = "items" if bound != 1 else "item"
    assert message.endswith(
        f"at lines: must have at {word} {bound} {noun} that match the schema's contains rule"
    )
    assert CANARY not in message


def test_false_subschema_names_no_value():
    """jsonschema records no path step for a `false` subschema, so the location
    is the parent object."""
    message = _refusal({"secret": CANARY}, _obj(secret=False))
    assert message.endswith("at the top level: holds a value the schema does not allow here")
    assert CANARY not in message


# --- The FRR management driver's schema, as shipped -------------------------


def test_frr_schema_is_read_from_the_driver():
    assert FRR_SCHEMA["properties"]["commands"]["items"]["maxLength"] == 512
    assert FRR_SCHEMA["properties"]["commands"]["maxItems"] == 100


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        (
            {"commands": [f"neighbor 192.0.2.1 password {CANARY} " + "x" * 500]},
            "at commands[0]: must be at most 512 characters long",
        ),
        (
            {"commands": [f"neighbor 192.0.2.1 password {CANARY}"] + ["!"] * 100},
            "at commands: must have at most 100 items",
        ),
        (
            {"command": [f"enable password {CANARY}"]},
            "at command: must be of type string",
        ),
        (
            {"commands": ["router bgp 65000", ""], "command": CANARY},
            "at commands[1]: must be at least 1 character long",
        ),
        (
            {"commands": ["ok"], "neighbor_password": CANARY},
            "at the top level: does not allow the field 'neighbor_password'",
        ),
    ],
)
def test_frr_schema_refusal_never_quotes_a_line(config, expected):
    message = _refusal(config, FRR_SCHEMA)
    assert message == f"device 'r1': config failed schema validation {expected}"
    assert CANARY not in message


# --- The registry path ---------------------------------------------------------


@pytest.mark.parametrize(
    ("connection_type", "config", "expected"),
    [
        ("Management", {"vlan": NUMBER_CANARY}, "at vlan: must be at most 4094"),
        (
            "Management",
            {"hostname": CANARY * 10},
            "at hostname: must be at most 128 characters long",
        ),
        ("Management", {"ip": [CANARY]}, "at ip: must be of type string"),
        (
            "Management",
            {"hostname": "ok", "bogus": CANARY},
            "at the top level: does not allow the field 'bogus'",
        ),
        (
            "Layer 2 Switch",
            {"vlan_assignments": {"ge-0/0/1": CANARY}},
            "at vlan_assignments['ge-0/0/1']: must be of type integer",
        ),
        (
            "Layer 3 Switch",
            {"interfaces": [{"name": "eth0", "zone": CANARY}]},
            "at interfaces[0].zone: must be one of 'trust', 'untrust', 'dmz'",
        ),
        (
            "Layer 3 Switch",
            {"routes": [{"destination": "0.0.0.0/0", "interface": "eth0"}, {"next_hop": CANARY}]},
            "at routes[1]: needs the fields 'destination', 'interface'",
        ),
        (
            "Layer 3 Switch",
            {"virtual_routers": [{"name": "vr", "interfaces": ["eth0", CANARY * 10]}]},
            "at virtual_routers[0].interfaces[1]: must be at most 64 characters long",
        ),
    ],
)
def test_registry_refusal_never_quotes_a_value(connection_type, config, expected):
    with pytest.raises(ConfigValidationError) as exc:
        validate_device_config(connection_type, config, role="r1")
    message = str(exc.value)
    assert message == f"device 'r1': config failed schema validation {expected}"
    assert CANARY not in message
    assert str(NUMBER_CANARY) not in message
    # The registry path through the published-schema entry point says the same.
    with pytest.raises(ConfigValidationError) as same:
        validate_device_config_with_schema(connection_type, config, schema=None, role="r1")
    assert str(same.value) == message


def test_no_role_keeps_the_bare_device_prefix():
    with pytest.raises(ConfigValidationError) as exc:
        validate_device_config("Management", {"vlan": NUMBER_CANARY})
    assert str(exc.value) == "device: config failed schema validation at vlan: must be at most 4094"


# --- Schema text: shown from the registry, not from a driver's schema -------


def test_published_schema_free_text_is_not_quoted():
    schema = _obj(zone={"enum": ["driver-zone-a", "driver-zone-b"]}, p={"pattern": "^drv-[0-9]+$"})
    for config in ({"zone": CANARY}, {"p": CANARY}):
        message = _refusal(config, schema)
        assert "driver-zone" not in message
        assert "drv-" not in message


def _first_error(schema: dict, instance, **kwargs) -> jsonschema.ValidationError:
    validator = jsonschema.Draft202012Validator(schema, **kwargs)
    return jsonschema.exceptions.best_match(validator.iter_errors(instance))


def test_schema_values_are_shown_when_the_caller_allows_them():
    pattern = _first_error(_obj(p={"pattern": "^[a-z]+$"}), {"p": CANARY})
    const = _first_error(_obj(c={"const": "fixed"}), {"c": CANARY})
    enum = _first_error(_obj(e={"enum": ["a", 1, True, None]}), {"e": CANARY})
    assert schema_error_text(pattern, show_schema_values=True) == (
        "at p: must match the pattern '^[a-z]+$'"
    )
    assert schema_error_text(const, show_schema_values=True) == "at c: must equal 'fixed'"
    assert schema_error_text(enum, show_schema_values=True) == (
        "at e: must be one of 'a', 1, true, null"
    )


def test_a_long_or_structured_schema_value_is_never_quoted():
    many = _first_error(_obj(e={"enum": [f"v{i}" for i in range(20)]}), {"e": CANARY})
    nested = _first_error(_obj(e={"enum": [{"k": "v"}]}), {"e": CANARY})
    long_pattern = _first_error(_obj(p={"pattern": "^" + "a" * 200 + "$"}), {"p": CANARY})
    assert schema_error_text(many, show_schema_values=True).endswith(
        "must be one of the values the schema allows"
    )
    assert schema_error_text(nested, show_schema_values=True).endswith(
        "must be one of the values the schema allows"
    )
    assert schema_error_text(long_pattern, show_schema_values=True).endswith(
        "must match the schema's pattern"
    )


def test_format_rule_names_the_format_only():
    error = _first_error(
        {"type": "string", "format": "ipv4"},
        CANARY,
        format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER,
    )
    assert schema_error_text(error, show_schema_values=False) == (
        "at the top level: must be a valid ipv4"
    )


# --- Nesting, arrays, and depth ------------------------------------------------


def test_nested_combinator_reports_the_deepest_failure_without_its_value():
    schema = _obj(
        x={
            "anyOf": [
                {"type": "object", "properties": {"line": {"type": "string", "maxLength": 3}}},
                {"type": "object", "properties": {"line": {"type": "string", "maxLength": 3}}},
            ]
        }
    )
    message = _refusal({"x": {"line": CANARY}}, schema)
    assert CANARY not in message
    assert "at x" in message


def test_every_error_in_a_combinator_context_renders_without_its_value():
    """best_match picks one error; every error jsonschema found, including the
    nested `context` of a oneOf, renders without the canary."""
    schema = _obj(
        x={
            "oneOf": [
                {"type": "array", "items": {"type": "string", "maxLength": 2}},
                {"type": "object", "required": ["k"]},
                {"type": "string", "pattern": "^[0-9]+$"},
            ]
        }
    )
    errors = list(jsonschema.Draft202012Validator(schema).iter_errors({"x": [CANARY]}))

    def walk(errs):
        for err in errs:
            yield err
            yield from walk(err.context)

    rendered = [schema_error_text(e, show_schema_values=False) for e in walk(errors)]
    assert len(rendered) > 1
    for text in rendered:
        assert CANARY not in text


def test_deep_path_through_arrays_and_objects():
    schema = _obj(
        a={
            "type": "array",
            "items": {
                "type": "array",
                "items": _obj(k={"type": "string", "maxLength": 2}),
            },
        }
    )
    message = _refusal({"a": [[], [{"k": "ok"}, {"k": CANARY}]]}, schema)
    assert message.endswith("at a[1][1].k: must be at most 2 characters long")


def test_a_path_deeper_than_the_cap_is_cut():
    schema: dict = {"type": "string", "maxLength": 1}
    config: object = CANARY
    for _ in range(20):
        schema = {"type": "object", "additionalProperties": schema}
        config = {"k": config}
    message = _refusal(config, schema)
    assert message.endswith(
        "at " + ".".join(["k"] * 16) + "[...]: must be at most 1 character long"
    )


# --- Key names --------------------------------------------------------------


def _key_path(key: str) -> str:
    schema = {"type": "object", "additionalProperties": {"type": "integer"}}
    message = _refusal({key: "not-an-integer"}, schema)
    return message.split(" at ", 1)[1].rsplit(": must be of type integer", 1)[0]


def test_plain_key_is_written_bare_and_other_keys_are_quoted():
    assert _key_path("hostname") == "hostname"
    assert _key_path("ge-0/0/1") == "['ge-0/0/1']"


def test_a_key_that_reads_like_a_credential_line_is_masked():
    path = _key_path(f"neighbor 192.0.2.1 password {CANARY}")
    assert CANARY not in path
    assert path == "['neighbor 192.0.2.1 password [redacted]']"


def test_a_long_key_is_cut():
    path = _key_path("k" * 300)
    assert path == "['" + "k" * 64 + "...']"


def test_a_key_with_a_control_character_or_lone_surrogate_is_escaped():
    path = _key_path("bad\nkey\ud800")
    assert "\n" not in path
    assert "\ud800" not in path
    # The refusal is a 422 detail: it must encode as UTF-8.
    path.encode("utf-8")


def test_a_value_with_a_lone_surrogate_leaves_an_encodable_message():
    message = _refusal({"line": "x\ud800" + CANARY}, _obj(line={"maxLength": 1}))
    json.dumps({"detail": message}, ensure_ascii=False).encode("utf-8")
    assert CANARY not in message


# --- No jsonschema error rides along ------------------------------------------


@pytest.mark.parametrize("schema", [None, FRR_SCHEMA])
def test_refusal_carries_no_jsonschema_error(schema):
    """The jsonschema error quotes the config; a traceback a logger prints shows
    the cause and the context, so the refusal has neither."""
    config = {"vlan": NUMBER_CANARY} if schema is None else {"command": [CANARY]}
    with pytest.raises(ConfigValidationError) as exc:
        validate_device_config_with_schema("Management", config, schema=schema)
    assert exc.value.__cause__ is None
    assert exc.value.__context__ is None
