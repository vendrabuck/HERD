"""Unit tests for app.services.canvas_nodes.strip_device_nodes (hardening).

Pure-function tests: no DB, no HTTP. See test_bulk.py / test_forks.py /
test_topologies.py for the HTTP-level proof that every write boundary applies
this before a row is stored.
"""

import copy

from app.services.canvas_nodes import DEVICE_NODE_ALLOWED_KEYS, strip_device_nodes


def _canvas_with_everything():
    """One device node carrying field_data (with a password key) and other
    non-allowlisted fields, one network element node, one dynamic-placeholder
    shape, one edge, and one malformed node."""
    return {
        "nodes": [
            {
                "id": "n1",
                "type": "deviceNode",
                "position": {"x": 0, "y": 0},
                "data": {
                    "device": {
                        "id": "11111111-1111-1111-1111-111111111111",
                        "name": "sw-1",
                        "topology_type": "PHYSICAL",
                        "connection_type": "Layer 3 Switch",
                        "status": "AVAILABLE",
                        "template_name": "Generic Switch",
                        "template_icon": "icon.svg",
                        "template_id": "22222222-2222-2222-2222-222222222222",
                        "field_data": {"password": "super-secret", "host": "10.0.0.1"},
                        "driver_sha256": "abc123",
                        "exclusive": True,
                    },
                    "label": "sw-1",
                    "topologyType": "PHYSICAL",
                },
            },
            {
                "id": "n2",
                "type": "networkElementNode",
                "position": {"x": 100, "y": 0},
                "data": {
                    "element": {
                        "id": "33333333-3333-3333-3333-333333333333",
                        "element_type": "vlan_segment",
                        "label": "vlan-100",
                        "attrs": {},
                    }
                },
            },
            {
                "id": "n3",
                "type": "dynamicPlaceholderNode",
                "position": {"x": 200, "y": 0},
                "data": {
                    "templateId": "44444444-4444-4444-4444-444444444444",
                    "templateName": "Dynamic VM",
                    "templateIcon": None,
                    "count": 2,
                },
            },
            {
                "id": "n4",
                "type": "deviceNode",
                "position": {"x": 300, "y": 0},
                "data": {
                    "device": {
                        "id": "55555555-5555-5555-5555-555555555555",
                        "name": "sw-2",
                    }
                },
            },
            "not-a-dict-node",
        ],
        "edges": [
            {
                "id": "e1",
                "source": "n1",
                "target": "n4",
                "data": {"layer": "L1", "sourcePort": "eth0", "targetPort": "eth1"},
            }
        ],
        "viewport": {"x": 0, "y": 0, "zoom": 1},
    }


def test_strips_field_data_and_non_allowlisted_keys():
    canvas = _canvas_with_everything()
    result = strip_device_nodes(canvas)

    stripped_device = result["nodes"][0]["data"]["device"]
    assert "field_data" not in stripped_device
    assert "driver_sha256" not in stripped_device
    assert "exclusive" not in stripped_device
    assert "template_id" not in stripped_device


def test_keeps_allowlisted_keys_byte_for_byte():
    canvas = _canvas_with_everything()
    result = strip_device_nodes(canvas)

    stripped_device = result["nodes"][0]["data"]["device"]
    original_device = canvas["nodes"][0]["data"]["device"]
    for key in DEVICE_NODE_ALLOWED_KEYS & original_device.keys():
        assert stripped_device[key] == original_device[key]
    # Exactly the intersection of the allowlist and what was present survives.
    assert set(stripped_device.keys()) == DEVICE_NODE_ALLOWED_KEYS & original_device.keys()


def test_other_node_types_and_edges_untouched():
    canvas = _canvas_with_everything()
    result = strip_device_nodes(canvas)

    assert result["nodes"][1] == canvas["nodes"][1]  # network element node
    assert result["nodes"][2] == canvas["nodes"][2]  # dynamic placeholder node
    assert result["nodes"][4] == "not-a-dict-node"  # malformed node passed through
    assert result["edges"] == canvas["edges"]
    assert result["viewport"] == canvas["viewport"]


def test_thin_device_node_with_no_extra_keys_is_unchanged_by_reference():
    canvas = _canvas_with_everything()
    result = strip_device_nodes(canvas)
    # n4's device dict already only carries allowlisted keys (id, name): the
    # node object itself should come back unchanged.
    assert result["nodes"][3] is canvas["nodes"][3]


def test_does_not_mutate_input():
    canvas = _canvas_with_everything()
    original = copy.deepcopy(canvas)
    strip_device_nodes(canvas)
    assert canvas == original


def test_idempotent():
    canvas = _canvas_with_everything()
    once = strip_device_nodes(canvas)
    twice = strip_device_nodes(once)
    assert once == twice


def test_no_change_returns_same_canvas_object():
    canvas = {
        "nodes": [
            {
                "id": "n1",
                "type": "deviceNode",
                "data": {"device": {"id": "1", "name": "sw-1"}, "label": "sw-1"},
            }
        ],
        "edges": [],
    }
    result = strip_device_nodes(canvas)
    assert result is canvas


def test_none_and_non_dict_canvas_pass_through():
    assert strip_device_nodes(None) is None
    assert strip_device_nodes({}) == {}
    assert strip_device_nodes({"nodes": "not-a-list"}) == {"nodes": "not-a-list"}


def test_device_node_with_non_dict_device_left_as_is():
    canvas = {
        "nodes": [{"id": "n1", "type": "deviceNode", "data": {"device": "not-a-dict"}}],
        "edges": [],
    }
    result = strip_device_nodes(canvas)
    assert result is canvas


def test_role_only_template_device_dict_survives():
    """Topology-template canvases carry {"role": ...} in place of a real
    device before instantiation (routes/templates.py's
    _extract_role_template); this synthetic key must survive stripping."""
    canvas = {
        "nodes": [{"id": "n1", "type": "deviceNode", "data": {"device": {"role": "switch-1"}}}],
        "edges": [],
    }
    result = strip_device_nodes(canvas)
    assert result["nodes"][0]["data"]["device"] == {"role": "switch-1"}


def test_legacy_node_with_no_type_tag_still_stripped():
    """A thin/legacy node saved before the seed fix set the deviceNode type
    discriminator: strip_device_nodes classifies by the presence of
    data.device, not by node.type, so field_data on such a node is still
    removed."""
    canvas = {
        "nodes": [
            {
                "id": "n1",
                "type": None,
                "data": {"device": {"id": "1", "field_data": {"password": "x"}}},
            }
        ],
        "edges": [],
    }
    result = strip_device_nodes(canvas)
    assert "field_data" not in result["nodes"][0]["data"]["device"]
    assert result["nodes"][0]["data"]["device"]["id"] == "1"
