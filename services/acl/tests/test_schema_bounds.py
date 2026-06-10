"""Length-bound test for the acl BatchCheckRequest (#129).

Each resource_id triggers a grant lookup, so an unbounded list forces unbounded
work in one request. Pure Pydantic constraint, constructed directly.
"""

import uuid

import pytest
from app.schemas.grant import BatchCheckRequest
from pydantic import ValidationError


def _batch(n: int) -> BatchCheckRequest:
    return BatchCheckRequest(
        user_id=uuid.uuid4(),
        resource_type="device",
        resource_ids=[uuid.uuid4() for _ in range(n)],
        permission="view",
    )


def test_batch_check_resource_ids_at_cap_accepted():
    _batch(500)


def test_batch_check_resource_ids_over_cap_rejected():
    with pytest.raises(ValidationError):
        _batch(501)
