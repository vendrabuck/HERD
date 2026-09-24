"""Static pin: inventory's hardcoded poll-interval floor equals execution's
default scheduler tick (issue #880).

No stack, no Docker, and deliberately no import of either service's package:
inventory and execution both name their package `app`, so importing both in
one process would have the second import shadow the first (or collide,
depending on import order and sys.modules caching). Instead this reads each
constant statically out of its source file with the `ast` module, the same
static-introspection posture as tests/unit/test_compose_settings_wiring.py
and tests/unit/test_pg_live_gate_wiring.py (those parse docker-compose.yml
and the Makefile as text/YAML rather than importing anything either).

Why the two constants must match: inventory's `MIN_POLL_INTERVAL_SECONDS`
(services/inventory/app/schemas/device.py) rejects any `poll_interval_seconds`
below it with a 422 at write time. Execution's scheduler
(services/execution/app/services/health_scheduler.py) only claims due rows
once per tick, `health_poll_scheduler_tick_seconds`
(services/execution/app/config.py), so a device's effective poll period is
never shorter than one tick no matter what interval is configured. If
inventory's floor were lower than execution's default tick, the API would
accept a cadence the scheduler cannot honor at that default; if it were
higher, the API would refuse a cadence the scheduler could actually meet.
Either mismatch is a real defect, not a style nit, so this test fails loudly
naming both files and both values rather than passing silently on a drift.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

INVENTORY_DEVICE_SCHEMA_PATH = REPO_ROOT / "services/inventory/app/schemas/device.py"
INVENTORY_CONSTANT_NAME = "MIN_POLL_INTERVAL_SECONDS"

EXECUTION_CONFIG_PATH = REPO_ROOT / "services/execution/app/config.py"
EXECUTION_SETTINGS_CLASS = "Settings"
EXECUTION_FIELD_NAME = "health_poll_scheduler_tick_seconds"


def _find_module_level_int_constant(path: Path, name: str) -> int | None:
    """Return the value of a module-level `<name> = <int literal>` assignment
    in the file at `path`, or None if no such assignment exists.
    """
    tree = ast.parse(path.read_text(), filename=str(path))
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            continue
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, int):
            return node.value.value
    return None


def _find_settings_field_int_default(path: Path, class_name: str, field_name: str) -> int | None:
    """Return the default value of `<field_name>: <ann> = <int literal>`
    inside `class <class_name>(...): ...` in the file at `path`, or None if
    the class or the field (with an int-literal default) is not found.
    """
    tree = ast.parse(path.read_text(), filename=str(path))
    for node in ast.walk(tree):
        if not (isinstance(node, ast.ClassDef) and node.name == class_name):
            continue
        for stmt in node.body:
            if not isinstance(stmt, ast.AnnAssign):
                continue
            if not (isinstance(stmt.target, ast.Name) and stmt.target.id == field_name):
                continue
            if stmt.value is not None and isinstance(stmt.value, ast.Constant):
                if isinstance(stmt.value.value, int):
                    return stmt.value.value
    return None


def test_inventory_floor_matches_execution_default_tick():
    inventory_value = _find_module_level_int_constant(
        INVENTORY_DEVICE_SCHEMA_PATH, INVENTORY_CONSTANT_NAME
    )
    execution_value = _find_settings_field_int_default(
        EXECUTION_CONFIG_PATH, EXECUTION_SETTINGS_CLASS, EXECUTION_FIELD_NAME
    )

    assert inventory_value is not None, (
        f"could not find a module-level int literal `{INVENTORY_CONSTANT_NAME} = <int>` "
        f"in {INVENTORY_DEVICE_SCHEMA_PATH}; it may have been renamed, removed, or its "
        "value is no longer a plain int literal, any of which this parity test must "
        "catch rather than silently pass."
    )
    assert execution_value is not None, (
        f"could not find `{EXECUTION_FIELD_NAME}: <ann> = <int>` on class "
        f"`{EXECUTION_SETTINGS_CLASS}` in {EXECUTION_CONFIG_PATH}; it may have been "
        "renamed, removed, or its default is no longer a plain int literal, any of "
        "which this parity test must catch rather than silently pass."
    )
    assert inventory_value == execution_value, (
        f"{INVENTORY_DEVICE_SCHEMA_PATH} defines {INVENTORY_CONSTANT_NAME} = "
        f"{inventory_value}, but {EXECUTION_CONFIG_PATH} defines "
        f"Settings.{EXECUTION_FIELD_NAME} = {execution_value} as its default. These "
        "two must stay equal: inventory's floor must never accept a cadence the "
        "execution scheduler cannot honor at its default tick, and must never refuse "
        "a cadence the scheduler can meet."
    )


def test_parser_actually_finds_both_values():
    """Guard against the parity test above passing for the wrong reason: if
    either helper silently returned None for both files, the equality
    assertion above (None == None) would pass without proving anything. This
    pins that both lookups really extract a real int, independent of what
    that int currently is.
    """
    inventory_value = _find_module_level_int_constant(
        INVENTORY_DEVICE_SCHEMA_PATH, INVENTORY_CONSTANT_NAME
    )
    execution_value = _find_settings_field_int_default(
        EXECUTION_CONFIG_PATH, EXECUTION_SETTINGS_CLASS, EXECUTION_FIELD_NAME
    )

    assert isinstance(inventory_value, int), (
        f"expected an int for {INVENTORY_CONSTANT_NAME} in "
        f"{INVENTORY_DEVICE_SCHEMA_PATH}, got {inventory_value!r}"
    )
    assert isinstance(execution_value, int), (
        f"expected an int for Settings.{EXECUTION_FIELD_NAME} in "
        f"{EXECUTION_CONFIG_PATH}, got {execution_value!r}"
    )
