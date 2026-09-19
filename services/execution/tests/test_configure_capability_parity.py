"""Parity pin (issue #839): herd_common.device_config.CONFIGURE_CONNECTION_TYPES
must never drift from execution's own driver_loader.REQUIRED_METHODS.

Capability comes from the driver CONTRACT, not a declared flag: REQUIRED_METHODS
is enforced at driver load (validate_driver), so it is the one place that
actually says which connection types must implement `configure`. The shared
constant in herd_common exists so inventory can gate an apply without importing
execution's driver_loader module; this test is what keeps the two from ever
disagreeing silently.
"""

from app.services.driver_loader import REQUIRED_METHODS
from herd_common.device_config import CONFIGURE_CONNECTION_TYPES


def test_configure_connection_types_matches_required_methods_contract():
    derived = {
        connection_type
        for connection_type, methods in REQUIRED_METHODS.items()
        if "configure" in methods
    }
    assert CONFIGURE_CONNECTION_TYPES == derived


def test_configure_connection_types_matches_required_methods_is_management_only():
    """Spelled out explicitly too, so a change to either side that keeps them
    equal-but-wrong (e.g. both accidentally empty) still fails loudly."""
    assert CONFIGURE_CONNECTION_TYPES == {"Management"}
    assert "configure" in REQUIRED_METHODS["Management"]
    for connection_type, methods in REQUIRED_METHODS.items():
        if connection_type == "Management":
            continue
        assert "configure" not in methods
