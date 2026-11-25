"""The live suite's foreign-row comparison holds its tolerance in both directions.

`tests/_prune_marker_snapshot.py` decides which committed changes to a foreign row's
pending fork-prune marker the live suite (test_pending_fork_prune_live_pg.py) accepts
while the gate's sweeper runs. A change it should refuse must be refused and a change
the running sweeper may make must pass (issue #1522).
"""

import pytest

from tests._prune_marker_snapshot import SQL_NULL, foreign_marker_violations, holds_a_list

LIST = '["2d1f6c1e-0d7c-4b5e-9a51-0f3c1b2a4d5e"]'


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (SQL_NULL, False),
        ("null", False),
        ("[]", False),
        ("[ ]", False),
        ("{}", False),
        ("5", False),
        ('""', False),
        (LIST, True),
        ('["\\u0000"]', True),
    ],
)
def test_holds_a_list(state, expected):
    assert holds_a_list(state) is expected


@pytest.mark.parametrize(
    ("was", "now"),
    [
        (SQL_NULL, SQL_NULL),
        (LIST, SQL_NULL),
        (LIST, '["other"]'),
        (LIST, "null"),
        ("null", "null"),
        ("null", SQL_NULL),
        ("5", SQL_NULL),
        ("[]", SQL_NULL),
    ],
)
def test_changes_the_running_sweeper_may_make_pass(was, now):
    assert foreign_marker_violations({"r": was}, {"r": now}) == []


@pytest.mark.parametrize(
    ("was", "now"),
    [
        (SQL_NULL, "null"),
        (SQL_NULL, LIST),
        (SQL_NULL, "[]"),
        ("null", "[]"),
        ("null", LIST),
        ("5", "null"),
    ],
)
def test_a_changed_null_or_empty_row_fails(was, now):
    assert foreign_marker_violations({"r": was}, {"r": now}) == [f"r: {was!r} became {now!r}"]


def test_rows_created_or_deleted_during_the_test_are_ignored():
    before = {"kept": SQL_NULL, "deleted": SQL_NULL}
    after = {"kept": SQL_NULL, "created": "null"}
    assert foreign_marker_violations(before, after) == []
