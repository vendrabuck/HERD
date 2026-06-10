"""Length-bound tests for cabling schemas (#129, #130).

Pydantic Field constraints are pure and synchronous, so these exercise the
boundary directly via model construction, no DB or HTTP. Each test pins the
boundary (cap+1 rejected, cap accepted), not just "rejects something huge".
"""

import uuid

import pytest
from app.schemas.connection import ConnectionCreate
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
