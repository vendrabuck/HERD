"""Unit tests for the seeded-e2e skip gate (issue #629).

Pure, stack-free tests: they exercise tests.e2e.conftest.format_skip_block
directly, the formatting helper factored out of the pytest_sessionfinish /
pytest_runtest_logreport hooks so it can be pinned without spinning up a
pytester run or a real e2e session. The hooks themselves (env-var gating,
report collection, session.exitstatus) are read-through-code by the Makefile
and nightly.yml wiring; this test pins the one piece of logic worth a unit
test in isolation, the block a human or CI log actually reads.
"""

from tests.e2e.conftest import format_skip_block


def test_format_skip_block_empty():
    expected = "HERD_E2E_REQUIRE_NO_SKIP=1: 0 test(s) skipped on a seeded stack:"
    assert format_skip_block([]) == expected


def test_format_skip_block_single():
    block = format_skip_block([("tests/e2e/test_fork_live_edit.py::test_x", "no AVAILABLE device")])
    lines = block.splitlines()
    assert lines[0] == "HERD_E2E_REQUIRE_NO_SKIP=1: 1 test(s) skipped on a seeded stack:"
    assert lines[1] == "  tests/e2e/test_fork_live_edit.py::test_x: no AVAILABLE device"


def test_format_skip_block_multiple_preserves_order_and_count():
    skips = [
        ("tests/e2e/test_a.py::test_one", "reason one"),
        ("tests/e2e/test_b.py::test_two", "reason two"),
        ("tests/e2e/test_c.py::test_three", "reason three"),
    ]
    block = format_skip_block(skips)
    lines = block.splitlines()
    assert lines[0] == "HERD_E2E_REQUIRE_NO_SKIP=1: 3 test(s) skipped on a seeded stack:"
    assert lines[1:] == [f"  {nodeid}: {reason}" for nodeid, reason in skips]
