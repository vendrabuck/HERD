"""The in-memory record of the last Save and Restart (issue #1433): app.restart_record.

The route-level behavior (what POST /apply records, what GET /apply/last answers, a
request cut off mid-restart) is in test_docker_ctl.py; these pin the record's own state
machine: none, then running, then done, one run at a time, a fresh id per run, and the
time each result was recorded.
"""

from __future__ import annotations

import ast
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import restart_record
from app.restart_record import RESULT_LOST_ERROR, RestartRecord

RESULT = {"restarted": ["auth"], "errors": ["Failed to restart acl: DockerAPIError"]}
# A fixed clock: the record's own time is what these tests pin, never the host's.
FIXED = datetime(2026, 11, 27, 16, 51, 23, 987654, tzinfo=timezone.utc)
FIXED_TEXT = "2026-11-27T16:51:23+00:00"


def fixed_record() -> RestartRecord:
    return RestartRecord(clock=lambda: FIXED)


def test_a_new_record_holds_no_run():
    assert fixed_record().snapshot() == {
        "id": None,
        "state": "none",
        "result": None,
        "finished_at": None,
    }


def test_begin_records_a_running_run_with_a_random_hex_id():
    record = fixed_record()

    run_id = record.begin()

    assert isinstance(run_id, str) and len(run_id) == 32
    assert int(run_id, 16) >= 0
    assert record.snapshot() == {
        "id": run_id,
        "state": "running",
        "result": None,
        "finished_at": None,
    }


def test_begin_while_a_run_is_running_is_refused_and_keeps_that_run():
    record = fixed_record()
    run_id = record.begin()

    assert record.begin() is None
    assert record.snapshot() == {
        "id": run_id,
        "state": "running",
        "result": None,
        "finished_at": None,
    }


def test_finish_records_the_result_and_its_time_and_frees_the_next_run():
    record = fixed_record()
    first = record.begin()

    record.finish(first, RESULT)

    assert record.snapshot() == {
        "id": first,
        "state": "done",
        "result": RESULT,
        "finished_at": FIXED_TEXT,
    }
    second = record.begin()
    assert second is not None and second != first
    assert record.snapshot() == {
        "id": second,
        "state": "running",
        "result": None,
        "finished_at": None,
    }


def test_finished_at_is_the_clock_at_finish_in_utc_with_its_offset():
    times = iter(
        [
            datetime(2026, 11, 27, 8, 0, 5, tzinfo=timezone(timedelta(hours=-8))),
            datetime(2026, 11, 28, 0, 0, 0, tzinfo=timezone.utc),
        ]
    )
    record = RestartRecord(clock=lambda: next(times))

    first = record.begin()
    record.finish(first, RESULT)
    assert record.snapshot()["finished_at"] == "2026-11-27T16:00:05+00:00"

    second = record.begin()
    record.finish(second, RESULT)
    assert record.snapshot()["finished_at"] == "2026-11-28T00:00:00+00:00"


def test_the_default_clock_is_utc_and_parses_back():
    record = RestartRecord()
    run_id = record.begin()
    record.finish(run_id, RESULT)

    text = record.snapshot()["finished_at"]

    parsed = datetime.fromisoformat(text)
    assert text.endswith("+00:00")
    assert parsed.utcoffset() == timedelta(0)
    assert abs(datetime.now(timezone.utc) - parsed) < timedelta(minutes=1)


def test_every_run_gets_a_distinct_id():
    record = fixed_record()
    ids = set()
    for _ in range(50):
        run_id = record.begin()
        record.finish(run_id, RESULT)
        ids.add(run_id)

    assert len(ids) == 50


def test_finish_with_a_stale_id_changes_nothing():
    record = fixed_record()
    first = record.begin()
    record.finish(first, RESULT)
    second = record.begin()

    record.finish(first, {"restarted": [], "errors": ["late"]})

    assert record.snapshot() == {
        "id": second,
        "state": "running",
        "result": None,
        "finished_at": None,
    }


def test_a_second_finish_of_the_same_run_changes_nothing():
    times = iter([FIXED, FIXED + timedelta(hours=1)])
    record = RestartRecord(clock=lambda: next(times))
    run_id = record.begin()
    record.finish(run_id, RESULT)

    record.finish(run_id, {"restarted": [], "errors": ["again"]})

    assert record.snapshot()["result"] == RESULT
    assert record.snapshot()["finished_at"] == FIXED_TEXT


def test_the_snapshot_and_the_stored_result_are_copies():
    record = fixed_record()
    run_id = record.begin()
    given = {"restarted": ["auth"], "errors": []}
    record.finish(run_id, given)

    given["restarted"].append("inventory")
    record.snapshot()["result"]["errors"].append("x")

    assert record.snapshot()["result"] == {"restarted": ["auth"], "errors": []}


def test_the_lost_result_wording():
    assert RESULT_LOST_ERROR == "The restart ended without a result; check service health"


def test_restart_record_imports_only_the_standard_library():
    tree = ast.parse(Path(restart_record.__file__).read_text(encoding="utf-8"))
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            roots.add(node.module.split(".")[0])
    assert roots, "no imports found; the parse is wrong"
    assert roots <= set(sys.stdlib_module_names) | {"__future__"}, roots
