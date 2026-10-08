"""Unit tests for herd_common.jetstream.connect_nats (issue #1083).

These drive the REAL nats-py client, not a mock that raises: a connect with
`max_reconnect_attempts=-1` never returns and never raises against a broker
that is down, so a mock that raises proved a path the real client never took.
The broker side is a minimal in-process fake that speaks just enough of the
NATS text protocol for a handshake (INFO, then PONG for every PING).
"""

import asyncio

import nats
import pytest
from herd_common.jetstream import (
    NATS_INITIAL_CONNECT_ATTEMPTS,
    connect_nats,
)
from nats.errors import NoServersError

_INFO = (
    b'INFO {"server_id":"fake","version":"2.10.0","go":"go1.22","host":"127.0.0.1",'
    b'"port":4222,"max_payload":1048576,"proto":1,"headers":true}\r\n'
)


class _FakeBroker:
    """Accepts NATS clients on a fixed port; can be stopped and restarted."""

    def __init__(self) -> None:
        self.port: int | None = None
        self._server: asyncio.base_events.Server | None = None
        self._writers: list[asyncio.StreamWriter] = []

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._writers.append(writer)
        writer.write(_INFO)
        await writer.drain()
        try:
            while line := await reader.readline():
                if line.startswith(b"PING"):
                    writer.write(b"PONG\r\n")
                    await writer.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            writer.close()

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", self.port or 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        assert self._server is not None
        self._server.close()
        for w in self._writers:
            w.close()
        self._writers.clear()
        await self._server.wait_closed()
        self._server = None


async def _wait_for(predicate, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.02)


def _closed_port() -> int:
    # Port 1 (tcpmux) is never served on a test host: connect is refused at once.
    return 1


async def test_initial_connect_to_a_down_broker_raises_instead_of_hanging():
    """The bug: `nats.connect(..., max_reconnect_attempts=-1)` never returns.
    connect_nats must raise the client's NoServersError within its bound."""
    with pytest.raises(NoServersError):
        await asyncio.wait_for(
            connect_nats(
                f"nats://127.0.0.1:{_closed_port()}",
                initial_attempts=2,
                reconnect_time_wait=0.01,
            ),
            timeout=10,
        )


async def test_unbounded_initial_connect_hangs_documents_the_root_cause():
    """Pins the nats-py behavior the fix works around: a negative cap retries
    the FIRST connect forever (no return, no raise)."""
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(
            nats.connect(
                f"nats://127.0.0.1:{_closed_port()}",
                max_reconnect_attempts=-1,
                reconnect_time_wait=0.01,
            ),
            timeout=0.5,
        )


@pytest.mark.parametrize("attempts", [0, -1])
async def test_initial_attempts_below_one_is_refused(attempts):
    # nats-py reads 0 as unlimited too, so 0 would reintroduce the hang.
    with pytest.raises(ValueError, match="initial_attempts must be at least 1"):
        await connect_nats("nats://127.0.0.1:4222", initial_attempts=attempts)


def test_default_bound_is_at_least_one():
    assert NATS_INITIAL_CONNECT_ATTEMPTS >= 1


async def test_established_connection_reconnects_past_the_initial_bound():
    """Once connected, the reconnect cap is unlimited: the client survives a
    broker outage far longer than `initial_attempts` reconnect tries and
    reconnects when the broker returns."""
    broker = _FakeBroker()
    await broker.start()
    nc = await connect_nats(
        f"nats://127.0.0.1:{broker.port}",
        initial_attempts=1,
        reconnect_time_wait=0.05,
    )
    try:
        assert nc.is_connected
        assert nc.options["max_reconnect_attempts"] == -1

        await broker.stop()
        await _wait_for(lambda: not nc.is_connected)
        # 0.6 s at a 0.05 s wait is many more tries than the bound of 1.
        await asyncio.sleep(0.6)
        assert not nc.is_closed

        await broker.start()
        await _wait_for(lambda: nc.is_connected)
    finally:
        await nc.close()
        if broker._server is not None:
            await broker.stop()


async def test_control_a_bounded_cap_alone_closes_the_connection():
    """Control for the test above: without the switch to -1 the same outage
    exhausts the cap and closes the client, so the test above discriminates."""
    broker = _FakeBroker()
    await broker.start()
    nc = await nats.connect(
        f"nats://127.0.0.1:{broker.port}",
        max_reconnect_attempts=1,
        reconnect_time_wait=0.05,
    )
    try:
        await broker.stop()
        await _wait_for(lambda: nc.is_closed)
    finally:
        await nc.close()
