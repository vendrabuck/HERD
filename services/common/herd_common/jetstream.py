"""Shared JetStream stream-declaration helpers.

`ensure_stream` is an add-or-update helper for the service that OWNS a
stream's configuration. Three call sites (reservations' `HERD_RESERVATIONS`,
execution's `HERD_HEALTH` and `HERD_DLQ`) each called
`js.add_stream(name, subjects)` directly. That is idempotent only when the
existing stream's configuration matches exactly: `add_stream` against a
stream that already exists with a DIFFERENT configuration raises rather than
returning it. Adding a `max_age` retention cap to a stream that was
originally created without one (an upgraded-in-place `make prod` stack, issue
#620) is exactly that case, so a naive `add_stream` would turn a routine
config change into a boot failure. `ensure_stream` tries `add_stream` first
and falls back to `update_stream` only when the server reports the specific
"stream name already in use with a different configuration" error; any other
failure propagates unchanged.

`ensure_stream_exists` is for a CONSUMER that shares a stream it does not
own (integration's, notifications', and execution's NATS consumers, which
each unconditionally declared a stream on every boot). Once the owning
producer's `ensure_stream` call applies a `max_age`, a consumer that also
called `add_stream` with its own (max-age-less) config would hit the same
in-use error on every boot, and if it also fell back to `update_stream` it
would fight the producer, flipping the config back and forth on alternating
restarts. `ensure_stream_exists` never writes a config over an existing
stream: it checks presence with `stream_info` and only calls `add_stream`
(with no `max_age`) when the stream is genuinely missing. In the normal boot
order the owning producer creates the stream with its real config first, so
a consumer's `stream_info` finds it and never reaches `add_stream` at all;
the no-`max_age` fallback config exists only for the case where a consumer
starts before its stream's producer.

`connect_nats` (issue #1083) is the one NATS connect every service uses: a
bounded first connect that raises, then unlimited reconnects once connected.

`ensure_consumer`, `nak_delay`, and `parse_nak_backoff_schedule` are issue
#895's helpers: the durable-config upgrade path, and the NAK-delay schedule
every consumer's transient-error branch consults so a bare `msg.nak()` no
longer redelivers immediately (see each function's docstring).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence

from nats.js.api import ConsumerConfig, StreamConfig
from nats.js.errors import BadRequestError, NotFoundError

logger = logging.getLogger(__name__)

# Default NAK-delay schedule (seconds), matching the original hardcoded
# `backoff=[1, 5, 15, 60, 120]` every consumer used to pass to ConsumerConfig
# before issue #895 found that JetStream silently replaces `ack_wait` with
# `backoff[0]` whenever `backoff` is set, and that `backoff` only times
# ack-timeout redeliveries, never a NAK (a bare `msg.nak()` redelivers
# immediately). This tuple is the fallback default for each service's
# NATS_NAK_BACKOFF_SECONDS setting, not itself read at runtime.
DEFAULT_NAK_BACKOFF_SECONDS = (1, 5, 15, 60, 120)

# JetStream API error code for "stream name already in use with a different
# configuration" (server-side constant `JSStreamNameExistErr` in
# nats-server's `jetstream_errors_generated.go`). Confirmed against the
# nats-server source, not guessed: HTTP status 400 (surfaced by nats-py as
# `BadRequestError`), err_code 10058.
JS_STREAM_NAME_IN_USE = 10058

# Bounded INITIAL connect (issue #1083). nats-py treats a negative
# `max_reconnect_attempts` as "retry forever" for the first connection too
# (the `while True` loop in `Client.connect` continues on NoServersError), so
# `nats.connect(url, max_reconnect_attempts=-1)` against a broker that is down
# at boot never returns and never raises, and a lifespan awaiting it never
# finishes starting. `connect_nats` makes at most this many attempts, spaced
# by NATS_RECONNECT_TIME_WAIT_SECONDS, before raising, so each caller's
# logged-and-continue branch is reachable. Note nats-py reads 0 as unlimited
# too (its pool only discards a server when the cap is > 0), so the bound
# must be at least 1.
NATS_INITIAL_CONNECT_ATTEMPTS = 5
NATS_RECONNECT_TIME_WAIT_SECONDS = 2


async def connect_nats(
    url: str,
    *,
    initial_attempts: int | None = None,
    reconnect_time_wait: float | None = None,
):
    """Connect to NATS with a bounded first connect and unlimited reconnects.

    The first connection is tried at most `initial_attempts` times (plus the
    first try) and then raises the client's own error (`NoServersError`), so
    the caller can log and continue without the broker. Once a connection is
    ESTABLISHED, the client's reconnect cap is switched to -1 (never give up):
    the durable consumers and the outbox relays depend on that connection
    recovering after a broker restart, which the default 60-attempt cap would
    eventually abandon. nats-py reads `options["max_reconnect_attempts"]` on
    every reconnect pass, so the change applies to every later reconnect.

    Omitted arguments read the module constants at CALL time, so a test can
    shorten the bound by patching them.
    """
    if initial_attempts is None:
        initial_attempts = NATS_INITIAL_CONNECT_ATTEMPTS
    if reconnect_time_wait is None:
        reconnect_time_wait = NATS_RECONNECT_TIME_WAIT_SECONDS
    if initial_attempts < 1:
        raise ValueError("initial_attempts must be at least 1 (nats-py reads 0 as unlimited)")
    import nats

    nc = await nats.connect(
        url,
        max_reconnect_attempts=initial_attempts,
        reconnect_time_wait=reconnect_time_wait,
    )
    nc.options["max_reconnect_attempts"] = -1
    return nc


async def ensure_stream(
    js,
    *,
    name: str,
    subjects: list[str],
    max_age_seconds: float | None,
) -> None:
    """Create `name` if it does not exist, else update it to match.

    `max_age_seconds` is seconds, matching `StreamConfig.max_age`'s Python-API
    unit (the nats-py client converts to nanoseconds when it serializes the
    request); 0 or None both mean no retention cap, since the client encodes
    an absent `max_age` as 0 on the wire and the server treats 0 as unbounded.
    """
    config = StreamConfig(
        name=name,
        subjects=subjects,
        max_age=max_age_seconds or None,
    )
    try:
        await js.add_stream(config)
        logger.info("JetStream stream %s created", name)
    except BadRequestError as exc:
        if exc.err_code != JS_STREAM_NAME_IN_USE:
            raise
        await js.update_stream(config)
        logger.info("JetStream stream %s updated", name)


async def ensure_stream_exists(js, *, name: str, subjects: list[str]) -> None:
    """Confirm `name` exists; create it with no `max_age` only if missing.

    For a consumer that does not own the stream's configuration (see the
    module docstring). Never calls `update_stream`: an existing stream, with
    whatever config its owning producer applied, is left untouched. Any
    exception from `stream_info` other than `NotFoundError` propagates
    unchanged, as does any exception from the fallback `add_stream`.
    """
    try:
        await js.stream_info(name)
    except NotFoundError:
        await js.add_stream(StreamConfig(name=name, subjects=subjects))
        logger.info("JetStream stream %s created", name)


async def ensure_consumer(
    js, *, stream: str, subject: str, durable: str, config: ConsumerConfig
) -> None:
    """Create or update the durable consumer `durable` on `stream` to match
    `config`, in place, then leave `config` ready for a following
    `js.pull_subscribe(subject, durable=durable, config=config)` call.

    Why this exists (issue #895): `nats.js.client.JetStreamContext.pull_subscribe`
    only creates a consumer when one is NOT already found by
    `consumer_info(stream, durable)`; if the durable already exists it skips
    `add_consumer` entirely and just binds to whatever config is already on
    the server. On a persistent NATS volume (`make prod` mounts `nats-data`),
    that means a code change to a durable's `ConsumerConfig` (for example,
    this issue's `backoff` removal) would never reach an already-provisioned
    consumer: the old config, `backoff` included, would silently persist
    across every future deploy.

    `js.add_consumer(stream, config=config)` does not have that problem: it
    is itself a create-or-update call, and updating a durable's `ack_wait`
    or clearing its `backoff` are compatible field changes, not the
    "consumer already exists" (err_code 10148) conflict a change to an
    immutable field (deliver_policy, filter_subject, ...) would raise.
    Proven live against nats-server 2.10.29 on a throwaway stream (never
    HERD_RESERVATIONS/HERD_HEALTH): a durable created with
    `backoff=[1, 5, 15, 60, 120]` reported back `ack_wait=1.0` (the server
    substitutes `backoff[0]`); calling `add_consumer` again for the SAME
    durable with `backoff` cleared and `ack_wait=30` updated that durable in
    place to report `ack_wait=30.0`, `backoff=None`.

    Call this before `pull_subscribe`, not instead of it: `pull_subscribe`
    still does the actual inbox setup and subscription binding
    (`pull_subscribe_bind`); by the time it runs, `consumer_info` finds the
    consumer this call just created or updated, so it skips its own
    `add_consumer` and only binds, exactly the do-nothing-extra path it takes
    for any pre-existing durable today.

    Mutates `config.name`, `config.durable_name`, and (only when neither is
    already set) `config.filter_subject`, mirroring exactly what
    `pull_subscribe`'s own creation branch does; reads use `getattr` with a
    default so a minimal test double need not define every ConsumerConfig
    attribute.
    """
    config.name = durable
    config.durable_name = durable
    if not getattr(config, "filter_subjects", None) and not getattr(config, "filter_subject", None):
        config.filter_subject = subject
    await js.add_consumer(stream, config=config)


def nak_delay(num_delivered: int | None, schedule: Sequence[float]) -> float:
    """Map a message's `msg.metadata.num_delivered` (the delivery count
    including the attempt that just failed) to the NAK delay to request for
    the next redelivery, per issue #895's fix: `await
    msg.nak(delay=nak_delay(num_delivered, schedule))` in place of a bare
    `await msg.nak()`, which JetStream would otherwise redeliver
    immediately.

    Delivery 1 failing maps to `schedule[0]`, delivery 2 to `schedule[1]`,
    and so on; a `num_delivered` at or past `len(schedule)` clamps to
    `schedule[-1]` rather than raising, since `max_deliver` (not this
    function) is what bounds how many deliveries actually occur. A missing
    or non-positive `num_delivered` (no metadata, or a test double that
    omits it) falls back to `schedule[0]`, the same first-delivery delay.

    Raises ValueError if `schedule` is empty; there is no sane delay to
    return for an empty schedule, and silently returning 0 would reintroduce
    this issue's immediate-redelivery bug by another path.
    """
    if not schedule:
        raise ValueError("NATS NAK backoff schedule must not be empty")
    if not num_delivered or num_delivered < 1:
        index = 0
    else:
        index = min(num_delivered - 1, len(schedule) - 1)
    return schedule[index]


def parse_nak_backoff_schedule(value: str) -> list[int]:
    """Parse a NATS NAK-delay schedule (issue #895) from a comma-separated
    string (an env var's raw form, e.g. "1,5,15,60,120").

    Used twice per service: once inside each service's
    `NATS_NAK_BACKOFF_SECONDS` Settings field validator, purely to VALIDATE
    and normalize (empty entries, non-integers, and negative values all
    raise ValueError with a message naming the offending entry) before
    re-joining into the field's stored, normalized string, and again at
    consumer-module import time to turn that field's validated string into
    the `list[int]` `nak_delay` needs. One shared function so all three
    consumers (execution, notifications, integration) validate and parse
    identically instead of carrying three copies.
    """
    parts = [p.strip() for p in value.split(",")]
    if not parts or any(p == "" for p in parts):
        raise ValueError(
            "NATS NAK backoff schedule must be a non-empty comma-separated list "
            "of non-negative integers, e.g. '1,5,15,60,120'"
        )
    schedule: list[int] = []
    for part in parts:
        try:
            seconds = int(part)
        except ValueError:
            raise ValueError(
                f"NATS NAK backoff schedule entry {part!r} is not an integer"
            ) from None
        if seconds < 0:
            raise ValueError(f"NATS NAK backoff schedule entry {part!r} must be non-negative")
        schedule.append(seconds)
    return schedule


# Smallest ack_wait a consumer may be configured with (issue #944). The
# heartbeat runs at half of it, and a sub-second heartbeat interval would be
# noise, not a safety margin; each service's NATS_ACK_WAIT_SECONDS Settings
# field refuses anything lower at load.
MIN_ACK_WAIT_SECONDS = 2


def validate_ack_wait_seconds(value: int) -> int:
    """Settings-time check for a consumer's `nats_ack_wait_seconds` (issue
    #944): returns `value` unchanged, or raises ValueError naming the minimum.
    Shared so execution, notifications, and integration refuse the same way."""
    if value < MIN_ACK_WAIT_SECONDS:
        raise ValueError(
            f"NATS_ACK_WAIT_SECONDS must be at least {MIN_ACK_WAIT_SECONDS} seconds, got {value}"
        )
    return value


def heartbeat_interval(ack_wait_seconds: float) -> float:
    """The in-progress heartbeat cadence for a consumer whose ack_wait is
    `ack_wait_seconds`: always HALF of it (issue #944), derived here and
    nowhere else so no consumer can drift from the rule. Half leaves margin for
    a late heartbeat while still resetting the ack timer well before it
    expires. True division on purpose: a 3 s ack_wait gives 1.5 s, never an
    integer floor that could reach 0 and spin."""
    return ack_wait_seconds / 2


async def keep_messages_alive(messages: list, interval: float) -> None:
    """Reset ack_wait on every still-in-flight message until it is settled.

    The ONE heartbeat implementation every durable consumer shares (issue #317
    for execution, issue #911 for integration and notifications). Runs
    concurrently with the sequential processing of a fetched batch. Each cycle
    sends work-in-progress to every message still in `messages`; the caller
    removes a message as soon as it is acked/naked, so heartbeating stops for
    settled messages. in_progress failures are swallowed: a heartbeat is
    best-effort and must never wedge the consumer. The task never ends on its
    own; the caller cancels it. `interval` must stay strictly below the
    consumer's ack_wait (use `heartbeat_interval`).
    """
    while True:
        await asyncio.sleep(interval)
        for msg in list(messages):
            try:
                await msg.in_progress()
            except Exception:
                logger.debug("in_progress heartbeat failed; continuing", exc_info=True)


async def process_batch_with_heartbeat(
    msgs: list,
    process_one: Callable[[object], Awaitable[object]],
    interval: float,
) -> None:
    """Run `process_one(msg)` for each fetched message in order while
    `keep_messages_alive` heartbeats every message that is not yet settled
    (the one running AND any queued behind it).

    A message leaves the in-flight list as soon as its `process_one` returns
    or raises, so its heartbeat stops. `process_one` is expected to ack or nak
    (or dead-letter) the message itself; an exception it raises is logged and
    the batch continues. The heartbeat task is always cancelled and awaited on
    the way out, including when the caller is cancelled mid-message, so no
    task leaks.
    """
    in_flight = list(msgs)
    heartbeat = asyncio.create_task(keep_messages_alive(in_flight, interval))
    try:
        for msg in msgs:
            try:
                await process_one(msg)
            except Exception:
                logger.error("Unexpected error processing NATS message", exc_info=True)
            finally:
                try:
                    in_flight.remove(msg)
                except ValueError:
                    pass
    finally:
        heartbeat.cancel()
        # gather (not a bare await inside try/except CancelledError): a cancel
        # aimed at THIS task while it waits here must still propagate, whereas
        # catching CancelledError would swallow it and leave the consumer loop
        # running after stop_nats_consumer cancelled it.
        await asyncio.gather(heartbeat, return_exceptions=True)


__all__ = [
    "MIN_ACK_WAIT_SECONDS",
    "heartbeat_interval",
    "validate_ack_wait_seconds",
    "keep_messages_alive",
    "process_batch_with_heartbeat",
    "ensure_stream",
    "ensure_stream_exists",
    "ensure_consumer",
    "nak_delay",
    "parse_nak_backoff_schedule",
    "DEFAULT_NAK_BACKOFF_SECONDS",
    "JS_STREAM_NAME_IN_USE",
]
