"""The one clock every herd_common cost test reads (issue #1477).

A cost test proves that a piece of code does a bounded, linear amount of work on input
a caller controls. It must pass on a fast idle host, on a two-core shared CI runner
running four suites at once, pinned to one core (`taskset -c 0`), under coverage, and
with a CPU hog on the same core. Wall-clock time cannot do that: under a shared core a
short call fits one scheduler slice and runs at full speed while a long one is
descheduled for part of every run, so a wall-clock ratio of a long call to a short one
grows with the number of competing processes (#1464 saw 9.7 to 11.9 against a linear
4 with two CPU hogs; #1477 saw a scan at 18.5 times `json.loads` against a measured
2 to 8). So every timing here is CPU time of this process, `time.process_time()`,
which does not advance while the process waits for a core, and each one keeps the
fastest of several calls, which drops a page fault or a cache flush in one of them.

Two shapes of bound, both relative, never a threshold in seconds:

- `growth_ratios`: the cost at sizes n, kn, k*k*n (k = 4). Linear work gives about k at
  each step, quadratic about k*k; a bound of about twice k holds the first and fails
  the second. This is the property a cost test exists for, and it compares the code
  only with itself, so it does not move with the runner's CPU model.
- `cpu_seconds` of the code against `cpu_seconds` of a reference on the same input
  (a parse it saves, one C pass): only when the comparison IS the property, and with
  the margin the test's docstring records from measurements.
"""

from __future__ import annotations

import gc
import time
from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")


def cpu_seconds(fn: Callable[[], object], repeats: int = 5) -> float:
    """The least CPU time of `repeats` calls of `fn`, in seconds."""
    best = float("inf")
    for _ in range(repeats):
        started = time.process_time()
        fn()
        best = min(best, time.process_time() - started)
    return best


def growth_ratios(
    make: Callable[[int], T],
    run: Callable[[T], object],
    *,
    bound: float,
    start: int = 1024,
    factor: int = 4,
    steps: int = 2,
    repeats: int = 3,
    min_base_seconds: float = 0.002,
    max_start: int = 8 << 20,
) -> tuple[int, list[float], list[float]]:
    """How the cost of `run(make(size))` grows as the size is multiplied by `factor`.

    The base size n starts at `start` and doubles until one run costs at least
    `min_base_seconds` of CPU (or n reaches `max_start`), so the base is never lost in
    timer resolution on a fast host; the sizes are then n, factor*n, and so on, `steps`
    times. Each size keeps the fastest of `repeats` runs. When a ratio exceeds `bound`
    every size is measured once more and keeps its fastest time across both attempts,
    so one burst on a shared host cannot fail the caller while a real super-linear cost
    (which exceeds the bound on every attempt) still does. The garbage collector is off
    while timing, so a collection triggered by the input's allocation lands outside.

    Returns (n, times, ratios); the caller asserts `max(ratios) <= bound`.
    """
    gc_was_enabled = gc.isenabled()
    gc.disable()
    try:

        def timed(size: int) -> float:
            value = make(size)
            return cpu_seconds(lambda: run(value), repeats)

        n = start
        while timed(n) < min_base_seconds and n < max_start:
            n *= 2
        sizes = [n * factor**step for step in range(steps + 1)]
        times = [timed(size) for size in sizes]
        if max(_ratios(times)) > bound:
            times = [min(t, timed(size)) for t, size in zip(times, sizes, strict=True)]
    finally:
        if gc_was_enabled:
            gc.enable()
    return n, times, _ratios(times)


def _ratios(times: list[float]) -> list[float]:
    return [later / earlier for earlier, later in zip(times, times[1:], strict=False)]
