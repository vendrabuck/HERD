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
"""

from __future__ import annotations

import logging

from nats.js.api import StreamConfig
from nats.js.errors import BadRequestError, NotFoundError

logger = logging.getLogger(__name__)

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


__all__ = ["ensure_stream", "ensure_stream_exists", "JS_STREAM_NAME_IN_USE"]
