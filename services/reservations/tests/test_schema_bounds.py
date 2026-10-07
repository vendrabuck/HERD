"""Length-bound tests for reservation schemas (#129, #130).

Pure Pydantic constraints; constructed directly with no DB or HTTP. Pins the
device_ids and purpose bounds, and confirms the pre-existing not-empty
device_ids validator still fires alongside the new max_length.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from app.schemas.reservation import DynamicRequestSpec, ReservationCreate, ReservationUpdate
from pydantic import ValidationError

_START = datetime.now(timezone.utc) + timedelta(minutes=1)
_END = _START + timedelta(hours=1)


def _create(**overrides):
    base = {
        "device_ids": [uuid.uuid4()],
        "start_time": _START,
        "end_time": _END,
    }
    base.update(overrides)
    return ReservationCreate(**base)


def test_device_ids_at_cap_accepted():
    _create(device_ids=[uuid.uuid4() for _ in range(200)])


def test_device_ids_over_cap_rejected():
    with pytest.raises(ValidationError):
        _create(device_ids=[uuid.uuid4() for _ in range(201)])


def test_device_ids_empty_still_rejected():
    # The pre-existing not-empty validator must still fire under the new bound.
    with pytest.raises(ValidationError):
        _create(device_ids=[])


def test_purpose_over_cap_rejected():
    with pytest.raises(ValidationError):
        _create(purpose="p" * 2001)


def test_update_device_ids_over_cap_rejected():
    with pytest.raises(ValidationError):
        ReservationUpdate(device_ids=[uuid.uuid4() for _ in range(201)])


def test_update_purpose_over_cap_rejected():
    with pytest.raises(ValidationError):
        ReservationUpdate(purpose="p" * 2001)


def _dynamic(n):
    return [DynamicRequestSpec(template_id=uuid.uuid4()) for _ in range(n)]


def test_dynamic_requests_at_cap_accepted():
    """RES-DYN-2 (issue #998): 50 dynamic requests per reservation."""
    assert len(_create(device_ids=[], dynamic_requests=_dynamic(50)).dynamic_requests) == 50


def test_dynamic_requests_over_cap_rejected():
    with pytest.raises(ValidationError) as exc:
        _create(device_ids=[], dynamic_requests=_dynamic(51))
    errors = exc.value.errors()
    assert [e["loc"] for e in errors] == [("dynamic_requests",)]
    assert errors[0]["type"] == "too_long"
