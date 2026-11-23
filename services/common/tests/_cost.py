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

One measure reads no clock at all (#1512): `call_profile` counts what one call does,
under a `sys.monitoring` tool of its own: the calls of C callables (functions,
methods, types) that code in the profiled function's directory (its package) makes,
by callable, and the regex patterns they run; the Python functions started, by name;
and the Python steps taken there (lines, jumps, and generator resumes). It is exact
and the same on every build measured, so a test can compare code with a frozen budget
of the same C passes by counts, where a CPU-time ratio of two equal costs is noise
around 1.
"""

from __future__ import annotations

import gc
import os
import re
import sys
import threading
import time
import types
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
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


@dataclass(frozen=True)
class CallProfile:
    """What one call did, as `call_profile` counts it."""

    c_calls: Counter[str]
    python_calls: Counter[str]
    steps: int
    call_events: int
    patterns: frozenset[tuple[str | bytes, int]]


# The `sys.monitoring` tool ids `call_profile` may claim, in order: the profiler's, then
# the two CPython leaves unnamed. The debugger's (0), coverage's (1), and the
# optimizer's (5) are left to their owners.
_TOOL_IDS = (2, 3, 4)


def call_profile(fn: Callable[..., object], *args: object) -> CallProfile:
    """Count what `fn(*args)` does in this thread; no clock is read.

    Its home is the directory of `fn`'s own file and every directory below it (for a
    function of `herd_common`, the whole package), so a helper moved to another module
    of the package is counted exactly as before the move. This helper's own code is
    never counted, though it sits in `tests/`.

    - `c_calls`: each call of a callable that is not a Python function or method (a
      builtin function, a method of a builtin type, a type), made by code in its home,
      by `__qualname__`: `bytes.count`, `Pattern.search`, `sum`. A C call made by code
      outside the home is not counted; that code runs only when the home calls it or a
      C function calls back into it, and either way its start is counted below.
    - `python_calls`: each Python function started, in any file, by `co_name`, `fn`
      itself included; a generator counts once, when it first starts.
    - `steps`: the LINE, JUMP, and PY_RESUME events of code in its home. A loop on one
      line fires one LINE event in all but one JUMP per iteration, so any Python loop in
      the home grows `steps` with its number of iterations.
    - `call_events`: every CALL event made by code in its home, Python callees
      included, so a caller can tell that CALL events arrived at all.
    - `patterns`: the (pattern, flags) of each compiled regex whose method code in its
      home called.

    Not seen: operators (`in`, `==`, slicing, `+`), which are bytecode, not calls; the
    work inside one C call (`bytes.count` of a longer substring is still one call); and
    the calls one C function makes to another.

    The events come from a `sys.monitoring` tool id of this helper's own, claimed for
    the call and released after it (its events cleared, its callbacks unregistered).
    The garbage collector is off during the call and is left as it was found, also when
    the call or the hook's own setup raises. Coverage measures through `sys.settrace`
    (its C tracer, the default before Python 3.14) or through its own tool id
    (`sys.monitoring.COVERAGE_ID`, the default from 3.14); every tool and the trace
    function receive each event independently, so neither changes what the other sees,
    and this helper never calls `sys.settrace` or `sys.setprofile`. Events of other
    threads are dropped. It raises `RuntimeError` when every id in `_TOOL_IDS` is taken,
    and `AssertionError` (a `raise`, so `python -O` keeps it) when it saw no start of
    `fn`'s own code, so an empty profile never reaches a caller.
    """
    monitoring = sys.monitoring
    events = monitoring.events
    tool = next((tid for tid in _TOOL_IDS if monitoring.get_tool(tid) is None), None)
    if tool is None:
        raise RuntimeError(f"call_profile: sys.monitoring tool ids {_TOOL_IDS} are all taken")
    home = os.path.dirname(os.path.abspath(fn.__code__.co_filename)) + os.sep
    in_home: dict[str, bool] = {}
    thread = threading.get_ident()
    own = call_profile.__code__
    c_calls: Counter[str] = Counter()
    python_calls: Counter[str] = Counter()
    patterns: set[tuple[str | bytes, int]] = set()
    started = False
    steps = 0
    call_events = 0

    def at_home(code: types.CodeType) -> bool:
        name = code.co_filename
        if name not in in_home:
            in_home[name] = os.path.abspath(name).startswith(home)
        return in_home[name]

    def on_call(code: types.CodeType, _offset: int, callee: object, arg0: object) -> None:
        nonlocal call_events
        if threading.get_ident() != thread or code is own or not at_home(code):
            return
        call_events += 1
        if isinstance(callee, (types.FunctionType, types.MethodType)):
            return
        name = getattr(callee, "__qualname__", type(callee).__qualname__)
        c_calls[name] += 1
        if name.startswith("Pattern."):
            owner = getattr(callee, "__self__", None)
            owner = owner if isinstance(owner, re.Pattern) else arg0
            if isinstance(owner, re.Pattern):
                patterns.add((owner.pattern, owner.flags))

    def on_start(code: types.CodeType, _offset: int) -> None:
        nonlocal started
        if threading.get_ident() == thread:
            python_calls[code.co_name] += 1
            started = started or code is fn.__code__

    def on_step(code: types.CodeType, *_where: int) -> None:
        nonlocal steps
        if threading.get_ident() == thread and code is not own and at_home(code):
            steps += 1

    callbacks = {
        events.CALL: on_call,
        events.PY_START: on_start,
        events.LINE: on_step,
        events.JUMP: on_step,
        events.PY_RESUME: on_step,
    }
    mask = 0
    for event in callbacks:
        mask |= event
    gc_was_enabled = gc.isenabled()
    monitoring.use_tool_id(tool, "herd cost call_profile")
    try:
        for event, callback in callbacks.items():
            monitoring.register_callback(tool, event, callback)
        gc.disable()
        try:
            monitoring.set_events(tool, mask)
            fn(*args)
        finally:
            try:
                monitoring.set_events(tool, 0)
            finally:
                if gc_was_enabled:
                    gc.enable()
    finally:
        for event in callbacks:
            monitoring.register_callback(tool, event, None)
        monitoring.free_tool_id(tool)
    if not started:
        raise AssertionError(
            f"call_profile saw no start of {fn.__qualname__}: no events were recorded"
        )
    return CallProfile(
        c_calls=c_calls,
        python_calls=python_calls,
        steps=steps,
        call_events=call_events,
        patterns=frozenset(patterns),
    )


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
