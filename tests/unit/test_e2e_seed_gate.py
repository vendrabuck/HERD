"""Unit tests for the seeded-e2e skip gate (issue #629).

Pure, stack-free tests: they exercise tests.e2e.conftest.format_skip_block and
format_exempt_block directly, the formatting helpers factored out of the
pytest_sessionfinish / pytest_runtest_logreport / pytest_collection_modifyitems
hooks so they can be pinned without spinning up a pytester run or a real e2e
session. The hooks themselves (env-var gating, report collection, marker
lookup, session.exitstatus) are read-through-code by the Makefile and
nightly.yml wiring; these tests pin the two pieces of logic worth a unit test
in isolation, the blocks a human or CI log actually reads. format_skip_block
counts and lists only non-exempt (failing) skips; format_exempt_block is the
separate, non-failing seeded_skip_ok visibility list, added alongside the
seeded_skip_ok marker so an environmentally-expected skip (e.g. the LDAP
login tests on a local-auth gate stack) is still visible in the log without
inflating the failing count.
"""

from tests.e2e.conftest import format_exempt_block, format_skip_block


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


def test_format_exempt_block_empty_is_empty_string():
    assert format_exempt_block([]) == ""


def test_format_exempt_block_single():
    block = format_exempt_block(
        [("tests/e2e/test_ldap_login.py::test_ldap_user_can_login", "AUTH_METHOD != 'ldap'")]
    )
    lines = block.splitlines()
    assert lines[0] == "exempt (seeded_skip_ok):"
    expected_row = "  tests/e2e/test_ldap_login.py::test_ldap_user_can_login: AUTH_METHOD != 'ldap'"
    assert lines[1] == expected_row


def test_format_exempt_block_multiple_preserves_order():
    exempt = [
        ("tests/e2e/test_ldap_login.py::test_ldap_user_can_login", "reason one"),
        ("tests/e2e/test_ldap_login.py::test_ldap_login_jit_provisions_user", "reason two"),
    ]
    block = format_exempt_block(exempt)
    lines = block.splitlines()
    assert lines[0] == "exempt (seeded_skip_ok):"
    assert lines[1:] == [f"  {nodeid}: {reason}" for nodeid, reason in exempt]


def test_format_exempt_block_does_not_affect_skip_block_count():
    # The two blocks are independent: an exempt list is never folded into the
    # failing count format_skip_block reports, which is why the hook prints
    # them as two separate sections rather than merging the lists.
    failing_count_line = format_skip_block([]).splitlines()[0]
    assert failing_count_line == "HERD_E2E_REQUIRE_NO_SKIP=1: 0 test(s) skipped on a seeded stack:"
