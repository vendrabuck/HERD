"""Masking cost is linear in the text's length (issue #1464).

The mask runs synchronously inside async handlers on text a caller or a device
controls (execution masks `method_kwargs` before validating them; the assistant
masks driver output), so a super-linear pattern is a denial of service: before
the URL pattern's quantifiers were bounded, `" " + "a-" * n` took 11 s at 64 KB.

Each case times redact_config at three sizes, n, 4n, and 16n, and requires each
step to cost at most RATIO_BOUND times the one before: linear work gives about 4
(at most 4.7 in 160 measured runs on the development host), quadratic about 16.
The bound is RELATIVE, so the test holds on a slow, busy, or single-core host (it
must pass under `taskset -c 0`); n is first grown until one call takes at least
MIN_BASE_SECONDS, each size keeps the fastest of REPEATS calls, and a case over
the bound is measured once more and keeps each size's fastest time across both
attempts, so a burst of load on a shared host cannot fail it while a real
quadratic (which fails every attempt) still does. The URL-shaped cases carry a
`://`, because the URL pattern only runs on a line that has one.
"""

import gc
import json
import time

import pytest
from herd_common.config_redaction import redact_config

RATIO_BOUND = 8.0
MIN_BASE_SECONDS = 0.005
REPEATS = 3

SHAPES = {
    "hyphen word run": lambda n: " " + "a-" * (n // 2),
    "dotted word run": lambda n: " " + "a." * (n // 2),
    "boundary character run": lambda n: '"{[(,;?&<>' * (n // 10),
    "key= repeated": lambda n: "key= " * (n // 5),
    "double quote run": lambda n: '"' * n,
    "single quote run": lambda n: "'" * n,
    "keyword glued run": lambda n: "password" * (n // 8),
    "one long line of words": lambda n: "abc " * (n // 4),
    "scheme separator run": lambda n: "://" * (n // 3),
    "url-like run": lambda n: "a://" * (n // 4),
    "hyphen run after a scheme marker": lambda n: "x:// " + "a-" * (n // 2),
    "dotted run after a scheme marker": lambda n: "x:// " + "a." * (n // 2),
    "user name run after a scheme": lambda n: "http://" + "u" * n,
    "escaped newline run": lambda n: "\\n" * (n // 2),
    "escaped quote keyword run": lambda n: '\\"password\\": \\"x\\" ' * (n // 22),
    "xml element run": lambda n: "<password>x</password>" * (n // 22),
    "python repr run": lambda n: "'password' " * (n // 11),
    "json string of lines": lambda n: json.dumps("password x\n" * (n // 11)),
    "lines with keywords": lambda n: "password x\n" * (n // 11),
}


def _seconds(text: str) -> float:
    # CPU time, not wall-clock: under a shared core a short call fits one scheduler
    # slice and runs at full speed while a long one is slowed k-fold, so a wall-clock
    # ratio grows with the number of competing processes on every attempt (seen with
    # two CPU hogs: ratios of 9.7 to 11.9). process_time counts only this process.
    best = float("inf")
    for _ in range(REPEATS):
        started = time.process_time()
        redact_config(text)
        best = min(best, time.process_time() - started)
    return best


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_masking_cost_grows_linearly(shape):
    make = SHAPES[shape]
    gc_was_enabled = gc.isenabled()
    gc.disable()
    try:
        n = 1024
        while _seconds(make(n)) < MIN_BASE_SECONDS and n < 8 * 1024 * 1024:
            n *= 2
        sizes = (n, 4 * n, 16 * n)
        times = [_seconds(make(size)) for size in sizes]
        if max(times[1] / times[0], times[2] / times[1]) > RATIO_BOUND:
            times = [min(t, _seconds(make(size))) for t, size in zip(times, sizes)]
    finally:
        if gc_was_enabled:
            gc.enable()
    ratios = [times[1] / times[0], times[2] / times[1]]
    assert max(ratios) <= RATIO_BOUND, (shape, n, times, ratios)
