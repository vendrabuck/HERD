"""Unit tests for the e2e preference-PATCH predicate (issue #1070).

Stack-free: they exercise tests.e2e.conftest.pw_prefs_patch_carries with fake
Playwright responses. The nightly leak came from a SUBSTRING predicate: the
race test waited for a PATCH whose raw body contained its shared search token,
and an earlier PATCH saving a longer search that started with that token
satisfied the wait, so the real PATCH landed after the test restored the
baseline. The predicate compares the parsed body by equality instead, and the
source scan below keeps substring tests on a request body out of tests/e2e.
"""

import ast
import json
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


E2E_DIR = Path(__file__).resolve().parents[1] / "e2e"

# Playwright request attributes that hold the RAW body (`post_data_json` is parsed).
_BODY_ATTRS = {"post_data", "post_data_buffer"}
# String methods that keep a raw body raw.
_BODY_PRESERVING_METHODS = {"decode", "lower", "upper", "casefold", "strip", "lstrip", "rstrip"}
# String methods that test for a substring.
_SUBSTRING_METHODS = {
    "find",
    "rfind",
    "index",
    "rindex",
    "count",
    "startswith",
    "endswith",
    "__contains__",
}


def _is_raw_body(expr: ast.expr, tainted: set[str]) -> bool:
    """True when `expr` evaluates to a request body that has not been parsed."""
    if isinstance(expr, ast.Attribute) and expr.attr in _BODY_ATTRS:
        return True
    if isinstance(expr, ast.Name):
        return expr.id in tainted
    if isinstance(expr, ast.NamedExpr):
        return _is_raw_body(expr.value, tainted)
    if isinstance(expr, ast.BoolOp):
        return any(_is_raw_body(v, tainted) for v in expr.values)
    if isinstance(expr, ast.IfExp):
        return _is_raw_body(expr.body, tainted) or _is_raw_body(expr.orelse, tainted)
    if isinstance(expr, ast.Subscript):
        return _is_raw_body(expr.value, tainted)
    if isinstance(expr, ast.Call):
        func = expr.func
        if isinstance(func, ast.Name) and func.id in {"str", "bytes"} and expr.args:
            return _is_raw_body(expr.args[0], tainted)
        if isinstance(func, ast.Attribute) and func.attr in _BODY_PRESERVING_METHODS:
            return _is_raw_body(func.value, tainted)
    return False


def _tainted_names(tree: ast.AST) -> set[str]:
    """Names assigned a raw request body anywhere in the module (to a fixed point)."""
    tainted: set[str] = set()
    while True:
        before = len(tainted)
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and _is_raw_body(node.value, tainted):
                tainted.update(t.id for t in node.targets if isinstance(t, ast.Name))
            elif (
                isinstance(node, ast.AnnAssign | ast.NamedExpr)
                and node.value is not None
                and isinstance(node.target, ast.Name)
                and _is_raw_body(node.value, tainted)
            ):
                tainted.add(node.target.id)
        if len(tainted) == before:
            return tainted


def find_body_substring_tests(source: str) -> list[int]:
    """Line numbers where a raw request body is tested for a substring (issue #1070).

    Flags `x in <body>` and `x not in <body>`, and a substring method
    (`find`, `index`, `count`, `startswith`, `endswith`, `__contains__`) called on
    the body, where the body is a `.post_data` read directly, through `or ""`,
    `str(...)`, `.decode()` or a case change, or through a name assigned one of
    those on an earlier line (issue #1145).
    """
    tree = ast.parse(source)
    tainted = _tainted_names(tree)
    hits: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            for op, right in zip(node.ops, node.comparators, strict=True):
                if isinstance(op, ast.In | ast.NotIn) and _is_raw_body(right, tainted):
                    hits.add(node.lineno)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in _SUBSTRING_METHODS
            and _is_raw_body(node.func.value, tainted)
        ):
            hits.add(node.lineno)
    return sorted(hits)


def test_detector_flags_every_spelling_of_a_body_substring_test():
    src = (
        "def historic(request, shared):\n"
        '    return request.method == "PATCH" and shared in (request.post_data or "")\n'
        "def two_lines(request, shared):\n"
        '    body = request.post_data or ""\n'
        "    if shared in body:\n"
        "        return True\n"
        "def stringified(request, x):\n"
        "    return x in str(request.post_data)\n"
        "def found(request, x):\n"
        "    return request.post_data.find(x) >= 0\n"
        "def prefix(request, x):\n"
        "    return (request.post_data or '').startswith(x)\n"
        "def negated(request, x):\n"
        "    return x not in request.post_data\n"
        "def chained(response, x):\n"
        "    raw = response.request.post_data_buffer\n"
        "    text = raw.decode()\n"
        "    return text.lower().endswith(x)\n"
    )
    assert find_body_substring_tests(src) == [2, 5, 8, 10, 12, 14, 18]


def test_detector_ignores_a_parsed_body():
    src = (
        "def parsed(request, shared):\n"
        '    body = json.loads(request.post_data or "{}")\n'
        '    return shared in body.get("saved_filters", {})\n'
        "def parsed_json(request, key):\n"
        "    return key in (request.post_data_json or {})\n"
        "def equality(request, raw):\n"
        "    return request.post_data == raw\n"
        "def other_text(text, x):\n"
        "    return x in text and text.startswith(x)\n"
    )
    assert find_body_substring_tests(src) == []


def body_substring_offenders(e2e_dir: Path) -> list[str]:
    """Every flagged line under `e2e_dir`, subdirectories included."""
    offenders = []
    for path in sorted(e2e_dir.rglob("*.py")):
        source = path.read_text()
        lines = source.splitlines()
        for lineno in find_body_substring_tests(source):
            offenders.append(f"{path.relative_to(e2e_dir)}:{lineno}: {lines[lineno - 1].strip()}")
    return offenders


def test_scan_reaches_nested_e2e_directories(tmp_path):
    nested = tmp_path / "pages" / "admin"
    nested.mkdir(parents=True)
    (nested / "test_nested.py").write_text(
        "def wait(request, x):\n    return x in (request.post_data or '')\n"
    )
    assert body_substring_offenders(tmp_path) == [
        "pages/admin/test_nested.py:2: return x in (request.post_data or '')"
    ]


def test_no_e2e_test_matches_a_request_body_by_substring():
    assert list(E2E_DIR.rglob("*.py")), f"scan found no e2e files under {E2E_DIR}"
    offenders = body_substring_offenders(E2E_DIR)
    assert offenders == [], (
        "match a request body by parsed equality (pw_prefs_patch_carries), not by substring:\n"
        + "\n".join(offenders)
    )
