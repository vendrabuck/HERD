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

`ensure_consumer`, `nak_delay`, and `parse_nak_backoff_schedule` are issue
#895's helpers: the durable-config upgrade path, and the NAK-delay schedule
every consumer's transient-error branch consults so a bare `msg.nak()` no
longer redelivers immediately (see each function's docstring).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

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


def parse_nak_backoff_schedule(value: str | Sequence[object]) -> list[int]:
    """Parse a NATS NAK-delay schedule (issue #895) from either a
    comma-separated string (an env var's raw form, e.g. "1,5,15,60,120") or
    an already-split sequence (a Settings field's stored `list[str]`, or any
    list of int-like values).

    Used twice per service: once inside each service's
    `NATS_NAK_BACKOFF_SECONDS` Settings field validator, purely to VALIDATE
    and normalize (empty entries, non-integers, and negative values all
    raise ValueError with a message naming the offending entry), and again
    at consumer-module import time to turn that field's validated
    `list[str]` into the `list[int]` `nak_delay` needs. One shared function
    so all three consumers (execution, notifications, integration) validate
    and parse identically instead of carrying three copies.
    """
    if isinstance(value, str):
        parts = [p.strip() for p in value.split(",")]
    else:
        parts = [str(p).strip() for p in value]
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


__all__ = [
    "ensure_stream",
    "ensure_stream_exists",
    "ensure_consumer",
    "nak_delay",
    "parse_nak_backoff_schedule",
    "DEFAULT_NAK_BACKOFF_SECONDS",
    "JS_STREAM_NAME_IN_USE",
]
