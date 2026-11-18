"""Cabling's topology import check costs about one parse, at any depth.

The check for a lone surrogate in a value this service parses itself was a walk that
built a location tuple for every element, so its memory grew with elements times
nesting depth: a deep and wide document cost gigabytes and seconds (review of #1456).
It is now `herd_common.validation_errors.holds_lone_surrogate`, one serialization in
C. Each test bounds the check by the parse it follows, peak memory and time, which
holds pinned to one core and on a fast host.
"""

from __future__ import annotations

import json
import time
import tracemalloc

import pytest
from app.services.bulk_service import IMPORT_NOT_TEXT_DETAIL, parse_json_topologies
from fastapi import HTTPException

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


@pytest.mark.parametrize("leaf", [b'"\\ud83d\\ude00"', b'"\\ud800"'], ids=["pair", "lone"])
def test_a_deep_and_wide_json_import_file_costs_about_one_parse(leaf):
    raw = b'{"items": ' + _deep(leaf) + b"}"

    def check():
        try:
            parse_json_topologies(raw)
        except HTTPException as exc:
            assert exc.detail == IMPORT_NOT_TEXT_DETAIL

    def parse():
        return json.loads(raw)

    assert _peak(check) < 4 * _peak(parse)
    assert _best_time(check) < 3 * _best_time(parse)
