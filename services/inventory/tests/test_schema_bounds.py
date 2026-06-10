"""Length-bound tests for inventory device-group schemas (#129, #130).

Pure Pydantic Field constraints; constructed directly with no DB or HTTP. Pins
the boundary (cap+1 rejected, cap accepted).
"""

import uuid

import pytest
from app.schemas.device_group import (
    BulkDeviceIds,
    BulkUserGroupIds,
    DeviceGroupCreate,
)
from app.schemas.port import PortCreate, PortUpdate
from pydantic import ValidationError


def test_bulk_device_ids_at_cap_accepted():
    BulkDeviceIds(device_ids=[uuid.uuid4() for _ in range(500)])


def test_bulk_device_ids_over_cap_rejected():
    with pytest.raises(ValidationError):
        BulkDeviceIds(device_ids=[uuid.uuid4() for _ in range(501)])


def test_bulk_user_group_ids_over_cap_rejected():
    with pytest.raises(ValidationError):
        BulkUserGroupIds(user_group_ids=[uuid.uuid4() for _ in range(501)])


def test_device_group_description_over_cap_rejected():
    with pytest.raises(ValidationError):
        DeviceGroupCreate(name="rack-1", description="d" * 2001)


def test_port_name_empty_rejected():
    # The source fix: a port should never be creatable with no name, so the
    # connection layer never faces an empty port name (#130 follow-up).
    with pytest.raises(ValidationError):
        PortCreate(name="", template_id=uuid.uuid4())


def test_port_name_at_cap_accepted():
    PortCreate(name="x" * 255, template_id=uuid.uuid4())


def test_port_name_over_cap_rejected():
    with pytest.raises(ValidationError):
        PortCreate(name="x" * 256, template_id=uuid.uuid4())


def test_port_update_name_empty_rejected():
    # An explicit empty rename is invalid; omitting name (None) is allowed.
    with pytest.raises(ValidationError):
        PortUpdate(name="")
    PortUpdate(name=None)
