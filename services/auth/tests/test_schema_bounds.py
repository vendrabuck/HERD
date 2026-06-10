"""Length-bound tests for auth group schemas (#129, #130).

Pure Pydantic Field constraints; constructed directly with no DB or HTTP. Pins
the boundary (cap+1 rejected, cap accepted).
"""

import uuid

import pytest
from app.schemas.group import (
    BulkAddMembersRequest,
    BulkRemoveMembersRequest,
    GroupCreateRequest,
)
from pydantic import ValidationError


def test_bulk_add_members_at_cap_accepted():
    BulkAddMembersRequest(user_ids=[uuid.uuid4() for _ in range(500)])


def test_bulk_add_members_over_cap_rejected():
    with pytest.raises(ValidationError):
        BulkAddMembersRequest(user_ids=[uuid.uuid4() for _ in range(501)])


def test_bulk_remove_members_over_cap_rejected():
    with pytest.raises(ValidationError):
        BulkRemoveMembersRequest(user_ids=[uuid.uuid4() for _ in range(501)])


def test_group_description_at_cap_accepted():
    GroupCreateRequest(name="net-eng", description="d" * 2000)


def test_group_description_over_cap_rejected():
    with pytest.raises(ValidationError):
        GroupCreateRequest(name="net-eng", description="d" * 2001)
