"""The one clock every herd_common cost test reads (issue #1477).

A cost test proves that a piece of code does a bounded amount of work on input a caller
controls. It must pass on a fast idle host, on a shared CI runner running several suites
at once, pinned to one core (`taskset -c 0`), under coverage, and beside CPU hogs.
Wall-clock time cannot do that: under a shared core a short call fits one scheduler slice
and runs at full speed while a long one is descheduled for part of every run, so a
wall-clock ratio of a long call to a short one grows with the number of competing
processes (#1464 saw 9.7 to 11.9 against a linear 4 with two CPU hogs; #1477 saw a scan
read 18.5 times `json.loads` where its CPU-time ratio is under 9). So every timing here
is CPU time of this process, `time.process_time()`, which does not advance while the
process waits for a core.

CPU time is not immune to contention: a process on the same core or on its SMT sibling
still evicts caches and shares memory bandwidth, and a memory-bound neighbour on the
sibling doubled the scan's CPU time in the #1477 review. So each measurement keeps the
fastest of several runs, the two sides of a comparison are measured in alternating
rounds (a change in contention lands on both), a comparison over its bound is measured
once more keeping each side's fastest time across both attempts, and every bound carries
a margin over the worst observation.

Two shapes of bound, both relative, never a threshold in seconds:

- `growth_ratios`: the cost at sizes n, 4n, and 16n. A bound of 8 per fourfold step holds
  linear work (about 4) and n log n (about 4.4), and fails a quadratic once, at 16n, its
  quadratic part costs more than twice its linear part (a dominant quadratic measures
  about 16). It does not reliably catch n^1.5 (exactly 8 when dominant, so at the edge),
  a quadratic part smaller than that at 16n, or a cost that turns super-linear only
  above 16n. The base n adapts to the host: it doubles until one run costs at least
  `min_base_seconds`, so a faster CPU measures larger sizes.
- `cpu_ratio`: the code against a reference on the same input (a parse it saves, one C
  pass), which catches a constant-factor slowdown that growth cannot see. Use it only
  when the comparison IS the property, with the bound's margin recorded beside it.
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
        best = min(best, _once(fn))
    return best


def cpu_ratio(
    numerator: Callable[[], object],
    denominator: Callable[[], object],
    *,
    bound: float,
    repeats: int = 5,
) -> float:
    """The CPU time of `numerator` over that of `denominator`, each its fastest call.

    The two are called in alternating rounds, `repeats` rounds. When the ratio exceeds
    `bound`, another `repeats` rounds run and each side keeps its fastest time across
    both attempts, so one burst of contention cannot fail the caller while a real
    slowdown (over the bound on both attempts) still does. The caller asserts
    `ratio < bound`.
    """
    times = _rounds([numerator, denominator], repeats)
    if times[0] / times[1] >= bound:
        times = [min(pair) for pair in zip(times, _rounds([numerator, denominator], repeats))]
    return times[0] / times[1]


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
    """How the CPU cost of `run(make(size))` grows as the size is multiplied by `factor`.

    The base size n starts at `start` and doubles until one run costs at least
    `min_base_seconds` (or n reaches `max_start`); the sizes are then n, factor*n, and
    so on, `steps` times. The inputs are built first, then timed in `repeats` rounds
    that each run every size once, each size keeping its fastest run. When a ratio
    exceeds `bound`, another `repeats` rounds run and each size keeps its fastest time
    across both attempts. The garbage collector is off while timing.

    Returns (n, times, ratios); the caller asserts `max(ratios) <= bound`.
    """
    gc_was_enabled = gc.isenabled()
    gc.disable()
    try:
        n = start
        while n < max_start:
            value = make(n)
            if cpu_seconds(lambda: run(value), repeats) >= min_base_seconds:
                break
            n *= 2
        inputs = [make(n * factor**step) for step in range(steps + 1)]
        calls = [lambda value=value: run(value) for value in inputs]
        times = _rounds(calls, repeats)
        if max(_ratios(times)) > bound:
            times = [min(pair) for pair in zip(times, _rounds(calls, repeats))]
    finally:
        if gc_was_enabled:
            gc.enable()
    return n, times, _ratios(times)


def _once(fn: Callable[[], object]) -> float:
    started = time.process_time()
    fn()
    return time.process_time() - started


def _rounds(calls: list[Callable[[], object]], repeats: int) -> list[float]:
    """Each call's fastest CPU time over `repeats` rounds that run every call once."""
    best = [float("inf")] * len(calls)
    for _ in range(repeats):
        for index, call in enumerate(calls):
            best[index] = min(best[index], _once(call))
    return best


def _ratios(times: list[float]) -> list[float]:
    return [later / earlier for earlier, later in zip(times, times[1:], strict=False)]
