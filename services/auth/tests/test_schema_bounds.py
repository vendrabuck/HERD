"""Length-bound tests for auth group schemas (#129, #130), plus a schema-shape
pin for the generic Paginated[T] base (#511).

Pure Pydantic Field constraints; constructed directly with no DB or HTTP. Pins
the boundary (cap+1 rejected, cap accepted).
"""

import uuid

import pytest
from app.main import app
from app.schemas.auth import PaginatedUserResponse
from app.schemas.group import (
    BulkAddMembersRequest,
    BulkRemoveMembersRequest,
    GroupCreateRequest,
    PaginatedGroupResponse,
)
from app.schemas.ldap_sync import PaginatedMappingResponse, PaginatedSyncRunResponse
from app.schemas.pagination import Paginated
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


# --- Paginated[T] base (issue #511) ---

# One row per concrete class: its own base-field-plus-extra property set, in
# the exact order OpenAPI must publish it (items, total, skip, limit first,
# any per-class extra last), so a change to Paginated's field order or a new
# undeclared extra field on one of the four fails this test rather than
# silently drifting the contract snapshot.
_PAGINATED_CLASSES = {
    PaginatedUserResponse: ["items", "total", "skip", "limit"],
    PaginatedGroupResponse: ["items", "total", "skip", "limit"],
    PaginatedMappingResponse: ["items", "total", "skip", "limit"],
    PaginatedSyncRunResponse: ["items", "total", "skip", "limit"],
}


@pytest.mark.parametrize("cls", list(_PAGINATED_CLASSES))
def test_paginated_response_is_a_paginated_subclass(cls):
    assert issubclass(cls, Paginated)


@pytest.mark.parametrize("cls,expected_properties", list(_PAGINATED_CLASSES.items()))
def test_paginated_response_openapi_shape_matches_base_plus_extra(cls, expected_properties):
    schemas = app.openapi()["components"]["schemas"]
    schema = schemas[cls.__name__]

    assert list(schema["properties"].keys()) == expected_properties
    # The four base fields are always required; a per-class extra (like
    # MappingCreateResponse's sibling `warning`) is never required here since
    # none of the four currently declares one without a default.
    assert set(schema["required"]) == {"items", "total", "skip", "limit"}
