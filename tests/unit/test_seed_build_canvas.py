"""Unit tests for the demo-seed canvas builder (seed_devices_public.build_canvas).

These are pure, stack-free tests: they import the seed module and exercise
build_canvas directly. They guard the fix for issue #108, where seeded canvas
nodes lacked type "deviceNode" (so React Flow rendered its blank default node)
and carried only a thin {"device": {"id": ...}} reference (so the custom
DeviceNode had no name/topology_type to render).
"""

import importlib.util
import sys
from pathlib import Path

import pytest

# The seed script lives at the repo root, not inside a package, so load it by
# path rather than relying on it being importable as a top-level module.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SEED_PATH = _REPO_ROOT / "seed_devices_public.py"


@pytest.fixture(scope="module")
def seed():
    spec = importlib.util.spec_from_file_location("seed_devices_public", _SEED_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["seed_devices_public"] = module
    spec.loader.exec_module(module)
    return module


def _device(did: str, **overrides) -> dict:
    base = {
        "id": did,
        "name": f"switch-{did}",
        "topology_type": "PHYSICAL",
        "template_name": "L1 Switch",
        "template_icon": None,
        "status": "AVAILABLE",
        # Extra inventory fields that must NOT bleed into the canvas payload.
        "field_data": {"ip": "10.0.0.1"},
        "driver_id": None,
    }
    base.update(overrides)
    return base


def test_every_node_has_device_node_type(seed):
    lookup = {"a": _device("a"), "b": _device("b")}
    canvas = seed.build_canvas(["a", "b"], [(0, 1, "L2")], lookup)

    assert [n["id"] for n in canvas["nodes"]] == ["n0", "n1"]
    for node in canvas["nodes"]:
        assert node["type"] == "deviceNode"


def test_populated_node_carries_full_device_payload(seed):
    lookup = {"a": _device("a", name="L1-Edge-01", topology_type="PHYSICAL")}
    canvas = seed.build_canvas(["a"], [], lookup)

    data = canvas["nodes"][0]["data"]
    device = data["device"]
    assert device["id"] == "a"
    assert device["name"] == "L1-Edge-01"
    assert device["topology_type"] == "PHYSICAL"
    assert device["template_name"] == "L1 Switch"
    assert device["status"] == "AVAILABLE"
    # The label/topologyType mirror the live drop path exactly.
    assert data["label"] == "L1-Edge-01"
    assert data["topologyType"] == "PHYSICAL"
    # Only the canvas shape fields are embedded, not the whole inventory record.
    assert "field_data" not in device
    assert "driver_id" not in device


def test_none_slot_emits_typed_empty_node(seed):
    # A None device id is an intentional missing-device slot: empty data, but the
    # node must still be typed so it renders via DeviceNode (gray/missing), not as
    # a blank React Flow default node.
    canvas = seed.build_canvas([None], [], {})
    node = canvas["nodes"][0]
    assert node["type"] == "deviceNode"
    assert node["data"] == {}


def test_unknown_device_id_falls_back_to_thin_reference(seed):
    # Id not in the lookup (e.g. a device that vanished between fetch and build):
    # keep a thin reference so load-time hydration can still fill it in by id.
    canvas = seed.build_canvas(["ghost"], [], {"other": _device("other")})
    node = canvas["nodes"][0]
    assert node["type"] == "deviceNode"
    assert node["data"] == {"device": {"id": "ghost"}}


def test_missing_lookup_defaults_keep_node_renderable(seed):
    # A device record missing name/topology_type/status must not produce nulls
    # where DeviceNode expects strings (empty name span, color fallback, badge).
    lookup = {"a": {"id": "a"}}
    canvas = seed.build_canvas(["a"], [], lookup)
    device = canvas["nodes"][0]["data"]["device"]
    assert device["name"] == ""
    assert device["topology_type"] == "PHYSICAL"
    assert device["status"] == "AVAILABLE"


def test_omitting_lookup_preserves_thin_reference(seed):
    # Backward-compatible call without a lookup must not throw and must keep the
    # node hydratable (thin device id reference), still typed.
    canvas = seed.build_canvas(["a", None], [(0, 1, "L1")])
    nodes = canvas["nodes"]
    assert nodes[0]["type"] == "deviceNode"
    assert nodes[0]["data"] == {"device": {"id": "a"}}
    assert nodes[1]["data"] == {}


def test_edges_unchanged_by_node_payload(seed):
    canvas = seed.build_canvas(["a", "b"], [(0, 1, "L3")], {"a": _device("a"), "b": _device("b")})
    assert canvas["edges"] == [
        {
            "id": "e0",
            "source": "n0",
            "target": "n1",
            "data": {"layer": "L3", "isProposal": False},
        }
    ]
