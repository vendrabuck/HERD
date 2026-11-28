"""Unit tests for herd_common.timeutil.to_utc_instant (issue #1595).

The one naive-or-aware to UTC conversion. The table pins the whole input space
the three former copies (reservations' schema module, reservations' reporting
service, notifications' end-time label) were each written against.
"""

import os
import time
from datetime import UTC, date, datetime, timedelta, timezone

import pytest
from herd_common.timeutil import to_utc_instant

IST = timezone(timedelta(hours=5, minutes=30))
PST = timezone(timedelta(hours=-8))
NEAR_MIN = datetime(1, 1, 1, 0, 0, tzinfo=timezone(timedelta(hours=5)))
NEAR_MAX = datetime(9999, 12, 31, 23, 0, tzinfo=timezone(timedelta(hours=-5)))


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        pytest.param(
            datetime(2031, 1, 1, 12, 30),
            datetime(2031, 1, 1, 12, 30, tzinfo=UTC),
            id="naive-is-utc",
        ),
        pytest.param(
            datetime(2031, 1, 1, 12, 30, 15, 123456),
            datetime(2031, 1, 1, 12, 30, 15, 123456, tzinfo=UTC),
            id="naive-keeps-microseconds",
        ),
        pytest.param(
            datetime(2031, 1, 1, 12, 30, tzinfo=UTC),
            datetime(2031, 1, 1, 12, 30, tzinfo=UTC),
            id="aware-utc-unchanged",
        ),
        pytest.param(
            datetime(2031, 1, 1, 12, 30, tzinfo=IST),
            datetime(2031, 1, 1, 7, 0, tzinfo=UTC),
            id="plus-0530-converted",
        ),
        pytest.param(
            datetime(2031, 1, 1, 12, 30, tzinfo=PST),
            datetime(2031, 1, 1, 20, 30, tzinfo=UTC),
            id="minus-0800-converted",
        ),
        pytest.param(
            datetime(2031, 1, 1, 1, 0, tzinfo=IST),
            datetime(2030, 12, 31, 19, 30, tzinfo=UTC),
            id="conversion-crosses-the-date-line-backward",
        ),
        pytest.param(
            datetime(2031, 12, 31, 20, 0, tzinfo=PST),
            datetime(2032, 1, 1, 4, 0, tzinfo=UTC),
            id="conversion-crosses-the-date-line-forward",
        ),
        pytest.param(datetime.min, datetime.min.replace(tzinfo=UTC), id="naive-min"),
        pytest.param(datetime.max, datetime.max.replace(tzinfo=UTC), id="naive-max"),
        pytest.param(
            datetime.min.replace(tzinfo=UTC), datetime.min.replace(tzinfo=UTC), id="aware-utc-min"
        ),
        pytest.param(
            datetime.max.replace(tzinfo=UTC), datetime.max.replace(tzinfo=UTC), id="aware-utc-max"
        ),
        pytest.param(
            datetime(1, 1, 1, 5, 0, tzinfo=timezone(timedelta(hours=5))),
            datetime(1, 1, 1, 0, 0, tzinfo=UTC),
            id="offset-that-lands-exactly-on-the-minimum",
        ),
    ],
)
def test_returns_the_aware_utc_instant(given, expected):
    out = to_utc_instant(given)
    assert out == expected
    assert out.utcoffset() == timedelta(0)
    # Same wall fields, not just an equal instant: a naive input is relabelled, not shifted.
    assert out.replace(tzinfo=None) == expected.replace(tzinfo=None)


def test_a_naive_value_is_relabelled_not_shifted():
    naive = datetime(2031, 6, 1, 9, 15)
    assert to_utc_instant(naive).replace(tzinfo=None) == naive


def test_an_aware_value_is_the_same_instant():
    for aware in (datetime(2031, 6, 1, 9, 15, tzinfo=IST), datetime(2031, 6, 1, 9, 15, tzinfo=PST)):
        assert to_utc_instant(aware) == aware


@pytest.mark.parametrize("value", [NEAR_MIN, NEAR_MAX], ids=["before-year-1", "after-year-9999"])
def test_an_instant_with_no_utc_equivalent_raises_overflow(value):
    with pytest.raises(OverflowError):
        to_utc_instant(value)


@pytest.mark.parametrize(
    "value",
    [None, date(2031, 1, 1), "2031-01-01T00:00:00Z", 1_900_000_000],
    ids=["none", "date-not-datetime", "string", "int"],
)
def test_a_non_datetime_raises_attribute_error(value):
    with pytest.raises(AttributeError):
        to_utc_instant(value)


@pytest.mark.parametrize("zone", ["UTC0", "PST8PDT", "Asia/Kolkata", "Pacific/Kiritimati"])
def test_the_result_never_depends_on_the_process_zone(zone):
    naive = datetime(2031, 7, 4, 3, 2, 1)
    aware = datetime(2031, 7, 4, 3, 2, 1, tzinfo=IST)
    saved = os.environ.get("TZ")
    try:
        os.environ["TZ"] = zone
        time.tzset()
        assert to_utc_instant(naive) == datetime(2031, 7, 4, 3, 2, 1, tzinfo=UTC)
        assert to_utc_instant(aware) == datetime(2031, 7, 3, 21, 32, 1, tzinfo=UTC)
    finally:
        if saved is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = saved
        time.tzset()
