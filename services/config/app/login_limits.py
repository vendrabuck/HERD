"""Attempt limits for the config page login.

`POST /login` is routed through the gateway like every other config route, and a
successful login opens the settings editor and Save and Restart. The service
therefore limits failed logins in process, two ways at once:

- Per source address. The first two failures from a source cost nothing extra.
  From the third failure on, each failure makes the source wait before its next
  attempt: 1 second, doubling with every further failure, capped at 60 seconds
  (1, 2, 4, 8, 16, 32, 60, 60, ...). A source with no failure for
  `SOURCE_IDLE_SECONDS` starts over and is dropped from memory.
- Across all sources. When `CONFIG_LOGIN_MAX_ATTEMPTS` failures from any mix of
  sources fall within `CONFIG_LOGIN_LOCKOUT_SECONDS`, every login is refused for
  `CONFIG_LOGIN_LOCKOUT_SECONDS`.

While either wait applies, a login is answered 429 with `Retry-After` before the
password is checked. A successful login clears its source's failures and the
cross-source count. Each wait that begins is logged once at WARNING with the
action `config_login_locked`, the scope, the source address, the failure count,
and the wait; the password is never logged.

Source address and its trust assumption: the source is the first entry of
`X-Forwarded-For` when it parses as an IP address, else the TCP peer. This relies
on Traefik being the only way in: the config container publishes no host port,
and Traefik's entry points do not trust client-supplied forwarded headers (the
shipped `infra/traefik/traefik.yml` sets no `forwardedHeaders` trust), so Traefik
replaces any `X-Forwarded-For` a client sends with the client's own address. A
deployment that puts another proxy in front of Traefik without configuring
`forwardedHeaders.trustedIPs` makes every client share that proxy's address, so
the per-source wait applies to all of them together; one that marks forwarded
headers trusted for every address lets a client choose its source. The
cross-source limit applies either way.

State lives in this process only (a restart clears it; replicas count
separately), uses the monotonic clock, needs no background task, and is bounded:
a source is forgotten after `SOURCE_IDLE_SECONDS` without a failure, and at most
`MAX_TRACKED_SOURCES` are kept (the longest-quiet go first). The login route
checks, verifies, and records in one request without yielding to the event loop
between them (the password check is synchronous), and a lock guards the state
for any caller outside that path.

Standard library only: the config service does not ship herd_common.
"""

from __future__ import annotations

import ipaddress
import logging
import math
import os
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass

logger = logging.getLogger(__name__)

LOCKED_ACTION = "config_login_locked"

# Per-source schedule, fixed in code.
FREE_FAILURES = 2
BASE_DELAY_SECONDS = 1
MAX_DELAY_SECONDS = 60
SOURCE_IDLE_SECONDS = 900
MAX_TRACKED_SOURCES = 10_000

# Cross-source knobs, read from the environment like CONFIG_ADMIN_PASSWORD.
MAX_ATTEMPTS_ENV = "CONFIG_LOGIN_MAX_ATTEMPTS"
LOCKOUT_SECONDS_ENV = "CONFIG_LOGIN_LOCKOUT_SECONDS"
DEFAULT_MAX_ATTEMPTS = 20
DEFAULT_LOCKOUT_SECONDS = 300


def source_delay_seconds(failures: int) -> int:
    """The wait after a source's `failures`-th consecutive failure (0 for none)."""
    if failures <= FREE_FAILURES:
        return 0
    exponent = failures - FREE_FAILURES - 1
    # Cap the exponent too, so a long run of failures never builds a huge int.
    return min(MAX_DELAY_SECONDS, BASE_DELAY_SECONDS * 2 ** min(exponent, 16))


def _positive_int_from_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if value < 1:
        logger.warning("%s=%r is not a positive integer; using %d", name, raw, default)
        return default
    return value


def client_source(forwarded_for: str | None, peer: str | None) -> str:
    """The source address of a login: see the module docstring for the trust rule."""
    first = (forwarded_for or "").split(",")[0].strip()
    if first:
        try:
            return str(ipaddress.ip_address(first))
        except ValueError:
            pass
    return peer or "unknown"


@dataclass
class _SourceState:
    failures: int = 0
    locked_until: float = 0.0
    last_failure: float = 0.0


class LoginLimiter:
    def __init__(
        self,
        *,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        lockout_seconds: int = DEFAULT_LOCKOUT_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        max_sources: int = MAX_TRACKED_SOURCES,
    ) -> None:
        self.max_attempts = max_attempts
        self.lockout_seconds = lockout_seconds
        self._clock = clock
        self._max_sources = max_sources
        self._sources: OrderedDict[str, _SourceState] = OrderedDict()
        self._recent_failures: deque[float] = deque(maxlen=max_attempts)
        self._global_locked_until = 0.0
        self._lock = threading.Lock()

    @classmethod
    def from_env(cls, **kwargs) -> LoginLimiter:
        return cls(
            max_attempts=_positive_int_from_env(MAX_ATTEMPTS_ENV, DEFAULT_MAX_ATTEMPTS),
            lockout_seconds=_positive_int_from_env(LOCKOUT_SECONDS_ENV, DEFAULT_LOCKOUT_SECONDS),
            **kwargs,
        )

    def retry_after(self, source: str) -> int | None:
        """Whole seconds this source must still wait, or None when it may try now."""
        with self._lock:
            now = self._clock()
            until = self._global_locked_until
            state = self._sources.get(source)
            if state is not None:
                until = max(until, state.locked_until)
            if until <= now:
                return None
            return max(1, math.ceil(until - now))

    def record_failure(self, source: str) -> None:
        with self._lock:
            now = self._clock()
            self._evict_idle(now)
            state = self._sources.pop(source, None) or _SourceState()
            state.failures += 1
            state.last_failure = now
            delay = source_delay_seconds(state.failures)
            if delay:
                state.locked_until = now + delay
                self._log_locked("source", source, state.failures, delay)
            # Most recently failed last, so the front is always the quietest.
            self._sources[source] = state
            while len(self._sources) > self._max_sources:
                self._sources.popitem(last=False)

            window_start = now - self.lockout_seconds
            while self._recent_failures and self._recent_failures[0] <= window_start:
                self._recent_failures.popleft()
            self._recent_failures.append(now)
            if len(self._recent_failures) >= self.max_attempts:
                count = len(self._recent_failures)
                self._recent_failures.clear()
                self._global_locked_until = now + self.lockout_seconds
                self._log_locked("global", source, count, self.lockout_seconds)

    def record_success(self, source: str) -> None:
        with self._lock:
            self._sources.pop(source, None)
            self._recent_failures.clear()

    def tracked_sources(self) -> int:
        with self._lock:
            return len(self._sources)

    def _evict_idle(self, now: float) -> None:
        while self._sources:
            oldest = next(iter(self._sources.values()))
            if now - oldest.last_failure < SOURCE_IDLE_SECONDS:
                break
            self._sources.popitem(last=False)

    @staticmethod
    def _log_locked(scope: str, source: str, failures: int, wait: int) -> None:
        # The config service logs plain text, so the fields go in the message as
        # well as in `extra`.
        logger.warning(
            "%s scope=%s source=%s failures=%d retry_after=%d",
            LOCKED_ACTION,
            scope,
            source,
            failures,
            wait,
            extra={
                "action": LOCKED_ACTION,
                "scope": scope,
                "source": source,
                "failures": failures,
                "retry_after_seconds": wait,
            },
        )


# The process-wide limiter. The login route reads it through this module at call
# time, so tests can swap in one with their own clock and knobs.
LIMITER = LoginLimiter.from_env()
