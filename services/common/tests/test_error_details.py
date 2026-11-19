"""herd_common.error_details: a refused value is named by its field and the allowed
values, never echoed (issues #1458, #1459)."""

from __future__ import annotations

from herd_common.error_details import unknown_value_detail


def test_the_detail_names_the_field_and_the_allowed_values_in_order():
    assert unknown_value_detail("purpose_category", ["qa_regression", "other"]) == (
        "Unknown purpose_category; allowed: qa_regression, other"
    )


def test_the_allowed_values_may_be_any_iterable():
    assert unknown_value_detail("last_status", (s for s in ("UNKNOWN", "HEALTHY"))) == (
        "Unknown last_status; allowed: UNKNOWN, HEALTHY"
    )


def test_an_empty_allowed_set_still_names_the_field():
    assert unknown_value_detail("x", []) == "Unknown x; allowed: "
