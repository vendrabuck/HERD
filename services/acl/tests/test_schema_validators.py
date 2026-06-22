"""Field-validator tests for the acl request schemas.

test_schema_bounds.py pins the BatchCheckRequest length cap. This module pins
the resource_type and permission allow-list validators on CheckRequest and
BatchCheckRequest, which are constructed directly (no app, no DB) so the
synchronous validator branches are exercised and asserted on their exact
error wording. GrantCreateRequest's validators are already covered through the
POST /grants 422 paths in test_grants.py; these two schemas are the gap.
"""

import uuid

import pytest
from app.schemas.grant import BatchCheckRequest, CheckRequest
from pydantic import ValidationError


def _check(resource_type: str = "device", permission: str = "view") -> CheckRequest:
    return CheckRequest(
        user_id=uuid.uuid4(),
        resource_type=resource_type,
        resource_id=uuid.uuid4(),
        permission=permission,
    )


def _batch(resource_type: str = "device", permission: str = "view") -> BatchCheckRequest:
    return BatchCheckRequest(
        user_id=uuid.uuid4(),
        resource_type=resource_type,
        resource_ids=[uuid.uuid4()],
        permission=permission,
    )


# --- CheckRequest ---


@pytest.mark.parametrize("resource_type", ["device", "topology", "reservation"])
def test_check_request_accepts_valid_resource_types(resource_type):
    req = _check(resource_type=resource_type)
    assert req.resource_type == resource_type


@pytest.mark.parametrize("permission", ["view", "manage"])
def test_check_request_accepts_valid_permissions(permission):
    req = _check(permission=permission)
    assert req.permission == permission


def test_check_request_rejects_unknown_resource_type():
    with pytest.raises(ValidationError) as exc:
        _check(resource_type="switchport")
    msg = str(exc.value)
    assert "resource_type must be one of" in msg
    # The allow-list is rendered sorted, so the wording is deterministic.
    assert "device, reservation, topology" in msg


def test_check_request_rejects_unknown_permission():
    with pytest.raises(ValidationError) as exc:
        _check(permission="delete")
    msg = str(exc.value)
    assert "permission must be one of" in msg
    assert "manage, view" in msg


# --- BatchCheckRequest ---


@pytest.mark.parametrize("resource_type", ["device", "topology", "reservation"])
def test_batch_check_request_accepts_valid_resource_types(resource_type):
    req = _batch(resource_type=resource_type)
    assert req.resource_type == resource_type


@pytest.mark.parametrize("permission", ["view", "manage"])
def test_batch_check_request_accepts_valid_permissions(permission):
    req = _batch(permission=permission)
    assert req.permission == permission


def test_batch_check_request_rejects_unknown_resource_type():
    with pytest.raises(ValidationError) as exc:
        _batch(resource_type="vlan")
    msg = str(exc.value)
    assert "resource_type must be one of" in msg
    assert "device, reservation, topology" in msg


def test_batch_check_request_rejects_unknown_permission():
    with pytest.raises(ValidationError) as exc:
        _batch(permission="admin")
    msg = str(exc.value)
    assert "permission must be one of" in msg
    assert "manage, view" in msg
