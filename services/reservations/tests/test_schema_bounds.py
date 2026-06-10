"""Length-bound tests for reservation schemas (#129, #130).

Pure Pydantic constraints; constructed directly with no DB or HTTP. Pins the
device_ids and purpose bounds, and confirms the pre-existing not-empty
device_ids validator still fires alongside the new max_length.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from app.schemas.reservation import ReservationCreate, ReservationUpdate
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
