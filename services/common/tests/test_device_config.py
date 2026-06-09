"""Tests for herd_common.device_config.

device_config validates a single device's proposed `configure`-action payload
against an allowlist of jsonschema schemas (the CONFIG_SCHEMAS registry) keyed
by connection_type. It is HERD's single source of truth for "safe driver
inputs" and is shared by the inventory service (config-version writes) and the
AI orchestrator (which re-exports these symbols).

Coverage targets the real semantics: the no-op fast path, the two rejection
branches (missing / unknown connection_type), successful schema validation for
each registered connection_type, schema-failure branches per registry entry,
and the role-prefixed, chained ConfigValidationError.
"""

import pytest
from herd_common.device_config import (
    ALLOWED_CONFIG_KEYS,
    CONFIG_SCHEMAS,
    ConfigValidationError,
    PublishedSchemaError,
    validate_device_config,
    validate_device_config_with_schema,
)

# --- Module-level registry invariants ----------------------------------------


def test_config_validation_error_is_value_error():
    """Callers (inventory) catch this to surface a 422; it must subclass
    ValueError so existing `except ValueError` blocks keep working."""
    assert issubclass(ConfigValidationError, ValueError)


def test_allowed_config_keys_is_sorted_union_of_schema_properties():
    expected = sorted({key for schema in CONFIG_SCHEMAS.values() for key in schema["properties"]})
    assert ALLOWED_CONFIG_KEYS == expected
    # sorted() guarantees ascending order with no duplicates.
    assert ALLOWED_CONFIG_KEYS == sorted(set(ALLOWED_CONFIG_KEYS))


def test_registry_covers_the_three_known_connection_types():
    assert set(CONFIG_SCHEMAS) == {"Management", "Layer 2 Switch", "Layer 3 Switch"}


def test_every_schema_rejects_additional_properties():
    for schema in CONFIG_SCHEMAS.values():
        assert schema["additionalProperties"] is False


# --- No-op fast path ----------------------------------------------------------


@pytest.mark.parametrize("empty", [None, {}])
def test_empty_config_is_always_a_noop(empty):
    """No config means no-op and is allowed regardless of connection_type,
    even an unknown or missing one."""
    assert validate_device_config("Management", empty) is None
    assert validate_device_config(None, empty) is None
    assert validate_device_config("Nonexistent", empty) is None


# --- Rejection branches -------------------------------------------------------


def test_missing_connection_type_with_config_is_rejected():
    with pytest.raises(ConfigValidationError) as exc:
        validate_device_config(None, {"vlan": 10})
    assert "connection_type is missing" in str(exc.value)


def test_unknown_connection_type_with_config_is_rejected():
    with pytest.raises(ConfigValidationError) as exc:
        validate_device_config("Firewall 9000", {"vlan": 10})
    msg = str(exc.value)
    assert "does not accept" in msg
    assert "Firewall 9000" in msg


# --- Role prefix in error messages --------------------------------------------


def test_role_prefixes_error_message_when_provided():
    with pytest.raises(ConfigValidationError) as exc:
        validate_device_config(None, {"vlan": 10}, role="spine-01")
    assert "device 'spine-01'" in str(exc.value)


def test_no_role_uses_bare_device_prefix():
    with pytest.raises(ConfigValidationError) as exc:
        validate_device_config(None, {"vlan": 10})
    assert str(exc.value).startswith("device:")


# --- Management schema --------------------------------------------------------


def test_management_valid_config_passes():
    config = {
        "vlan": 100,
        "ip": "10.0.0.1",
        "hostname": "mgmt-sw",
        "description": "lab management",
    }
    assert validate_device_config("Management", config) is None


@pytest.mark.parametrize("vlan", [1, 4094])
def test_management_vlan_boundaries_pass(vlan):
    assert validate_device_config("Management", {"vlan": vlan}) is None


@pytest.mark.parametrize("vlan", [0, 4095])
def test_management_vlan_out_of_range_fails(vlan):
    with pytest.raises(ConfigValidationError) as exc:
        validate_device_config("Management", {"vlan": vlan})
    assert "schema validation" in str(exc.value)


def test_management_unknown_property_fails():
    with pytest.raises(ConfigValidationError):
        validate_device_config("Management", {"bogus": "x"})


def test_management_wrong_type_fails():
    with pytest.raises(ConfigValidationError):
        validate_device_config("Management", {"vlan": "not-an-int"})


# --- Layer 2 Switch schema ----------------------------------------------------


def test_layer2_valid_vlan_assignments_pass():
    config = {
        "vlan_assignments": {"ge-0/0/1": 100, "ge-0/0/2": 4094},
        "description": "edge",
    }
    assert validate_device_config("Layer 2 Switch", config) is None


def test_layer2_empty_vlan_assignments_pass():
    assert validate_device_config("Layer 2 Switch", {"vlan_assignments": {}}) is None


def test_layer2_vlan_assignment_out_of_range_fails():
    with pytest.raises(ConfigValidationError):
        validate_device_config("Layer 2 Switch", {"vlan_assignments": {"p1": 9999}})


def test_layer2_vlan_assignment_non_integer_fails():
    with pytest.raises(ConfigValidationError):
        validate_device_config("Layer 2 Switch", {"vlan_assignments": {"p1": "vlan"}})


# --- Layer 3 Switch schema ----------------------------------------------------


def test_layer3_full_valid_config_passes():
    config = {
        "interfaces": [
            {"name": "eth0", "ip": "10.0.0.1", "zone": "trust"},
            {"name": "eth1", "zone": "untrust"},
        ],
        "virtual_routers": [
            {"name": "vr-default", "interfaces": ["eth0", "eth1"]},
        ],
        "routes": [
            {"destination": "0.0.0.0/0", "next_hop": "10.0.0.254", "interface": "eth1"},
        ],
        "description": "routed core",
    }
    assert validate_device_config("Layer 3 Switch", config) is None


def test_layer3_interface_requires_name_and_zone():
    with pytest.raises(ConfigValidationError):
        validate_device_config("Layer 3 Switch", {"interfaces": [{"ip": "10.0.0.1"}]})


def test_layer3_interface_zone_must_be_in_enum():
    with pytest.raises(ConfigValidationError) as exc:
        validate_device_config("Layer 3 Switch", {"interfaces": [{"name": "eth0", "zone": "wan"}]})
    assert "schema validation" in str(exc.value)


def test_layer3_virtual_router_requires_name_and_interfaces():
    with pytest.raises(ConfigValidationError):
        validate_device_config("Layer 3 Switch", {"virtual_routers": [{"name": "vr"}]})


def test_layer3_route_requires_destination_and_interface():
    # next_hop is optional, but destination and interface are required.
    with pytest.raises(ConfigValidationError):
        validate_device_config("Layer 3 Switch", {"routes": [{"next_hop": "10.0.0.1"}]})


def test_layer3_route_without_next_hop_passes():
    config = {"routes": [{"destination": "192.168.0.0/24", "interface": "eth0"}]}
    assert validate_device_config("Layer 3 Switch", config) is None


# --- Error chaining -----------------------------------------------------------


def test_schema_failure_chains_original_validation_error():
    """The raised ConfigValidationError preserves the jsonschema cause so
    debuggers/loggers can see the underlying failure."""
    import jsonschema

    with pytest.raises(ConfigValidationError) as exc:
        validate_device_config("Management", {"vlan": 99999})
    assert isinstance(exc.value.__cause__, jsonschema.ValidationError)


# --- validate_device_config_with_schema: golden backward-compat (schema=None) -
#
# With schema=None the new function must be byte-for-byte the old behavior. We
# assert identical outcomes AND identical 422 wording across every branch the
# old validator has, so the registry path is provably unchanged.

_PUBLISHED = {
    "type": "object",
    "properties": {"hostname": {"type": "string", "maxLength": 8}},
    "additionalProperties": False,
}


@pytest.mark.parametrize("empty", [None, {}])
def test_with_schema_none_empty_is_noop(empty):
    assert validate_device_config_with_schema("Management", empty, schema=None) is None


def test_with_schema_none_matches_registry_accept():
    config = {"vlan": 100, "hostname": "mgmt-sw"}
    assert validate_device_config_with_schema("Management", config, schema=None) is None


def test_with_schema_none_missing_connection_type_same_wording():
    with pytest.raises(ConfigValidationError) as new_exc:
        validate_device_config_with_schema(None, {"vlan": 10}, schema=None)
    with pytest.raises(ConfigValidationError) as old_exc:
        validate_device_config(None, {"vlan": 10})
    assert str(new_exc.value) == str(old_exc.value)


def test_with_schema_none_unknown_connection_type_same_wording():
    with pytest.raises(ConfigValidationError) as new_exc:
        validate_device_config_with_schema("Firewall 9000", {"vlan": 10}, schema=None)
    with pytest.raises(ConfigValidationError) as old_exc:
        validate_device_config("Firewall 9000", {"vlan": 10})
    assert str(new_exc.value) == str(old_exc.value)


def test_with_schema_none_schema_failure_same_wording():
    with pytest.raises(ConfigValidationError) as new_exc:
        validate_device_config_with_schema("Management", {"vlan": 99999}, schema=None, role="sw1")
    with pytest.raises(ConfigValidationError) as old_exc:
        validate_device_config("Management", {"vlan": 99999}, role="sw1")
    assert str(new_exc.value) == str(old_exc.value)


# --- validate_device_config_with_schema: published-schema path ----------------


def test_with_published_schema_accepts():
    assert (
        validate_device_config_with_schema("Management", {"hostname": "short"}, schema=_PUBLISHED)
        is None
    )


@pytest.mark.parametrize("empty", [None, {}])
def test_with_published_schema_empty_is_noop(empty):
    assert validate_device_config_with_schema("Management", empty, schema=_PUBLISHED) is None


def test_with_published_schema_rejects_with_role_prefix():
    with pytest.raises(ConfigValidationError) as exc:
        validate_device_config_with_schema(
            "Management", {"hostname": "way-too-long"}, schema=_PUBLISHED, role="edge-1"
        )
    msg = str(exc.value)
    assert msg.startswith("device 'edge-1'")
    assert "schema validation" in msg


def test_with_published_schema_overrides_registry():
    """The published schema forbids `ip`, which the Management registry allows.
    A config the registry would accept must be REJECTED by the published schema,
    proving the published schema wins."""
    # Registry accepts ip; the published schema (additionalProperties: False,
    # only hostname) rejects it.
    assert validate_device_config("Management", {"ip": "10.0.0.1"}) is None
    with pytest.raises(ConfigValidationError):
        validate_device_config_with_schema("Management", {"ip": "10.0.0.1"}, schema=_PUBLISHED)


# --- validate_device_config_with_schema: SSRF / malformed-schema guard --------


def test_published_schema_with_remote_ref_is_rejected():
    hostile = {"type": "object", "properties": {"x": {"$ref": "http://169.254.169.254/"}}}
    with pytest.raises(PublishedSchemaError) as exc:
        validate_device_config_with_schema("Management", {"x": 1}, schema=hostile)
    assert "non-local $ref" in str(exc.value)


def test_published_schema_local_ref_is_allowed():
    schema = {
        "type": "object",
        "$defs": {"name": {"type": "string", "maxLength": 4}},
        "properties": {"hostname": {"$ref": "#/$defs/name"}},
        "additionalProperties": False,
    }
    assert (
        validate_device_config_with_schema("Management", {"hostname": "ok"}, schema=schema) is None
    )
    with pytest.raises(ConfigValidationError):
        validate_device_config_with_schema("Management", {"hostname": "toolong"}, schema=schema)


def test_published_schema_id_is_stripped():
    """A non-local $id must not establish a remote base URI; it is stripped and
    validation still works against the rest of the schema."""
    schema = {
        "$id": "http://evil.example/schema",
        "type": "object",
        "properties": {"hostname": {"type": "string", "maxLength": 4}},
        "additionalProperties": False,
    }
    assert (
        validate_device_config_with_schema("Management", {"hostname": "ok"}, schema=schema) is None
    )
    with pytest.raises(ConfigValidationError):
        validate_device_config_with_schema("Management", {"hostname": "toolong"}, schema=schema)


def test_published_schema_invalid_schema_is_rejected():
    not_a_schema = {"type": "not-a-real-type"}
    with pytest.raises(PublishedSchemaError):
        validate_device_config_with_schema("Management", {"x": 1}, schema=not_a_schema)
