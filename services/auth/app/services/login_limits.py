"""Per-source limit on password attempts (IAM-LOGIN-7).

Each password attempt costs one bcrypt computation (IAM-LOGIN-2) or one directory
round trip, so auth limits how fast one source address may make them. Shaped from
the config page's limiter (`services/config/app/login_limits.py`, OPS-CONFIG-22),
with three differences, each deliberate:

- Every attempt counts, admitted or refused, not only failures, and a success
  clears nothing. Counting failures only would leave the cost open to anyone with
  one working password (in local mode anyone can register one) by signing in over
  and over.
- It is keyed by source address only, never by the email or login name, so a
  client cannot lock a named account out, and the answer says nothing about
  whether an account exists.
- There is no cross-source lockout: on the main login one client could then lock
  every user out. The bound across sources is the hashing pool instead
  (`passwords.HashingPool`, IAM-HASH-7): a flood from many addresses gets 503s
  once the pool's wait is full, and the event loop stays free.

The allowance is a token bucket: a source starts with `max_attempts` attempts, and
they refill evenly, one every `window_seconds / max_attempts` seconds, up to
`max_attempts`. So a burst of `max_attempts` is admitted, and after that one
attempt per refill interval. An attempt with no allowance left is refused with the
whole seconds until the next one refills (at least 1), as the 429's `Retry-After`;
the refusal costs nothing, since the routes ask before any query, bcrypt, or
directory call.

Source address and its trust assumption. Auth publishes no host port; every
client request reaches it through Traefik, whose entry points do not trust a
client's forwarded headers (the shipped Traefik command sets no
`forwardedHeaders` options): Traefik drops any `X-Forwarded-For` a client sends
and appends the address it saw. The source is therefore the LAST
`X-Forwarded-For` entry, the one the trusted hop appended, when it parses as an IP
address, else the TCP peer. The last entry, not the first (the config limiter
takes the first): with `forwardedHeaders.insecure` or a wide `trustedIPs`,
Traefik would keep a client's own entries in front of the one it appends, and the
last entry is still the one the client did not write. A deployment that puts
another proxy in front of Traefik makes every client share that proxy's address
(the limit then applies to all of them together); a caller inside the compose
network, which reaches auth without Traefik, can name any source, as it can do
anything else a client can. An IPv6 source is keyed by its /64 network, since one
host commonly holds a whole /64; an IPv4-mapped IPv6 address is keyed as the IPv4
address.

State lives in this process only: a restart clears it, and with several workers
or replicas each one counts separately (with N of them, a source gets up to N
times the allowance). It uses the monotonic clock, needs no background task, and
is bounded: a source whose allowance has fully refilled is the same as an unknown
one and is dropped, and at most `MAX_TRACKED_SOURCES` are kept, the least recently
seen dropped first (a dropped source starts over with a full allowance).
`acquire` neither awaits nor yields, so a check and its count happen together; a
lock guards the state for any caller off the event loop.

The first refusal of a run logs one WARNING (action `auth_login_limited`, the
source and the wait); further refusals of the same run log nothing, so a flood
writes one line per source, not one per request. No line holds an email or a
password.
"""

from __future__ import annotations

import ipaddress
import logging
import math
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from starlette.requests import Request

logger = logging.getLogger(__name__)

LIMITED_ACTION = "auth_login_limited"
MAX_TRACKED_SOURCES = 10_000
IPV6_PREFIX_LENGTH = 64
UNKNOWN_SOURCE = "unknown"

DEFAULT_MAX_ATTEMPTS = 60
DEFAULT_WINDOW_SECONDS = 60


def _normalized(address: str) -> str | None:
    """The bucket key for one textual address, or None when it is not an IP."""
    try:
        ip = ipaddress.ip_address(address.strip())
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return str(ip.ipv4_mapped)
        return str(ipaddress.IPv6Network((ip, IPV6_PREFIX_LENGTH), strict=False))
    return str(ip)


def client_source(forwarded_for: Iterable[str], peer: str | None) -> str:
    """The source a request is counted against: see the module docstring.

    `forwarded_for` is every `X-Forwarded-For` header value, in order; `peer`
    is the TCP peer's host.
    """
    entries = [part for value in forwarded_for for part in value.split(",")]
    last = next((e.strip() for e in reversed(entries) if e.strip()), None)
    if last is not None:
        key = _normalized(last)
        if key is not None:
            return key
    if peer:
        return _normalized(peer) or peer
    return UNKNOWN_SOURCE


def request_source(request: Request) -> str:
    """`client_source` for a Starlette request."""
    peer = request.client.host if request.client else None
    return client_source(request.headers.getlist("x-forwarded-for"), peer)


def _positive_or_default(name: str, value: int, default: int) -> int:
    if value >= 1:
        return value
    logger.warning(
        "%s=%r is not a positive integer; using %d",
        name,
        value,
        default,
        extra={"action": "auth_login_limit_setting_ignored", "setting": name},
    )
    return default


@dataclass
class _Bucket:
    tokens: float
    updated: float
    logged: bool = False


class SourceLimiter:
    def __init__(
        self,
        *,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        window_seconds: int = DEFAULT_WINDOW_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        max_sources: int = MAX_TRACKED_SOURCES,
    ) -> None:
        if max_attempts < 1 or window_seconds < 1 or max_sources < 1:
            raise ValueError("limits must be positive")
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self._rate = max_attempts / window_seconds  # attempts refilled per second
        self._clock = clock
        self._max_sources = max_sources
        self._buckets: OrderedDict[str, _Bucket] = OrderedDict()
        self._lock = threading.Lock()

    @classmethod
    def from_settings(cls, config=None, **kwargs) -> SourceLimiter:
        if config is None:
            from app.config import settings as config
        return cls(
            max_attempts=_positive_or_default(
                "AUTH_LOGIN_MAX_ATTEMPTS", config.auth_login_max_attempts, DEFAULT_MAX_ATTEMPTS
            ),
            window_seconds=_positive_or_default(
                "AUTH_LOGIN_WINDOW_SECONDS",
                config.auth_login_window_seconds,
                DEFAULT_WINDOW_SECONDS,
            ),
            **kwargs,
        )

    def acquire(self, source: str) -> int | None:
        """Count one attempt from `source`: None when it is admitted, else the
        whole seconds (at least 1) until the source may try again."""
        with self._lock:
            now = self._clock()
            self._drop_refilled(now)
            bucket = self._buckets.pop(source, None)
            if bucket is None:
                bucket = _Bucket(tokens=float(self.max_attempts), updated=now)
            else:
                elapsed = max(0.0, now - bucket.updated)
                bucket.tokens = min(float(self.max_attempts), bucket.tokens + elapsed * self._rate)
                bucket.updated = now
            # Most recently seen last, so the front is always the quietest.
            self._buckets[source] = bucket
            while len(self._buckets) > self._max_sources:
                self._buckets.popitem(last=False)
            if bucket.tokens >= 1.0:
                bucket.tokens -= 1.0
                bucket.logged = False
                return None
            wait = max(1, math.ceil((1.0 - bucket.tokens) / self._rate))
            if not bucket.logged:
                bucket.logged = True
                self._log_limited(source, wait)
            return wait

    def tracked_sources(self) -> int:
        with self._lock:
            return len(self._buckets)

    def _drop_refilled(self, now: float) -> None:
        # A bucket untouched for a whole window has refilled completely, so it
        # is the same as an unknown source: forget it.
        while self._buckets:
            oldest = next(iter(self._buckets.values()))
            if now - oldest.updated < self.window_seconds:
                break
            self._buckets.popitem(last=False)

    def _log_limited(self, source: str, wait: int) -> None:
        logger.warning(
            "Password attempts limited for source %s; retry after %d s",
            source,
            wait,
            extra={
                "action": LIMITED_ACTION,
                "source": source,
                "retry_after_seconds": wait,
                "max_attempts": self.max_attempts,
                "window_seconds": self.window_seconds,
            },
        )


# The process-wide limiter. The routes read it through this module at call time,
# so tests can swap in one with their own clock and knobs.
LIMITER = SourceLimiter.from_settings()


def reset_for_tests() -> None:
    """A fresh process-wide limiter from the current settings."""
    global LIMITER
    LIMITER = SourceLimiter.from_settings()
