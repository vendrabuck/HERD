"""Config's settings-save check costs about one parse, at any depth.

The check for a lone surrogate in a value this service parses itself was a walk that
built a location tuple for every element, so its memory grew with elements times
nesting depth: a deep and wide document cost gigabytes and seconds (review of #1456).
It is now `herd_common.validation_errors.holds_lone_surrogate`, one serialization in
C. Each test bounds the check by the parse it follows, peak memory and time (here the check
is one serialization of the parsed values; the bound is 5 times the parse, as for the
other callers, whose check includes a parse too), which holds pinned to one core, on a
fast host, and under load.
"""

from __future__ import annotations

import json
import time
import tracemalloc

import pytest
from app.validation_errors import holds_lone_surrogate

DEPTH = 300
WIDTH = 100_000


def _deep(leaf: bytes) -> bytes:
    """A list nested DEPTH deep whose innermost list holds WIDTH zeros, then `leaf`."""
    return b"[" * DEPTH + b"0," * WIDTH + leaf + b"]" * DEPTH


def _peak(fn) -> int:
    tracemalloc.start()
    try:
        fn()
        return tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


def _best_time(fn, runs: int = 3) -> float:
    best = float("inf")
    for _ in range(runs):
        start = time.perf_counter()
        fn()
        best = min(best, time.perf_counter() - start)
    return best


@pytest.mark.parametrize(
    ("leaf", "expected"), [(b'"\\ud83d\\ude00"', False), (b'"\\ud800"', True)], ids=["pair", "lone"]
)
def test_the_save_check_on_deep_and_wide_values_costs_about_one_parse(leaf, expected):
    """`update_settings` calls this on the request's `values` before anything else."""
    raw = b'{"LOG_LEVEL": ' + _deep(leaf) + b"}"
    values = json.loads(raw)
    assert holds_lone_surrogate(values) is expected

    def check():
        return holds_lone_surrogate(values)

    def parse():
        return json.loads(raw)

    assert _peak(check) < 4 * _peak(parse)
    assert _best_time(check) < 5 * _best_time(parse)
