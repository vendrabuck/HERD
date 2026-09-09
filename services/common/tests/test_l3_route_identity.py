"""Unit tests for the shared Layer 3 route identity helper (ADR 0014, issue #757).

Pins ``route_identity_key`` byte-identical to the packing cabling's
``RouteSpec.route_key`` used before the round-2 S4 review fix moved it here, and
covers the properties that fix established: virtual_router participates in
identity, empty strings normalize to null, and destination is canonicalized.
"""

import json

from herd_common.l3_route_identity import route_identity_key


def test_matches_the_literal_pre_extraction_cabling_packing():
    """Byte-identical to `json.dumps([destination, interface, next_hop or "",
    virtual_router or ""], ensure_ascii=False)`, the exact expression
    services/cabling/app/services/l3_intent.py's RouteSpec.route_key used
    before it was extracted here (issue #757)."""
    destination, interface, next_hop, virtual_router = (
        "10.20.0.0/24",
        "eth1",
        "10.0.0.2",
        "default",
    )
    expected = json.dumps(
        [destination, interface, next_hop or "", virtual_router or ""],
        ensure_ascii=False,
    )
    assert route_identity_key(destination, interface, next_hop, virtual_router) == expected


def test_matches_literal_with_null_next_hop_and_virtual_router():
    destination, interface = "10.20.0.0/24", "eth1"
    expected = json.dumps([destination, interface, "", ""], ensure_ascii=False)
    assert route_identity_key(destination, interface, None, None) == expected


def test_virtual_router_is_part_of_identity():
    """Two routes differing only by virtual_router are distinct keys (S4)."""
    a = route_identity_key("10.0.0.0/24", "eth0", "10.0.0.1", "red")
    b = route_identity_key("10.0.0.0/24", "eth0", "10.0.0.1", "blue")
    assert a != b


def test_empty_string_next_hop_normalizes_to_null():
    with_empty = route_identity_key("10.0.0.0/24", "eth0", "", "default")
    with_none = route_identity_key("10.0.0.0/24", "eth0", None, "default")
    assert with_empty == with_none


def test_empty_string_virtual_router_normalizes_to_null():
    with_empty = route_identity_key("10.0.0.0/24", "eth0", "10.0.0.1", "")
    with_none = route_identity_key("10.0.0.0/24", "eth0", "10.0.0.1", None)
    assert with_empty == with_none


def test_destination_is_canonicalized():
    """A host-form destination packs the same key as its canonical network form."""
    host_form = route_identity_key("10.0.0.5/24", "eth0", None, None)
    canonical_form = route_identity_key("10.0.0.0/24", "eth0", None, None)
    assert host_form == canonical_form


def test_malformed_destination_kept_verbatim_not_raised():
    """Identity packing never refuses a bad destination; validation does."""
    key = route_identity_key("not-an-ip", "eth0", None, None)
    assert json.loads(key)[0] == "not-an-ip"


def test_distinct_routes_produce_distinct_keys():
    a = route_identity_key("10.0.0.0/24", "eth0", "10.0.0.1", None)
    b = route_identity_key("10.0.1.0/24", "eth0", "10.0.0.1", None)
    c = route_identity_key("10.0.0.0/24", "eth1", "10.0.0.1", None)
    d = route_identity_key("10.0.0.0/24", "eth0", "10.0.0.2", None)
    assert len({a, b, c, d}) == 4
