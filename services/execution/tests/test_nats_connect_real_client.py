"""Issue #1083: start_nats_consumer against a broker that is down, with the
REAL nats client (no mock of nats.connect).

Before the fix the connect used max_reconnect_attempts=-1, which nats-py
applies to the FIRST connect as well: the call never returned and never
raised, so the service's lifespan never finished and the logged-and-continue
branch below was unreachable. herd_common.jetstream.connect_nats bounds the
first connect; the bound is shortened here so the test runs in well under a
second.

Issue #1418 pins the warning: it names the consumers that are not running and
says their events wait on the streams, with the exception class name only and
never the URL (which can hold credentials) or a traceback.
"""

import asyncio
import logging
from types import SimpleNamespace

import herd_common.jetstream as jetstream
import pytest

# app.main calls setup_logging at import, which clears the root handlers; import
# it here, before caplog installs its handler, not inside a test.
from app import main as main_module
from app.config import settings
from app.services.nats_consumer import start_nats_consumer


@pytest.mark.asyncio
async def test_start_nats_consumer_returns_when_broker_is_down(monkeypatch, caplog):
    monkeypatch.setattr(jetstream, "NATS_INITIAL_CONNECT_ATTEMPTS", 2)
    monkeypatch.setattr(jetstream, "NATS_RECONNECT_TIME_WAIT_SECONDS", 0.01)
    # Port 1 is never served on a test host, so every connect is refused.
    # Userinfo in the URL stands for credentials the warning must never carry.
    monkeypatch.setattr(settings, "nats_url", "nats://herd-user:tok3n-value@127.0.0.1:1")
    app = SimpleNamespace(state=SimpleNamespace())

    with caplog.at_level("WARNING", logger="app.services.nats_consumer"):
        await asyncio.wait_for(start_nats_consumer(app), timeout=10)

    records = [r for r in caplog.records if r.name == "app.services.nats_consumer"]
    assert [(r.levelno, r.getMessage()) for r in records] == [
        (
            logging.WARNING,
            "NATS connect failed (NoServersError); not running: execution-consumer. "
            "Nothing retries the connect, so these consumers start only when the "
            "service is restarted with the broker reachable; events published to "
            "their streams meanwhile wait there, kept up to each stream's max age, "
            "and are processed after that restart",
        )
    ]
    assert records[0].exc_info is None
    assert "tok3n-value" not in caplog.text
    assert not hasattr(app.state, "nats")
    assert not hasattr(app.state, "nats_consumer_task")


@pytest.mark.asyncio
async def test_consumer_start_with_broker_down_skips_the_stream_ensures(monkeypatch, caplog):
    """Issue #1418, the lifespan's consumer step (`_start_consumer_and_streams`)
    with the real client: the failed connect leaves no connection, so the
    HERD_HEALTH and HERD_DLQ ensures return at once and log nothing; the
    connect warning is the only warning the step writes."""
    monkeypatch.setattr(jetstream, "NATS_INITIAL_CONNECT_ATTEMPTS", 2)
    monkeypatch.setattr(jetstream, "NATS_RECONNECT_TIME_WAIT_SECONDS", 0.01)
    monkeypatch.setattr(settings, "nats_url", "nats://127.0.0.1:1")
    app = SimpleNamespace(state=SimpleNamespace())

    with caplog.at_level("INFO"):
        await asyncio.wait_for(main_module._start_consumer_and_streams(app), timeout=10)

    ours = [
        r.getMessage()
        for r in caplog.records
        if r.name in ("app.services.nats_consumer", "app.main") and r.levelno >= logging.INFO
    ]
    assert len(ours) == 1
    assert ours[0].startswith(
        "NATS connect failed (NoServersError); not running: execution-consumer."
    )
    assert not hasattr(app.state, "nats")
