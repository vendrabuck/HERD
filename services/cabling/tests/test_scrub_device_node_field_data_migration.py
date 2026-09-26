"""Unit tests for migration 0013's frozen scrub logic (hardening).

Migrations must not import live application code (see the migration's own
comment and the precedent at services/execution/migrations/versions/
0019_backfill_l2_port_assignments.py), so 0013 carries a frozen copy of
DEVICE_NODE_ALLOWED_KEYS and strip_device_nodes. This test loads the migration
file directly (it has no DB side effects at import time: upgrade/downgrade are
just functions) and asserts the frozen copy matches the live module today, plus
exercises the scrub function's own idempotency over fixture row payloads.
"""

import copy
import importlib.util
from pathlib import Path

from app.services.canvas_nodes import DEVICE_NODE_ALLOWED_KEYS, strip_device_nodes

_MIGRATION_PATH = (
    Path(__file__).resolve().parent.parent
    / "migrations"
    / "versions"
    / "0013_scrub_device_node_field_data.py"
)

_spec = importlib.util.spec_from_file_location(
    "scrub_device_node_field_data_migration", _MIGRATION_PATH
)
_migration = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_migration)


def test_frozen_allowlist_matches_live_module():
    assert _migration.DEVICE_NODE_ALLOWED_KEYS == DEVICE_NODE_ALLOWED_KEYS


def test_frozen_strip_function_matches_live_behavior_on_fixtures():
    fixtures = [
        None,
        {},
        {"nodes": [], "edges": []},
        {
            "nodes": [
                {
                    "id": "n1",
                    "type": "deviceNode",
                    "data": {
                        "device": {
                            "id": "1",
                            "name": "sw-1",
                            "field_data": {"password": "x"},
                        }
                    },
                }
            ],
            "edges": [],
        },
        {
            "nodes": [
                {"id": "n1", "type": "deviceNode", "data": {"device": {"id": "1", "name": "a"}}}
            ],
            "edges": [],
        },
    ]
    for fixture in fixtures:
        expected = strip_device_nodes(copy.deepcopy(fixture) if fixture is not None else None)
        actual = _migration.strip_device_nodes(
            copy.deepcopy(fixture) if fixture is not None else None
        )
        assert actual == expected


def test_scrub_fixture_rows_idempotent():
    """Mirrors _scrub_table's per-row logic: a fixture set of row payloads
    (no device nodes, already clean, nested field_data) scrubs identically on
    a second pass."""
    rows = [
        {"nodes": [], "edges": []},  # no device nodes
        {
            "nodes": [
                {"id": "n1", "type": "deviceNode", "data": {"device": {"id": "1", "name": "a"}}}
            ],
            "edges": [],
        },  # already clean
        {
            "nodes": [
                {
                    "id": "n1",
                    "type": "deviceNode",
                    "data": {
                        "device": {
                            "id": "1",
                            "name": "a",
                            "field_data": {"nested": {"password": "x"}},
                        }
                    },
                }
            ],
            "edges": [],
        },  # nested field_data
    ]

    once = [_migration.strip_device_nodes(copy.deepcopy(row)) for row in rows]
    twice = [_migration.strip_device_nodes(copy.deepcopy(r)) for r in once]
    assert once == twice

    # The already-clean row is untouched; the dirty one lost field_data.
    assert once[1] == rows[1]
    assert "field_data" not in once[2]["nodes"][0]["data"]["device"]


def test_none_canvas_row_is_skipped_like_scrub_table_would():
    assert _migration.strip_device_nodes(None) is None
