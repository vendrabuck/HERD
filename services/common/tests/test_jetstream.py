"""Unit tests for herd_common.jetstream.ensure_stream (issue #620).

Covers the add-then-fall-back-to-update contract: a fresh create never calls
update_stream, an identical config succeeds via add_stream alone, a changed
config (BadRequestError with the JetStream "stream name already in use with a
different configuration" code) triggers exactly one update_stream call with
the same config, a BadRequestError carrying a different err_code propagates
instead of being swallowed, a non-BadRequestError propagates unchanged, and
max_age_seconds=None (or 0) yields a StreamConfig with no max_age.
"""

from unittest.mock import AsyncMock

import pytest
from herd_common.jetstream import JS_STREAM_NAME_IN_USE, ensure_stream
from nats.js.errors import BadRequestError


def _make_js(add_stream_error: Exception | None = None) -> AsyncMock:
    js = AsyncMock()
    if add_stream_error is not None:
        js.add_stream.side_effect = add_stream_error
    return js


@pytest.mark.asyncio
async def test_fresh_create_calls_add_stream_once_and_never_update_stream():
    js = _make_js()

    await ensure_stream(
        js, name="HERD_RESERVATIONS", subjects=["herd.reservations.*"], max_age_seconds=604800
    )

    js.add_stream.assert_awaited_once()
    js.update_stream.assert_not_called()


@pytest.mark.asyncio
async def test_identical_config_add_stream_succeeds_is_a_noop_for_update():
    """add_stream succeeding at all (identical or first-time config) means no update_stream call."""
    js = _make_js()

    await ensure_stream(js, name="HERD_HEALTH", subjects=["herd.health.*"], max_age_seconds=None)

    js.add_stream.assert_awaited_once()
    js.update_stream.assert_not_called()


@pytest.mark.asyncio
async def test_stream_name_in_use_error_triggers_exactly_one_update_stream_with_same_config():
    err = BadRequestError(
        code=400, err_code=JS_STREAM_NAME_IN_USE, description="stream name already in use"
    )
    js = _make_js(add_stream_error=err)

    await ensure_stream(js, name="HERD_DLQ", subjects=["herd.*.dlq.>"], max_age_seconds=3600)

    js.add_stream.assert_awaited_once()
    js.update_stream.assert_awaited_once()
    add_config = js.add_stream.await_args.args[0]
    update_config = js.update_stream.await_args.args[0]
    assert add_config == update_config
    assert update_config.name == "HERD_DLQ"
    assert update_config.subjects == ["herd.*.dlq.>"]
    assert update_config.max_age == 3600


@pytest.mark.asyncio
async def test_bad_request_error_with_different_err_code_propagates():
    err = BadRequestError(code=400, err_code=99999, description="some other bad request")
    js = _make_js(add_stream_error=err)

    with pytest.raises(BadRequestError):
        await ensure_stream(
            js, name="HERD_RESERVATIONS", subjects=["herd.reservations.*"], max_age_seconds=None
        )

    js.update_stream.assert_not_called()


@pytest.mark.asyncio
async def test_non_bad_request_error_propagates():
    js = _make_js(add_stream_error=RuntimeError("connection reset"))

    with pytest.raises(RuntimeError):
        await ensure_stream(
            js, name="HERD_RESERVATIONS", subjects=["herd.reservations.*"], max_age_seconds=None
        )

    js.update_stream.assert_not_called()


@pytest.mark.asyncio
async def test_max_age_seconds_none_yields_config_with_no_max_age():
    js = _make_js()

    await ensure_stream(js, name="HERD_HEALTH", subjects=["herd.health.*"], max_age_seconds=None)

    config = js.add_stream.await_args.args[0]
    assert config.max_age is None


@pytest.mark.asyncio
async def test_max_age_seconds_zero_yields_config_with_no_max_age():
    """0 means no cap (config.py convention), same as None."""
    js = _make_js()

    await ensure_stream(js, name="HERD_HEALTH", subjects=["herd.health.*"], max_age_seconds=0)

    config = js.add_stream.await_args.args[0]
    assert config.max_age is None
