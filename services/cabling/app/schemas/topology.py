from datetime import datetime
from typing import Any

from app.schemas._types import OptionalUUIDStr, UUIDStr, UUIDStrList
from pydantic import BaseModel, Field


class TopologyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class TopologyClone(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class TopologyUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    canvas_data: dict[str, Any] | None = None
    description: str | None = Field(default=None, max_length=2000)


class TopologyResponse(BaseModel):
    id: UUIDStr
    name: str
    created_by: UUIDStr
    owner_name: str = ""
    created_at: datetime
    updated_at: datetime
    modified_by: OptionalUUIDStr = None

    model_config = {"from_attributes": True}


class TopologyDetail(TopologyResponse):
    canvas_data: dict[str, Any] | None = None


class PaginatedTopologyResponse(BaseModel):
    items: list[TopologyResponse]
    total: int
    skip: int
    limit: int


class TopologyVersionResponse(BaseModel):
    id: UUIDStr
    topology_id: UUIDStr
    version_number: int
    name: str
    description: str | None = None
    created_by: UUIDStr
    author_name: str
    created_at: datetime
    restored_from_id: OptionalUUIDStr = None

    model_config = {"from_attributes": True}


class TopologyVersionDetail(TopologyVersionResponse):
    canvas_data: dict[str, Any] | None = None


class PaginatedTopologyVersionResponse(BaseModel):
    items: list[TopologyVersionResponse]
    total: int
    skip: int
    limit: int


class ModifiedItem(BaseModel):
    id: str
    before: dict[str, Any]
    after: dict[str, Any]


class TopologyVersionDiff(BaseModel):
    version_a: UUIDStr
    version_b: UUIDStr
    nodes_added: list[dict[str, Any]]
    nodes_removed: list[dict[str, Any]]
    nodes_modified: list[ModifiedItem]
    edges_added: list[dict[str, Any]]
    edges_removed: list[dict[str, Any]]
    edges_modified: list[ModifiedItem]


class TopologyRestoreRequest(BaseModel):
    description: str | None = Field(default=None, max_length=2000)
    restore_name: bool = False


class InvalidEdge(BaseModel):
    """One canvas edge `validate_canvas_edges` could not accept, with why.

    ``reason`` is a plain str (no enum), currently one of:

    - ``missing_device``: an endpoint's node id resolves to neither a known device nor
      a network element.
    - ``no_path``: both endpoints are known devices, but the cabling graph has no
      physical path between them.
    - ``element_to_element`` (ADR 0012 phase 1, issue #22): both endpoints are network
      element nodes. Two elements have no device and no port between them.
    - ``element_edge_no_port`` (ADR 0012 phase 1): one endpoint is a network element
      and the other a known device, but the device-side port name
      (``source_port_name``/``target_port_name``, whichever names the device) is
      missing or empty.

    A device-to-element edge with a non-empty device-side port name is VALID and never
    appears here.
    """

    edge_id: str
    source_device_id: OptionalUUIDStr = None
    target_device_id: OptionalUUIDStr = None
    layer: str | None = None
    reason: str


class InvalidRoute(BaseModel):
    """One Layer 3 routing-intent problem `validate_canvas_l3` found.

    ADR 0014 phase 1, issue #34. ``index`` is the route's ORIGINAL position within
    its node's ``data.l3.routes`` list (S12 review fix, round 2: never shifted by
    a duplicate collapsed earlier in the same list), or ``null`` for a switch-level
    refusal that stopped evaluation before any per-route check ran
    (``l3_malformed``, ``l3_not_a_router``, ``l3_switch_unconfigured``,
    ``l3_switch_unattached``). ``detail`` carries the parser's message for
    ``l3_malformed`` and is null for every other reason.

    ``reason`` is one of, in the order evaluated per switch (a switch-level reason
    stops further evaluation for that switch; per-route reasons, and
    ``l3_duplicate_route``, are all reported):

    - ``l3_malformed``: the node's ``data.l3`` does not match the ADR 0014 shape.
    - ``l3_not_a_router``: the device is not a ``Layer 3 Switch``.
    - ``l3_switch_unconfigured``: no latest config version, or its config lists no
      interfaces.
    - ``l3_switch_unattached``: the switch is not an endpoint of at least one
      resolved hop in this canvas (transit devices on a multi-hop path count;
      element attachments do not).
    - ``l3_bad_destination``: ``destination`` is not a parseable IP prefix.
    - ``l3_bad_next_hop``: ``next_hop`` is present and not a parseable IP address.
    - ``l3_unknown_interface``: ``interface`` is not among the config's interface
      names.
    - ``l3_unknown_virtual_router`` (ADR 0014 addendum X-I, issue #755): the route
      names a ``virtual_router`` the config's ``virtual_routers`` does not declare.
      A config with no ``virtual_routers`` key declares none, so every VRF-naming
      route refuses here.
    - ``l3_interface_outside_virtual_router`` (X-I): the route names a declared
      VRF, but ``interface`` is not one of that VRF's ``interfaces``.
    - ``l3_interface_bound_to_virtual_router`` (X-I): the route names no VRF, but
      ``interface`` is listed under one. An interface enslaved to a VRF is not in
      the default routing table, so a default-table route through it can never
      install.
    - ``l3_next_hop_unverifiable``: ``next_hop`` is present and the named interface
      carries no ``ip``, or an ``ip`` with no real prefix length.
    - ``l3_next_hop_outside_interface``: the interface's ``ip`` network does not
      contain ``next_hop``.
    - ``l3_duplicate_route`` (S12 review fix, round 2): this route's identity
      duplicates an earlier one in the same node's list and was collapsed to it.
      INFORMATIONAL: reported so nothing vanishes silently, but never makes
      ``valid`` false and never makes a save gate refuse (see
      ``l3_validation.route_causes_invalid``).
    """

    node_id: str
    device_id: OptionalUUIDStr = None
    index: int | None = None
    reason: str
    detail: str | None = None


class TopologyValidationResponse(BaseModel):
    valid: bool
    invalid_edges: list[InvalidEdge]
    # Issue #701 (fork endpoint-membership fix, phase 2): the canvas's device node
    # ids, deduplicated and sorted, using the same node_to_device resolution as the
    # edge walk above. A dynamic placeholder or network element node is never a
    # device (neither carries `data.device.id`) so neither ever appears here.
    # Additive on this response so reservations' create-time membership check can
    # ride the existing single validate/internal call instead of a second one.
    device_ids: UUIDStrList = Field(default_factory=list)
    # ADR 0014 phase 1 (issue #34): Layer 3 routing-intent problems, additive
    # alongside invalid_edges. `valid` is False when either list is non-empty.
    invalid_routes: list[InvalidRoute] = Field(default_factory=list)
