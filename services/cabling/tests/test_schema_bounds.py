"""Length-bound tests for cabling schemas (#129, #130).

Pydantic Field constraints are pure and synchronous, so these exercise the
boundary directly via model construction, no DB or HTTP. Each test pins the
boundary (cap+1 rejected, cap accepted), not just "rejects something huge".
"""

import uuid

import pytest
from app.schemas.connection import ConnectionCreate
from app.schemas.template import (
    InstantiateRequest,
    TemplateCreate,
    TemplateFromTopologyRequest,
    TemplateUpdate,
)
from app.schemas.topology import TopologyClone, TopologyCreate, TopologyUpdate
from pydantic import ValidationError


def test_topology_name_empty_rejected():
    with pytest.raises(ValidationError):
        TopologyCreate(name="")


def test_topology_name_at_cap_accepted():
    TopologyCreate(name="x" * 100)


def test_topology_name_over_cap_rejected():
    with pytest.raises(ValidationError):
        TopologyCreate(name="x" * 101)


def test_topology_clone_name_empty_rejected():
    with pytest.raises(ValidationError):
        TopologyClone(name="")


def test_topology_update_name_empty_rejected():
    # An explicit empty name on update is still invalid; None (omit) is allowed.
    with pytest.raises(ValidationError):
        TopologyUpdate(name="")
    TopologyUpdate(name=None)


def test_topology_description_over_cap_rejected():
    with pytest.raises(ValidationError):
        TopologyUpdate(name="ok", description="d" * 2001)


def test_connection_port_empty_rejected():
    base = {
        "device_a_id": uuid.uuid4(),
        "device_b_id": uuid.uuid4(),
        "port_a": "",
        "port_b": "Ethernet1",
    }
    with pytest.raises(ValidationError):
        ConnectionCreate(**base)


def test_connection_port_at_cap_accepted():
    ConnectionCreate(
        device_a_id=uuid.uuid4(),
        device_b_id=uuid.uuid4(),
        port_a="p" * 255,
        port_b="Ethernet1",
    )


def test_connection_port_over_cap_rejected():
    with pytest.raises(ValidationError):
        ConnectionCreate(
            device_a_id=uuid.uuid4(),
            device_b_id=uuid.uuid4(),
            port_a="p" * 256,
            port_b="Ethernet1",
        )


def test_connection_notes_over_cap_rejected():
    with pytest.raises(ValidationError):
        ConnectionCreate(
            device_a_id=uuid.uuid4(),
            device_b_id=uuid.uuid4(),
            port_a="Ethernet1",
            port_b="Ethernet2",
            notes="n" * 2001,
        )


# Topology templates take the topology bounds (issue #1005).
@pytest.mark.parametrize("model", [TemplateCreate, TemplateFromTopologyRequest])
def test_template_name_bounds(model):
    with pytest.raises(ValidationError):
        model(name="")
    with pytest.raises(ValidationError):
        model(name="x" * 101)
    model(name="x" * 100)


@pytest.mark.parametrize("model", [TemplateCreate, TemplateFromTopologyRequest, TemplateUpdate])
def test_template_description_bounds(model):
    model(name="ok", description="d" * 2000)
    with pytest.raises(ValidationError):
        model(name="ok", description="d" * 2001)


def test_template_update_name_bounds():
    TemplateUpdate(name=None)
    with pytest.raises(ValidationError):
        TemplateUpdate(name="")
    with pytest.raises(ValidationError):
        TemplateUpdate(name="x" * 101)


def test_instantiate_name_bounds():
    with pytest.raises(ValidationError):
        InstantiateRequest(name="", role_assignments={})
    with pytest.raises(ValidationError):
        InstantiateRequest(name="x" * 101, role_assignments={})
    InstantiateRequest(name="x" * 100, role_assignments={})
