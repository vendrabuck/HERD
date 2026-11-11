"""The JSON upload path's two bounds (issue #1202).

Pretty-printing indents every line by its depth, so the printed text of a nested
document grows with depth times length: a 91.8 KB upload of arrays 900 deep printed to
82.8 million characters (218 MiB), a 301.8 KB one to 272 million (586 MiB). The printing
now stops once it passes the character budget. A document nested past the recursion
limit raised RecursionError in the parse or the printing, which the route did not
catch (a 500); it is now read as text that is not JSON. An honest document prints
exactly as ``json.dumps(indent=2, sort_keys=True)`` printed it.
"""

import asyncio
import json
import sys
import tracemalloc

import pytest
from app import config as config_module
from app.services import extractor
from app.services.extractor import _extract_json

pytestmark = pytest.mark.timeout(60)

_BUDGET = 80_000


def _extract(data: bytes, budget: int = _BUDGET):
    async def run():
        return await extractor.extract_files([("data.json", data)])

    original = config_module.settings.upload_max_extracted_chars
    config_module.settings.upload_max_extracted_chars = budget
    try:
        (result,) = asyncio.run(run())
    finally:
        config_module.settings.upload_max_extracted_chars = original
    return result


def _nested(depth: int, items: int) -> bytes:
    return b"[" * depth + b"0," * items + b"0" + b"]" * depth


@pytest.mark.parametrize(
    "items", [45_000, 150_000], ids=["91.8-kb-to-82.8m-chars", "301.8-kb-to-272m-chars"]
)
def test_nested_arrays_print_at_most_the_budget_with_bounded_memory(items):
    data = _nested(900, items)
    tracemalloc.start()
    try:
        text = _extract_json(data, _BUDGET)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    # One printed piece past the budget at most: a newline and 900 levels of indent.
    assert _BUDGET < len(text) <= _BUDGET + 2 * 900 + 2
    # The parsed document plus the budget; the full printing was 218 and 586 MiB.
    assert peak < 32 * 1024 * 1024
    # What it printed is the start of the full printing: the full printing of the same
    # depth with ten items shares its first 810,000 characters of opening brackets.
    shorter = json.dumps(json.loads(_nested(900, 10)), indent=2, sort_keys=True)
    assert len(shorter) > len(text)
    assert text == shorter[: len(text)]

    result = _extract(data)
    assert len(result.text) == _BUDGET
    assert result.truncated is True


def test_nesting_past_the_parse_limit_is_returned_as_raw_text():
    data = b"[" * 100_000 + b"]" * 100_000
    with pytest.raises(RecursionError):
        json.loads(data)
    assert _extract_json(data, _BUDGET) == data.decode()
    result = _extract(data)
    assert result.text == "[" * _BUDGET
    assert result.truncated is True


def test_recursion_in_the_printing_is_returned_as_raw_text(monkeypatch):
    """A document the parser accepts and the printer recurses on: the printer is
    replaced by one that fails part way, as the recursive printer does at its limit."""

    class _RecursingEncoder(json.JSONEncoder):
        def iterencode(self, o, _one_shot=False):
            yield "[\n"
            raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr(extractor.json, "JSONEncoder", _RecursingEncoder)
    assert _extract_json(b"[[1]]", _BUDGET) == "[[1]]"


def test_the_real_printer_recursing_past_the_limit_is_raw_text():
    """Without the stand-in: a document as deep as the parser allows, printed with the
    recursion limit lowered under its depth, so the real printer raises."""
    depth = 500
    data = b"[" * depth + b"]" * depth
    json.loads(data)
    limit = sys.getrecursionlimit()
    sys.setrecursionlimit(300)
    try:
        assert _extract_json(data, _BUDGET) == data.decode()
    finally:
        sys.setrecursionlimit(limit)


_HONEST = [
    b'{"b": 2, "a": 1, "nested": {"z": [1, 2.5, null, true, false], "y": "text"}}',
    '{"café": "naïve © 中文 \U0001f600"}'.encode(),
    b"[]",
    b"{}",
    b'"a top-level string"',
    b"12345",
    b"[NaN, Infinity, -Infinity, 1e308, -0.0]",
    b'{"deep": ' + b"[" * 50 + b"1" + b"]" * 50 + b"}",
    json.dumps({f"k{i}": list(range(i % 7)) for i in range(2_000)}).encode(),
]


@pytest.mark.parametrize("data", _HONEST, ids=range(len(_HONEST)))
def test_honest_json_prints_exactly_as_before(data):
    before = json.dumps(json.loads(data), indent=2, sort_keys=True)
    assert _extract_json(data, 10**9) == before
    for budget in (len(before), len(before) - 1, 1):
        result = _extract(data, budget)
        assert (result.text, result.truncated) == (before[:budget], len(before) > budget)
