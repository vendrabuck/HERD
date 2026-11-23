"""`call_profile` in `tests/_cost.py` counts what a call does without a clock (#1512).

The body-scan cost tests compare the scan with a frozen budget by these counts, so a
profile that misses an event would let a costlier scan pass. These pin what it counts,
what it leaves out, that an empty profile fails instead of passing, and that it leaves
the interpreter's hooks (coverage's among them) as it found them. They run in the same
pytest process as the cost tests, under `--cov` in CI.
"""

from __future__ import annotations

import gc
import re
import sys
import threading
from collections import Counter

import pytest

from tests._cost import _TOOL_IDS, _once, call_profile

_COMMA = re.compile(rb",")


def _passes(body: bytes) -> int:
    if b"\\\\" in body:
        body = body.replace(b"\\\\", b"_")
    return sum(body.count(unit) for unit in (b"a", b"b")) + len(_COMMA.findall(body))


def _one_line_loop(body: bytes) -> int:
    return sum([1 for byte in body if byte == 92])


def _through_another_file(value: object) -> str:
    import json

    return json.dumps(value)


def _count_a(body: bytes) -> int:
    return body.count(b"a")


def _in_another_thread(body: bytes) -> None:
    worker = threading.Thread(target=_count_a, args=(body,))
    worker.start()
    worker.join()


def test_it_counts_c_calls_by_callable_and_python_functions_by_name():
    profile = call_profile(_passes, b"a\\\\b,a,")
    assert profile.c_calls == Counter(
        {"bytes.replace": 1, "sum": 1, "bytes.count": 2, "len": 1, "Pattern.findall": 1}
    )
    assert profile.python_calls == Counter({"_passes": 1, "<genexpr>": 1})
    assert profile.steps > 0
    assert profile.call_events == 7  # the six C calls and the generator expression's
    assert profile.patterns == frozenset({(b",", 0)})


def _bound_search(body: bytes) -> object:
    search = re.compile(rb"\\u", re.IGNORECASE).search
    return search(body)


def test_a_pattern_called_through_a_bound_method_is_recorded_with_its_flags():
    profile = call_profile(_bound_search, b"\\u")
    assert profile.patterns == frozenset({(rb"\\u", re.IGNORECASE)})


def _through_this_directory() -> float:
    return _once(lambda: None)


def test_code_in_another_module_of_the_same_directory_is_counted_as_its_own():
    """`_once` lives in `tests/_cost.py`, beside this file: a helper moved within the
    package is counted exactly as before the move, its C calls and its steps."""
    profile = call_profile(_through_this_directory)
    assert profile.c_calls == Counter({"process_time": 2})
    assert profile.python_calls == Counter(
        {"_through_this_directory": 1, "_once": 1, "<lambda>": 1}
    )


def test_operators_are_not_calls():
    """`in` and `==` are bytecode: a body without a backslash pair makes one call fewer
    (no `replace`), and the `in` search it still runs is not counted either way."""
    profile = call_profile(_passes, b"ab,")
    assert "bytes.replace" not in profile.c_calls
    assert sum(profile.c_calls.values()) == 5


def test_a_loop_on_one_line_grows_the_steps_with_the_input():
    """LINE fires once for a one-line comprehension; its JUMP per item is what grows."""
    small = call_profile(_one_line_loop, b"\\a" * 1000)
    large = call_profile(_one_line_loop, b"\\a" * 4000)
    assert large.steps - small.steps >= 3 * 2000, (small.steps, large.steps)
    assert small.c_calls == large.c_calls == Counter({"sum": 1})


def test_code_outside_the_directory_is_reached_through_a_counted_python_call():
    """`json` is outside this directory: its C calls and steps are not counted, the
    call into it is (a Python call, and a CALL event)."""
    profile = call_profile(_through_another_file, {"a": 1})
    assert profile.c_calls == Counter()
    assert profile.python_calls["dumps"] == 1
    assert profile.call_events == 1


def test_calls_in_another_thread_are_not_counted():
    """The thread runs a function of this file; only the thread's creation counts."""
    profile = call_profile(_in_another_thread, b"aaa")
    assert "bytes.count" not in profile.c_calls
    assert "_count_a" not in profile.python_calls
    assert profile.c_calls["Thread"] == 1


def test_a_call_that_never_runs_the_function_fails_instead_of_returning_nothing():
    """A generator function's call only creates the generator: no event of its code."""

    def never_started():
        yield b"a".count(b"a")

    with pytest.raises(AssertionError, match="saw no start of"):
        call_profile(never_started)


def test_it_restores_every_hook_and_frees_its_tool_id():
    monitoring = sys.monitoring
    before = (
        sys.gettrace(),
        sys.getprofile(),
        [monitoring.get_tool(tid) for tid in range(6)],
    )
    call_profile(_passes, b"a,b")
    with pytest.raises(ZeroDivisionError):
        call_profile(lambda: 1 / 0)
    after = (
        sys.gettrace(),
        sys.getprofile(),
        [monitoring.get_tool(tid) for tid in range(6)],
    )
    assert after == before


def test_the_garbage_collector_is_off_during_the_call_and_left_as_it_was():
    seen = []
    was_enabled = gc.isenabled()
    try:
        gc.enable()
        call_profile(lambda: seen.append(gc.isenabled()))
        assert gc.isenabled() is True
        gc.disable()
        call_profile(lambda: seen.append(gc.isenabled()))
        assert gc.isenabled() is False
    finally:
        if was_enabled:
            gc.enable()
    assert seen == [False, False]


def test_a_failing_hook_setup_leaves_the_collector_on_and_the_tool_id_free(monkeypatch):
    real = sys.monitoring.set_events

    def refuse(tool: int, mask: int) -> None:
        if mask:
            raise ValueError("set_events refused")
        real(tool, mask)

    was_enabled = gc.isenabled()
    gc.enable()
    try:
        tools = [sys.monitoring.get_tool(tid) for tid in range(6)]
        monkeypatch.setattr(sys.monitoring, "set_events", refuse)
        with pytest.raises(ValueError, match="set_events refused"):
            call_profile(_passes, b"a")
        monkeypatch.undo()
        assert gc.isenabled() is True
        assert [sys.monitoring.get_tool(tid) for tid in range(6)] == tools
    finally:
        if not was_enabled:
            gc.disable()


def test_with_every_tool_id_taken_it_raises():
    monitoring = sys.monitoring
    claimed = [tid for tid in _TOOL_IDS if monitoring.get_tool(tid) is None]
    for tid in claimed:
        monitoring.use_tool_id(tid, "test holder")
    try:
        with pytest.raises(RuntimeError, match="all taken"):
            call_profile(_passes, b"a")
    finally:
        for tid in claimed:
            monitoring.free_tool_id(tid)
