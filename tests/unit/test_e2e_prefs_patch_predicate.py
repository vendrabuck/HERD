"""Unit tests for the e2e preference-PATCH predicate (issue #1070).

Stack-free: they exercise tests.e2e.conftest.pw_prefs_patch_carries with fake
Playwright responses. The nightly leak came from a SUBSTRING predicate: the
race test waited for a PATCH whose raw body contained its shared search token,
and an earlier PATCH saving a longer search that started with that token
satisfied the wait, so the real PATCH landed after the test restored the
baseline. The predicate compares the parsed body by equality instead, and the
source scan below keeps substring tests on a request body out of tests/e2e.
"""

import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.e2e.conftest import pw_prefs_patch_carries

PREFS_URL = "https://localhost/api/user-profile/preferences"


def _response(body, *, method="PATCH", url=PREFS_URL, raw=None):
    post_data = raw if raw is not None else json.dumps(body)
    return SimpleNamespace(url=url, request=SimpleNamespace(method=method, post_data=post_data))


def _inventory(filter_value):
    return _response({"saved_filters": {"inventory": filter_value}})


def test_whole_filter_matches_only_the_exact_object():
    expected = {"search": "", "status": "AVAILABLE"}
    assert pw_prefs_patch_carries(_inventory(expected), "inventory", expected)
    assert not pw_prefs_patch_carries(_inventory({"search": ""}), "inventory", expected)
    assert not pw_prefs_patch_carries(
        _inventory({**expected, "template_id": "t"}), "inventory", expected
    )


def test_key_mode_matches_the_exact_search_not_a_longer_one_sharing_its_prefix():
    shared = "e2e-pw-inv-race-a4b352910c-"
    longer = _inventory({"search": shared + "a"})
    # The #1070 case: the old substring test said True here.
    assert shared in longer.request.post_data
    assert not pw_prefs_patch_carries(longer, "inventory", shared, key="search")
    assert pw_prefs_patch_carries(_inventory({"search": shared}), "inventory", shared, key="search")


def test_key_mode_ignores_other_keys_of_the_filter():
    resp = _inventory({"search": "x", "status": "MAINTENANCE"})
    assert pw_prefs_patch_carries(resp, "inventory", "MAINTENANCE", key="status")
    assert pw_prefs_patch_carries(resp, "inventory", "x", key="search")


@pytest.mark.parametrize(
    "resp",
    [
        _inventory("not-a-dict"),
        _inventory(None),
        _response({"saved_filters": {}}),
        _response({"extras": {"sort:inventory": None}}),
        _response({}),
    ],
)
def test_key_mode_is_false_without_a_filter_object(resp):
    assert not pw_prefs_patch_carries(resp, "inventory", "x", key="search")


def test_page_key_selects_the_page():
    resp = _response({"saved_filters": {"reservations": {"search": "t"}}})
    assert pw_prefs_patch_carries(resp, "reservations", {"search": "t"})
    assert not pw_prefs_patch_carries(resp, "inventory", {"search": "t"})


def test_only_a_preferences_patch_counts():
    body = {"saved_filters": {"inventory": {"search": "t"}}}
    assert not pw_prefs_patch_carries(_response(body, method="GET"), "inventory", {"search": "t"})
    assert not pw_prefs_patch_carries(
        _response(body, url="https://localhost/api/inventory/devices"),
        "inventory",
        {"search": "t"},
    )


@pytest.mark.parametrize("raw", ["", "{not json"])
def test_empty_or_malformed_body_is_false(raw):
    resp = _response(None, raw=raw)
    assert not pw_prefs_patch_carries(resp, "inventory", {"search": "t"})
    assert not pw_prefs_patch_carries(resp, "inventory", "t", key="search")


# A membership test whose right-hand side is a request body, for example
# `shared in (request.post_data or "")` or `x in request.post_data`.
_SUBSTRING_ON_BODY = re.compile(r"\bin\s*\(?\s*[\w.]*post_data\b")


def test_no_e2e_test_matches_a_request_body_by_substring():
    e2e_dir = Path(__file__).resolve().parents[1] / "e2e"
    offenders = []
    for path in sorted(e2e_dir.glob("*.py")):
        for lineno, line in enumerate(path.read_text().splitlines(), start=1):
            if _SUBSTRING_ON_BODY.search(line):
                offenders.append(f"{path.name}:{lineno}: {line.strip()}")
    assert offenders == [], (
        "match a request body by parsed equality (pw_prefs_patch_carries), not by substring:\n"
        + "\n".join(offenders)
    )
