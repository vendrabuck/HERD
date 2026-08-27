"""Shared add-or-update helper for declaring a JetStream stream.

Three call sites (reservations' `HERD_RESERVATIONS`, execution's `HERD_HEALTH`
and `HERD_DLQ`) each called `js.add_stream(name, subjects)` directly. That is
idempotent only when the existing stream's configuration matches exactly:
`add_stream` against a stream that already exists with a DIFFERENT
configuration raises rather than returning it. Adding a `max_age` retention
cap to a stream that was originally created without one (an upgraded-in-place
`make prod` stack, issue #620) is exactly that case, so a naive `add_stream`
would turn a routine config change into a boot failure. This helper tries
`add_stream` first and falls back to `update_stream` only when the server
reports the specific "stream name already in use with a different
configuration" error; any other failure propagates unchanged.
"""

from __future__ import annotations

import logging

from nats.js.api import StreamConfig
from nats.js.errors import BadRequestError

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


__all__ = ["ensure_stream", "JS_STREAM_NAME_IN_USE"]
