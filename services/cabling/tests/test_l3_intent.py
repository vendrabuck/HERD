"""Unit tests for the L3 routing-intent canvas parser (ADR 0014 phase 1, issue #34)."""

import uuid

import pytest
from app.services.l3_intent import L3IntentMalformed, RouteSpec, parse_l3_intent, parse_node_l3

DEVICE_A = uuid.uuid4()
DEVICE_B = uuid.uuid4()


def _canvas(nodes: list[dict]) -> dict:
    return {"nodes": nodes, "edges": []}


def _switch_node(node_id: str, device_id: uuid.UUID, l3: dict | None = None) -> dict:
    data: dict = {"device": {"id": str(device_id)}}
    if l3 is not None:
        data["l3"] = l3
    return {"id": node_id, "data": data}


def _element_node(node_id: str, l3: dict | None = None) -> dict:
    data: dict = {"element": {"id": "elem-1"}}
    if l3 is not None:
        data["l3"] = l3
    return {"id": node_id, "type": "networkElementNode", "data": data}


# --- RouteSpec.route_key ---


def test_route_key_packs_destination_interface_next_hop():
    spec = RouteSpec(
        destination="10.20.0.0/24", next_hop="10.0.0.2", interface="eth1", virtual_router=None
    )
    assert spec.route_key == "10.20.0.0/24|eth1|10.0.0.2"


def test_route_key_empty_string_for_null_next_hop():
    spec = RouteSpec(
        destination="10.20.0.0/24", next_hop=None, interface="eth1", virtual_router=None
    )
    assert spec.route_key == "10.20.0.0/24|eth1|"


# --- parse_node_l3 / parse_l3_intent: valid shapes ---


def test_parse_valid_single_route():
    l3 = {"routes": [{"destination": "10.20.0.0/24", "next_hop": "10.0.0.2", "interface": "eth1"}]}
    routes = parse_node_l3("n1", l3)
    assert routes == [
        RouteSpec(
            destination="10.20.0.0/24", next_hop="10.0.0.2", interface="eth1", virtual_router=None
        )
    ]


def test_parse_valid_interface_route_no_next_hop():
    l3 = {"routes": [{"destination": "0.0.0.0/0", "interface": "eth0"}]}
    routes = parse_node_l3("n1", l3)
    assert routes[0].next_hop is None


def test_parse_valid_with_virtual_router():
    l3 = {
        "routes": [
            {
                "destination": "10.20.0.0/24",
                "next_hop": "10.0.0.2",
                "interface": "eth1",
                "virtual_router": "default",
            }
        ]
    }
    routes = parse_node_l3("n1", l3)
    assert routes[0].virtual_router == "default"


def test_parse_empty_routes_list_is_valid():
    assert parse_node_l3("n1", {"routes": []}) == []


def test_parse_l3_intent_whole_canvas():
    canvas = _canvas(
        [
            _switch_node(
                "n1",
                DEVICE_A,
                {"routes": [{"destination": "10.0.0.0/24", "interface": "eth0"}]},
            ),
            _switch_node("n2", DEVICE_B),  # no l3 key: absent from the result
        ]
    )
    result = parse_l3_intent(canvas)
    assert set(result.keys()) == {DEVICE_A}
    assert len(result[DEVICE_A]) == 1


def test_parse_l3_intent_none_canvas_returns_empty():
    assert parse_l3_intent(None) == {}


def test_parse_l3_intent_no_nodes_key_returns_empty():
    assert parse_l3_intent({}) == {}


# --- Duplicate collapse ---


def test_duplicate_route_key_collapses_to_first_occurrence():
    l3 = {
        "routes": [
            {"destination": "10.0.0.0/24", "interface": "eth0", "next_hop": "10.0.0.1"},
            {"destination": "10.0.0.0/24", "interface": "eth0", "next_hop": "10.0.0.1"},
        ]
    }
    routes = parse_node_l3("n1", l3)
    assert len(routes) == 1


def test_duplicate_route_key_keeps_first_when_fields_differ_only_in_dupe_key():
    # Same route_key (destination|interface|next_hop) but let's confirm ordering:
    # the first occurrence wins even though a later one is otherwise identical.
    l3 = {
        "routes": [
            {"destination": "10.0.0.0/24", "interface": "eth0"},
            {"destination": "10.0.0.0/24", "interface": "eth0", "virtual_router": "vr2"},
        ]
    }
    routes = parse_node_l3("n1", l3)
    assert len(routes) == 1
    assert routes[0].virtual_router is None


# --- Nodes without l3 / element nodes ignored ---


def test_node_without_l3_key_is_absent_from_result():
    canvas = _canvas([_switch_node("n1", DEVICE_A)])
    assert parse_l3_intent(canvas) == {}


def test_element_node_l3_is_never_read():
    """An element node never resolves through node_to_device_map, so a stray l3
    key on one (malformed usage) is silently never parsed, not even to raise."""
    canvas = _canvas([_element_node("e1", {"routes": "not-a-list-but-never-read"})])
    assert parse_l3_intent(canvas) == {}


def test_node_with_no_device_id_is_skipped():
    canvas = _canvas([{"id": "n1", "data": {"l3": {"routes": []}}}])
    assert parse_l3_intent(canvas) == {}


# --- Malformed shapes ---


def test_malformed_l3_not_an_object():
    with pytest.raises(L3IntentMalformed) as exc:
        parse_node_l3("n1", ["not", "a", "dict"])
    assert exc.value.node_id == "n1"


def test_malformed_l3_extra_top_level_key():
    with pytest.raises(L3IntentMalformed):
        parse_node_l3("n1", {"routes": [], "extra": 1})


def test_malformed_l3_missing_routes_key():
    with pytest.raises(L3IntentMalformed):
        parse_node_l3("n1", {})


def test_malformed_routes_not_a_list():
    with pytest.raises(L3IntentMalformed):
        parse_node_l3("n1", {"routes": "not-a-list"})


def test_malformed_route_item_not_an_object():
    with pytest.raises(L3IntentMalformed):
        parse_node_l3("n1", {"routes": ["not-a-dict"]})


def test_malformed_route_unexpected_key():
    with pytest.raises(L3IntentMalformed):
        parse_node_l3(
            "n1",
            {"routes": [{"destination": "10.0.0.0/24", "interface": "eth0", "bogus": "x"}]},
        )


def test_malformed_route_missing_destination():
    with pytest.raises(L3IntentMalformed):
        parse_node_l3("n1", {"routes": [{"interface": "eth0"}]})


def test_malformed_route_missing_interface():
    with pytest.raises(L3IntentMalformed):
        parse_node_l3("n1", {"routes": [{"destination": "10.0.0.0/24"}]})


def test_malformed_route_empty_destination():
    with pytest.raises(L3IntentMalformed):
        parse_node_l3("n1", {"routes": [{"destination": "", "interface": "eth0"}]})


def test_malformed_route_destination_too_long():
    with pytest.raises(L3IntentMalformed):
        parse_node_l3("n1", {"routes": [{"destination": "x" * 65, "interface": "eth0"}]})


def test_malformed_route_interface_wrong_type():
    with pytest.raises(L3IntentMalformed):
        parse_node_l3("n1", {"routes": [{"destination": "10.0.0.0/24", "interface": 5}]})


def test_malformed_route_next_hop_too_long():
    with pytest.raises(L3IntentMalformed):
        parse_node_l3(
            "n1",
            {"routes": [{"destination": "10.0.0.0/24", "interface": "eth0", "next_hop": "x" * 65}]},
        )


def test_malformed_route_virtual_router_wrong_type():
    with pytest.raises(L3IntentMalformed):
        parse_node_l3(
            "n1",
            {"routes": [{"destination": "10.0.0.0/24", "interface": "eth0", "virtual_router": 5}]},
        )


def test_malformed_route_next_hop_null_is_allowed():
    routes = parse_node_l3(
        "n1", {"routes": [{"destination": "10.0.0.0/24", "interface": "eth0", "next_hop": None}]}
    )
    assert routes[0].next_hop is None


def test_parse_l3_intent_raises_on_first_malformed_node_in_canvas_order():
    canvas = _canvas(
        [
            _switch_node("n1", DEVICE_A, {"routes": []}),
            _switch_node("n2", DEVICE_B, {"bad": "shape"}),
        ]
    )
    with pytest.raises(L3IntentMalformed) as exc:
        parse_l3_intent(canvas)
    assert exc.value.node_id == "n2"
