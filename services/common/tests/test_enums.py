"""Tests for herd_common.enums."""

import enum

from herd_common.enums import TopologyType


def test_topology_type_physical_value():
    assert TopologyType.PHYSICAL == "PHYSICAL"
    assert TopologyType.PHYSICAL.value == "PHYSICAL"


def test_topology_type_cloud_value():
    assert TopologyType.CLOUD == "CLOUD"
    assert TopologyType.CLOUD.value == "CLOUD"


def test_topology_type_is_str_enum():
    assert isinstance(TopologyType.PHYSICAL, str)
    assert isinstance(TopologyType.CLOUD, str)
    assert issubclass(TopologyType, enum.Enum)


def test_topology_type_members():
    members = list(TopologyType)
    assert len(members) == 2
    assert TopologyType.PHYSICAL in members
    assert TopologyType.CLOUD in members
