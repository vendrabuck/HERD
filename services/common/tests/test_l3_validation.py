"""Unit tests for the shared per-route Layer 3 validation pass.

Two jobs. First, pin the MOVER (ADR 0014 addendum X-I, issue #755): cabling's
``_validate_one_route`` and execution's ``_validate_route_at_drive_time`` were
two hand-synced copies of this logic and are now thin unpackers over it, so the
reason vocabulary and evaluation order are pinned here once, exactly as both
copies produced them before the move. Second, pin the three VRF reasons X-I
adds, including the case a config written before X-I hits: no
``virtual_routers`` key at all. Third, pin X-K's interface-level attachment
check (issue #756) and the X-J config fields it reads.
"""

from __future__ import annotations

from herd_common.l3_validation import (
    InterfaceAttachment,
    usable_interface_attachments,
    usable_interfaces,
    usable_virtual_routers,
    validate_one_route,
)

IFACES = {"eth0": "10.0.0.1/24", "eth1": None, "dummy0": "192.0.2.254/30"}


# --- usable_interfaces (moved verbatim from both copies) --------------------


def test_usable_interfaces_maps_name_to_ip():
    assert usable_interfaces([{"name": "eth0", "ip": "10.0.0.1/24"}]) == {"eth0": "10.0.0.1/24"}


def test_usable_interfaces_keeps_an_entry_with_no_ip():
    assert usable_interfaces([{"name": "eth1"}]) == {"eth1": None}


def test_usable_interfaces_tolerates_a_non_list():
    assert usable_interfaces(None) == {}
    assert usable_interfaces({"eth0": "10.0.0.1/24"}) == {}
    assert usable_interfaces("eth0") == {}


def test_usable_interfaces_skips_garbled_entries():
    raw = [
        "eth0",
        {"ip": "10.0.0.1/24"},
        {"name": "", "ip": "x"},
        {"name": "eth2", "ip": "1.2.3.4/30"},
    ]
    assert usable_interfaces(raw) == {"eth2": "1.2.3.4/30"}


# --- usable_virtual_routers (new with X-I) ----------------------------------


def test_usable_virtual_routers_maps_name_to_member_set():
    raw = [{"name": "blue", "interfaces": ["dummy0", "eth1"]}]
    assert usable_virtual_routers(raw) == {"blue": {"dummy0", "eth1"}}


def test_usable_virtual_routers_is_empty_for_a_missing_key():
    assert usable_virtual_routers(None) == {}


def test_usable_virtual_routers_tolerates_garbled_entries():
    raw = [
        "blue",
        {"interfaces": ["dummy0"]},
        {"name": "", "interfaces": ["dummy0"]},
        {"name": "red", "interfaces": "dummy0"},
        {"name": "green", "interfaces": ["dummy1", 7, None, ""]},
    ]
    assert usable_virtual_routers(raw) == {"red": set(), "green": {"dummy1"}}


def test_usable_virtual_routers_unions_duplicate_names():
    raw = [{"name": "blue", "interfaces": ["a"]}, {"name": "blue", "interfaces": ["b"]}]
    assert usable_virtual_routers(raw) == {"blue": {"a", "b"}}


# --- The pre-X-I reason vocabulary and order, unchanged ---------------------


def test_bad_destination():
    assert (
        validate_one_route("not-a-prefix", "10.0.0.2", "eth0", None, interfaces=IFACES)
        == "l3_bad_destination"
    )


def test_bad_destination_for_a_missing_field():
    assert validate_one_route(None, None, "eth0", None, interfaces=IFACES) == "l3_bad_destination"


def test_bad_next_hop():
    assert (
        validate_one_route("10.1.0.0/24", "nope", "eth0", None, interfaces=IFACES)
        == "l3_bad_next_hop"
    )


def test_unknown_interface():
    assert (
        validate_one_route("10.1.0.0/24", "10.0.0.2", "eth9", None, interfaces=IFACES)
        == "l3_unknown_interface"
    )


def test_next_hop_unverifiable_when_the_interface_has_no_ip():
    assert (
        validate_one_route("10.1.0.0/24", "10.0.0.2", "eth1", None, interfaces=IFACES)
        == "l3_next_hop_unverifiable"
    )


def test_next_hop_unverifiable_when_the_interface_ip_has_no_prefix_length():
    assert (
        validate_one_route("10.1.0.0/24", "10.0.0.2", "eth0", None, interfaces={"eth0": "10.0.0.1"})
        == "l3_next_hop_unverifiable"
    )


def test_next_hop_outside_interface():
    assert (
        validate_one_route("10.1.0.0/24", "172.16.0.2", "eth0", None, interfaces=IFACES)
        == "l3_next_hop_outside_interface"
    )


def test_interface_route_skips_every_next_hop_check():
    assert validate_one_route("10.1.0.0/24", None, "eth1", None, interfaces=IFACES) is None


def test_clean_route_returns_none():
    assert validate_one_route("10.1.0.0/24", "10.0.0.2", "eth0", None, interfaces=IFACES) is None


def test_destination_check_precedes_the_next_hop_check():
    # Both fields are malformed; destination is evaluated first.
    assert (
        validate_one_route("nope", "nope", "eth0", None, interfaces=IFACES) == "l3_bad_destination"
    )


def test_unknown_interface_precedes_the_next_hop_subnet_checks():
    assert (
        validate_one_route("10.1.0.0/24", "172.16.0.2", "eth9", None, interfaces=IFACES)
        == "l3_unknown_interface"
    )


# --- X-I: the three VRF reasons ---------------------------------------------

VRFS = {"blue": {"dummy0"}}


def test_unknown_virtual_router():
    assert (
        validate_one_route(
            "10.1.0.0/24", None, "dummy0", "green", interfaces=IFACES, virtual_routers=VRFS
        )
        == "l3_unknown_virtual_router"
    )


def test_unknown_virtual_router_when_the_config_declares_no_virtual_routers_key():
    # A config written before X-I carries no `virtual_routers` key at all, which
    # reaches here as {} (or as the default None): it declares no VRF, so a route
    # naming one is refused rather than silently driven into the default table.
    assert (
        validate_one_route("10.1.0.0/24", None, "dummy0", "blue", interfaces=IFACES)
        == "l3_unknown_virtual_router"
    )
    assert (
        validate_one_route(
            "10.1.0.0/24", None, "dummy0", "blue", interfaces=IFACES, virtual_routers={}
        )
        == "l3_unknown_virtual_router"
    )


def test_unknown_virtual_router_for_a_non_string_name():
    # An unhashable VRF name must produce the reason, never a TypeError.
    assert (
        validate_one_route(
            "10.1.0.0/24", None, "dummy0", {"name": "blue"}, interfaces=IFACES, virtual_routers=VRFS
        )
        == "l3_unknown_virtual_router"
    )


def test_interface_outside_virtual_router():
    assert (
        validate_one_route(
            "10.1.0.0/24", None, "eth1", "blue", interfaces=IFACES, virtual_routers=VRFS
        )
        == "l3_interface_outside_virtual_router"
    )


def test_interface_bound_to_virtual_router():
    # No VRF named, but the interface is enslaved to one: a default-table route
    # through it can never install.
    assert (
        validate_one_route(
            "10.1.0.0/24", None, "dummy0", None, interfaces=IFACES, virtual_routers=VRFS
        )
        == "l3_interface_bound_to_virtual_router"
    )


def test_a_route_in_its_own_virtual_router_is_clean():
    assert (
        validate_one_route(
            "10.1.0.0/24", None, "dummy0", "blue", interfaces=IFACES, virtual_routers=VRFS
        )
        is None
    )


def test_a_default_table_route_is_clean_while_another_interface_is_vrf_bound():
    assert (
        validate_one_route(
            "10.1.0.0/24", "10.0.0.2", "eth0", None, interfaces=IFACES, virtual_routers=VRFS
        )
        is None
    )


def test_unknown_interface_precedes_every_vrf_reason():
    assert (
        validate_one_route(
            "10.1.0.0/24", None, "eth9", "green", interfaces=IFACES, virtual_routers=VRFS
        )
        == "l3_unknown_interface"
    )


def test_unknown_virtual_router_precedes_interface_outside_virtual_router():
    assert (
        validate_one_route(
            "10.1.0.0/24", None, "eth1", "green", interfaces=IFACES, virtual_routers=VRFS
        )
        == "l3_unknown_virtual_router"
    )


def test_vrf_reasons_precede_the_next_hop_subnet_checks():
    # The next hop is outside eth1's subnet too, but the VRF problem wins.
    assert (
        validate_one_route(
            "10.1.0.0/24", "172.16.0.2", "eth1", "blue", interfaces=IFACES, virtual_routers=VRFS
        )
        == "l3_interface_outside_virtual_router"
    )


def test_a_vrf_route_still_gets_the_next_hop_subnet_checks():
    # dummy0 is 192.0.2.254/30 (192.0.2.252 to 192.0.2.255), so 10.9.9.9 is outside it.
    assert (
        validate_one_route(
            "10.1.0.0/24", "10.9.9.9", "dummy0", "blue", interfaces=IFACES, virtual_routers=VRFS
        )
        == "l3_next_hop_outside_interface"
    )


# --- X-K: interface-level attachment (issue #756) ----------------------------

# The switch's own wiring, as cabling derives it from the resolved hops and
# execution from the fork's intended wires: one cabled port, ge-0/0/1.
WIRED = {"ge-0/0/1"}

# eth0 is the physical interface that port carries (the OS name and the HERD
# port name differ, so X-J's explicit `port` is what ties them together); sv100
# is an SVI; eth9 is a physical interface nothing is cabled to.
ATTACHMENTS = {
    "eth0": InterfaceAttachment(kind="physical", port="ge-0/0/1"),
    "sv100": InterfaceAttachment(kind="logical", port="sv100"),
    "eth9": InterfaceAttachment(kind="physical", port="eth9"),
}
X_K_IFACES = {"eth0": "10.0.0.1/24", "sv100": "10.9.0.1/24", "eth9": "10.8.0.1/24"}


def test_usable_interface_attachments_defaults_kind_physical_and_port_to_name():
    assert usable_interface_attachments([{"name": "eth0", "ip": "10.0.0.1/24"}]) == {
        "eth0": InterfaceAttachment(kind="physical", port="eth0")
    }


def test_usable_interface_attachments_reads_both_declared_fields():
    raw = [
        {"name": "eth0", "kind": "physical", "port": "ge-0/0/1"},
        {"name": "dummy0", "kind": "logical"},
    ]
    assert usable_interface_attachments(raw) == {
        "eth0": InterfaceAttachment(kind="physical", port="ge-0/0/1"),
        "dummy0": InterfaceAttachment(kind="logical", port="dummy0"),
    }


def test_usable_interface_attachments_falls_back_to_strict_defaults_on_garbage():
    """A garbled value lands on the CHECKED side, never the exempt one: an
    unknown `kind` is physical and a non-string or empty `port` is the name."""
    raw = [
        "eth0",
        {"ip": "10.0.0.1/24"},
        {"name": "eth1", "kind": "virtual", "port": 7},
        {"name": "eth2", "kind": None, "port": ""},
    ]
    assert usable_interface_attachments(raw) == {
        "eth1": InterfaceAttachment(kind="physical", port="eth1"),
        "eth2": InterfaceAttachment(kind="physical", port="eth2"),
    }


def test_usable_interface_attachments_tolerates_a_non_list():
    assert usable_interface_attachments(None) == {}
    assert usable_interface_attachments("eth0") == {}


def test_a_route_on_a_wired_physical_interface_is_clean():
    assert (
        validate_one_route(
            "10.1.0.0/24",
            "10.0.0.2",
            "eth0",
            None,
            interfaces=X_K_IFACES,
            interface_attachments=ATTACHMENTS,
            wired_ports=WIRED,
        )
        is None
    )


def test_a_route_on_an_unwired_physical_interface_is_refused():
    assert (
        validate_one_route(
            "10.1.0.0/24",
            None,
            "eth9",
            None,
            interfaces=X_K_IFACES,
            interface_attachments=ATTACHMENTS,
            wired_ports=WIRED,
        )
        == "l3_interface_unwired"
    )


def test_a_route_on_a_logical_interface_is_exempt():
    """A loopback, an SVI, or a dummy device enslaved to a VRF has no port of
    its own; the switch-level attachment check stands for it instead."""
    assert (
        validate_one_route(
            "10.1.0.0/24",
            None,
            "sv100",
            None,
            interfaces=X_K_IFACES,
            interface_attachments=ATTACHMENTS,
            wired_ports=WIRED,
        )
        is None
    )


def test_the_explicit_port_mapping_is_what_resolves_against_wired_ports():
    """eth0's own NAME is not a wired port; only its declared `port` is. Drop
    the `port` field and the same route refuses."""
    without_port = {"eth0": InterfaceAttachment(kind="physical", port="eth0")}
    assert (
        validate_one_route(
            "10.1.0.0/24",
            "10.0.0.2",
            "eth0",
            None,
            interfaces=X_K_IFACES,
            interface_attachments=without_port,
            wired_ports=WIRED,
        )
        == "l3_interface_unwired"
    )


def test_an_absent_kind_is_physical_and_therefore_checked():
    """A config written before X-J declares neither field: every interface is
    physical with port == name (the decided strict posture)."""
    attachments = usable_interface_attachments([{"name": "eth0", "ip": "10.0.0.1/24"}])
    assert (
        validate_one_route(
            "10.1.0.0/24",
            "10.0.0.2",
            "eth0",
            None,
            interfaces=X_K_IFACES,
            interface_attachments=attachments,
            wired_ports=WIRED,
        )
        == "l3_interface_unwired"
    )
    assert (
        validate_one_route(
            "10.1.0.0/24",
            "10.0.0.2",
            "eth0",
            None,
            interfaces=X_K_IFACES,
            interface_attachments=attachments,
            wired_ports={"eth0"},
        )
        is None
    )


def test_no_wired_ports_argument_skips_the_check_entirely():
    """`wired_ports=None` is "the caller has no resolved hops in hand", the
    pre-X-K behavior; an EMPTY SET is "nothing is wired" and refuses."""
    assert (
        validate_one_route(
            "10.1.0.0/24",
            None,
            "eth9",
            None,
            interfaces=X_K_IFACES,
            interface_attachments=ATTACHMENTS,
        )
        is None
    )
    assert (
        validate_one_route(
            "10.1.0.0/24",
            None,
            "eth9",
            None,
            interfaces=X_K_IFACES,
            interface_attachments=ATTACHMENTS,
            wired_ports=set(),
        )
        == "l3_interface_unwired"
    )


def test_an_interface_with_no_attachment_entry_takes_the_strict_default():
    """Mismatched maps (an interface in `interfaces` with no attachment entry)
    must not read as exempt."""
    assert (
        validate_one_route(
            "10.1.0.0/24",
            None,
            "eth9",
            None,
            interfaces=X_K_IFACES,
            interface_attachments={},
            wired_ports=WIRED,
        )
        == "l3_interface_unwired"
    )


def test_unknown_interface_precedes_the_unwired_check():
    assert (
        validate_one_route(
            "10.1.0.0/24",
            None,
            "eth404",
            None,
            interfaces=X_K_IFACES,
            interface_attachments=ATTACHMENTS,
            wired_ports=WIRED,
        )
        == "l3_unknown_interface"
    )


def test_the_unwired_check_precedes_every_vrf_reason():
    """An unwired physical interface enslaved to a VRF reports the wiring
    problem, not the VRF one: the order is shape, interface, wiring, VRF."""
    assert (
        validate_one_route(
            "10.1.0.0/24",
            None,
            "eth9",
            None,
            interfaces=X_K_IFACES,
            interface_attachments=ATTACHMENTS,
            virtual_routers={"blue": {"eth9"}},
            wired_ports=WIRED,
        )
        == "l3_interface_unwired"
    )


def test_a_logical_vrf_member_still_gets_the_vrf_reasons():
    """The lab's shape (ADR 0014 addendum X-G): `dummy0` is logical, enslaved to
    `blue`, and a route naming the wrong VRF still refuses on the VRF reason."""
    attachments = usable_interface_attachments([{"name": "dummy0", "kind": "logical"}])
    assert (
        validate_one_route(
            "10.1.0.0/24",
            None,
            "dummy0",
            "green",
            interfaces={"dummy0": "192.0.2.254/30"},
            interface_attachments=attachments,
            virtual_routers={"blue": {"dummy0"}},
            wired_ports=WIRED,
        )
        == "l3_unknown_virtual_router"
    )
