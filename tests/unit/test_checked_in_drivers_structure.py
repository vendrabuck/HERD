"""Every checked-in driver under drivers/ passes the one structural check (issue #1114).

The execution service validates a driver package by parsing driver.py, never by
importing it (`driver_structure_errors` in
services/execution/app/services/driver_structure.py, shared by the load path and the
package validator). That check requires a plain top-level `class Driver` statement and
the methods `REQUIRED_METHODS` lists for the package's connection type. This test runs
it over every package under drivers/ with the connection type the package's own
driver_metadata.json declares, so a refactor that moves a method out of sight of the
parser fails here, stack-free, instead of at upload time.

The module is loaded by path, not imported as `app...`: every service names its
package `app`, and driver_structure.py is pure standard library for exactly this use.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DRIVERS_DIR = REPO_ROOT / "drivers"
STRUCTURE_PATH = REPO_ROOT / "services/execution/app/services/driver_structure.py"

# The packages the repository ships today; a new one is picked up automatically, and
# this floor only guards against the discovery silently finding nothing.
KNOWN_DRIVERS = {
    "frr_l3",
    "frr_mgmt",
    "mock_hypervisor",
    "mock_l1",
    "mock_l2",
    "mock_l3",
    "srl_l2",
}


def _load_structure_module():
    name = "herd_execution_driver_structure_under_test"
    spec = importlib.util.spec_from_file_location(name, STRUCTURE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


STRUCTURE = _load_structure_module()

DRIVER_DIRS = sorted(p.parent for p in DRIVERS_DIR.glob("*/driver.py"))


def test_every_known_driver_is_discovered():
    assert KNOWN_DRIVERS <= {d.name for d in DRIVER_DIRS}


@pytest.mark.parametrize("driver_dir", DRIVER_DIRS, ids=lambda d: d.name)
def test_checked_in_driver_passes_the_shared_structural_check(driver_dir):
    metadata = json.loads((driver_dir / "driver_metadata.json").read_text(encoding="utf-8"))
    connection_type = metadata["connection_type"]
    assert connection_type in STRUCTURE.REQUIRED_METHODS
    assert STRUCTURE.driver_structure_errors(driver_dir, connection_type) == []


def test_the_check_is_not_vacuous():
    """The same check refuses a package missing a required method."""
    mock_l1 = DRIVERS_DIR / "mock_l1"
    errors = STRUCTURE.driver_structure_errors(mock_l1, "Layer 2 Switch")
    assert "Driver class is missing required method: create_vlan" in errors
