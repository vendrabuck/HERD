"""Unit tests for services.config_validator."""

import pytest
from app.services.config_validator import (
    ALLOWED_CONFIG_KEYS,
    ConfigValidationError,
    validate_device_config,
)


def test_none_config_passes_regardless_of_connection_type():
    validate_device_config(None, None)
    validate_device_config("Layer 2 Switch", None)
    validate_device_config("Management", None)


def test_empty_config_dict_passes():
    validate_device_config(None, {})
    validate_device_config("Layer 2 Switch", {})


def test_config_without_connection_type_rejected():
    with pytest.raises(ConfigValidationError, match="connection_type is missing"):
        validate_device_config(None, {"vlan": 10})


def test_config_on_unregistered_connection_type_rejected():
    with pytest.raises(ConfigValidationError, match="does not accept a configure action"):
        validate_device_config("Layer 1 Switch", {"vlan": 10})


def test_valid_management_config_passes():
    validate_device_config(
        "Management",
        {"vlan": 100, "ip": "10.0.0.1/24", "hostname": "fw-a", "description": "primary"},
    )


def test_unknown_key_rejected():
    with pytest.raises(ConfigValidationError, match="admin_password"):
        validate_device_config("Management", {"admin_password": "x"})


def test_vlan_out_of_range_rejected():
    with pytest.raises(ConfigValidationError):
        validate_device_config("Management", {"vlan": 5000})
    with pytest.raises(ConfigValidationError):
        validate_device_config("Management", {"vlan": 0})


def test_wrong_type_rejected():
    with pytest.raises(ConfigValidationError):
        validate_device_config("Management", {"vlan": "ten"})


def test_role_prefix_in_error_message():
    with pytest.raises(ConfigValidationError, match=r"device 'fw-a':"):
        validate_device_config("Management", {"bogus": 1}, role="fw-a")


# Generic Layer 3 routing config (registered under "Layer 3 Switch").

_VALID_L3_CONFIG = {
    "interfaces": [
        {"name": "eth1/3", "ip": "10.0.0.1/24", "zone": "trust"},
        {"name": "eth1/6", "ip": "203.0.113.2/30", "zone": "untrust"},
    ],
    "virtual_routers": [
        {"name": "default", "interfaces": ["eth1/3", "eth1/6"]},
    ],
    "routes": [
        {"destination": "0.0.0.0/0", "next_hop": "203.0.113.1", "interface": "eth1/6"},
    ],
    "description": "trust-to-untrust routed path",
}


def test_valid_l3_router_config_passes():
    validate_device_config("Layer 3 Switch", _VALID_L3_CONFIG)


def test_l3_config_minimal_interface_passes():
    # ip is optional; name + zone are the required interface fields.
    validate_device_config(
        "Layer 3 Switch",
        {"interfaces": [{"name": "eth1/3", "zone": "trust"}]},
    )


def test_l3_misconfigured_router_still_validates_structurally():
    """The demo's 'broken' state (untrust interface absent from the virtual
    router and no default route) is a valid SHAPE: schema validation passes,
    and the misconfiguration is a semantic gap the assistant reasons about,
    not a schema violation."""
    broken = {
        "interfaces": [
            {"name": "eth1/3", "ip": "10.0.0.1/24", "zone": "trust"},
            {"name": "eth1/6", "ip": "203.0.113.2/30", "zone": "untrust"},
        ],
        # eth1/6 deliberately not a member; no default route.
        "virtual_routers": [{"name": "default", "interfaces": ["eth1/3"]}],
        "routes": [],
    }
    validate_device_config("Layer 3 Switch", broken)


def test_l3_unknown_top_level_key_rejected():
    with pytest.raises(ConfigValidationError, match="firewall_rules"):
        validate_device_config("Layer 3 Switch", {"firewall_rules": []})


def test_l3_bad_zone_enum_rejected():
    with pytest.raises(ConfigValidationError):
        validate_device_config(
            "Layer 3 Switch",
            {"interfaces": [{"name": "eth1/3", "zone": "internal"}]},
        )


def test_l3_interface_missing_required_field_rejected():
    with pytest.raises(ConfigValidationError):
        validate_device_config(
            "Layer 3 Switch",
            {"interfaces": [{"name": "eth1/3"}]},  # zone is required
        )


def test_l3_interface_unknown_key_rejected():
    with pytest.raises(ConfigValidationError):
        validate_device_config(
            "Layer 3 Switch",
            {"interfaces": [{"name": "eth1/3", "zone": "trust", "speed": "10G"}]},
        )


def test_allowed_keys_are_derived_from_registry():
    """The AI tool schema and system prompt depend on ALLOWED_CONFIG_KEYS
    staying in sync with CONFIG_SCHEMAS."""
    assert "vlan" in ALLOWED_CONFIG_KEYS
    assert "ip" in ALLOWED_CONFIG_KEYS
    assert "hostname" in ALLOWED_CONFIG_KEYS
    assert "description" in ALLOWED_CONFIG_KEYS
    # L3 router schema top-level keys are also derived into the set. See
    # ROADMAP #21: these should later be decoupled from the generate-path
    # prompt allowlist, which only intends Management/L2 keys.
    assert "interfaces" in ALLOWED_CONFIG_KEYS
    assert "virtual_routers" in ALLOWED_CONFIG_KEYS
    assert "routes" in ALLOWED_CONFIG_KEYS
    # Deterministic ordering so the prompt is reproducible.
    assert ALLOWED_CONFIG_KEYS == sorted(ALLOWED_CONFIG_KEYS)
