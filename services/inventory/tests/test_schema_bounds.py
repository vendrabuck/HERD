"""Length-bound tests for inventory device-group schemas (#129, #130, #346).

Pure Pydantic Field constraints; constructed directly with no DB or HTTP. Pins
the boundary (cap+1 rejected, cap accepted).
"""

import uuid

import pytest
from app.schemas.device import DeviceCreate, DeviceUpdate, InternalDeviceCreate
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


# --- Device (#346) ---
# devices.name is a String(255) column (app/models/device.py); the bounds here
# mirror that width the same way PortCreate/PortUpdate mirror ports.name above.


def _device_kwargs(**overrides) -> dict:
    kwargs = {
        "name": "FW-01",
        "template_id": uuid.uuid4(),
        "topology_type": "PHYSICAL",
    }
    kwargs.update(overrides)
    return kwargs


def test_device_create_name_empty_rejected():
    with pytest.raises(ValidationError) as exc:
        DeviceCreate(**_device_kwargs(name=""))
    assert "String should have at least 1 character" in str(exc.value)


def test_device_create_name_at_cap_accepted():
    DeviceCreate(**_device_kwargs(name="x" * 255))


def test_device_create_name_over_cap_rejected():
    with pytest.raises(ValidationError) as exc:
        DeviceCreate(**_device_kwargs(name="x" * 256))
    assert "String should have at most 255 characters" in str(exc.value)


def test_device_update_name_empty_rejected():
    # An explicit empty rename is invalid; omitting name (None) is allowed.
    with pytest.raises(ValidationError):
        DeviceUpdate(name="")
    DeviceUpdate(name=None)


def test_device_update_name_at_cap_accepted():
    DeviceUpdate(name="x" * 255)


def test_device_update_name_over_cap_rejected():
    with pytest.raises(ValidationError):
        DeviceUpdate(name="x" * 256)


def test_internal_device_create_name_none_accepted():
    # Omitted name is valid: inventory server-generates it for dynamic instances.
    InternalDeviceCreate(name=None, template_id=uuid.uuid4(), reservation_id=uuid.uuid4())


def test_internal_device_create_name_empty_rejected():
    # A caller-supplied name still has to be non-empty; None (omitted) is the
    # only way to ask for the server-generated name.
    with pytest.raises(ValidationError):
        InternalDeviceCreate(name="", template_id=uuid.uuid4(), reservation_id=uuid.uuid4())


def test_internal_device_create_name_at_cap_accepted():
    InternalDeviceCreate(name="x" * 255, template_id=uuid.uuid4(), reservation_id=uuid.uuid4())


def test_internal_device_create_name_over_cap_rejected():
    with pytest.raises(ValidationError):
        InternalDeviceCreate(name="x" * 256, template_id=uuid.uuid4(), reservation_id=uuid.uuid4())
