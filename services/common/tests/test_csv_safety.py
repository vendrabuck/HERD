"""Unit tests for the shared CSV formula-injection helper (issue #910).

Covers every trigger character, the leading-whitespace-then-trigger decision,
non-string pass-through, the inverse importer helper, and round-trip identity
for both a neutralized value and a legitimately apostrophe-led one.
"""

import pytest
from herd_common.csv_safety import csv_safe_cell, csv_unsafe_cell

TRIGGER_CHARS = ["=", "+", "-", "@", "\t", "\r"]


@pytest.mark.parametrize("trigger", TRIGGER_CHARS)
def test_leading_trigger_is_neutralized(trigger):
    value = f'{trigger}HYPERLINK("http://evil","click")'
    result = csv_safe_cell(value)
    assert result == "'" + value
    assert result.startswith("'")


@pytest.mark.parametrize("trigger", TRIGGER_CHARS)
def test_leading_space_then_trigger_is_neutralized(trigger):
    """OWASP decision: leading ASCII spaces before a trigger are still
    dangerous (some spreadsheet engines strip them before evaluating), so the
    whole original value (spaces included) gets one quote prefixed."""
    value = f"  {trigger}1+1"
    result = csv_safe_cell(value)
    assert result == "'" + value


def test_plain_text_unchanged():
    assert csv_safe_cell("device-42") == "device-42"


def test_empty_string_unchanged():
    assert csv_safe_cell("") == ""


def test_none_unchanged():
    assert csv_safe_cell(None) is None


@pytest.mark.parametrize("value", [1, 0, -1, 3.14, -0.5])
def test_numbers_pass_through_untouched(value):
    assert csv_safe_cell(value) == value
    assert type(csv_safe_cell(value)) is type(value)


@pytest.mark.parametrize("value", [True, False])
def test_booleans_pass_through_untouched(value):
    assert csv_safe_cell(value) is value


def test_apostrophe_led_text_is_not_a_trigger_and_is_left_alone():
    # An apostrophe is not in the trigger set, so a legitimately
    # apostrophe-led name is never touched on export.
    assert csv_safe_cell("'quoted") == "'quoted"


# Inverse (import-side) helper ------------------------------------------------


@pytest.mark.parametrize("trigger", TRIGGER_CHARS)
def test_unsafe_cell_strips_exactly_one_quote_before_a_trigger(trigger):
    original = f"{trigger}1+1"
    safe = csv_safe_cell(original)
    assert csv_unsafe_cell(safe) == original


def test_unsafe_cell_leaves_apostrophe_led_text_alone():
    # "'quoted" carries no trigger after its leading quote, so the inverse
    # must not strip it: mangling a legitimate apostrophe-led name would be
    # exactly the bug the round-trip rule exists to prevent.
    assert csv_unsafe_cell("'quoted") == "'quoted"


def test_unsafe_cell_leaves_a_bare_string_alone():
    assert csv_unsafe_cell("device-42") == "device-42"


def test_unsafe_cell_non_string_passthrough():
    assert csv_unsafe_cell(None) is None
    assert csv_unsafe_cell(5) == 5


# Round trip -------------------------------------------------------------


@pytest.mark.parametrize("trigger", TRIGGER_CHARS)
def test_round_trip_identity_for_every_trigger(trigger):
    original = f"{trigger}SUM(A1:A9)"
    assert csv_unsafe_cell(csv_safe_cell(original)) == original


@pytest.mark.parametrize("trigger", TRIGGER_CHARS)
def test_round_trip_identity_for_leading_space_then_trigger(trigger):
    original = f"  {trigger}1+1"
    assert csv_unsafe_cell(csv_safe_cell(original)) == original


def test_round_trip_identity_for_quoted_name():
    original = "'quoted"
    assert csv_unsafe_cell(csv_safe_cell(original)) == original


def test_round_trip_identity_for_plain_text():
    original = "ordinary-topology-name"
    assert csv_unsafe_cell(csv_safe_cell(original)) == original
