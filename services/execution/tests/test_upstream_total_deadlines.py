"""Total deadlines on the driver download and the internal reads (issue #1498).

httpx applies a timeout per phase (connect, each read, write, pool), so a response
that delivers a byte more often than the per-phase timeout never times out: the
driver download (30 s per phase) and every ``_get_internal`` read (10 s per phase)
could stall the consumer, which handles one message at a time, without bound. Each
now runs under a total deadline, ``DRIVER_DOWNLOAD_DEADLINE_SECONDS`` (120) and
``INTERNAL_READ_DEADLINE_SECONDS`` (20), and a deadline surfaces as the transient
failure each path already had:

- the download: ``load_driver``'s ``RuntimeError`` naming only the class, stored as
  ``driver load failed: TimeoutError``, transient (WIRE-DRIVER-7, CFG-LOAD-2);
- an internal read: ``TransientUpstreamError`` in the transport-error form, which the
  consumer nacks (WIRE-GATE-3) and the manual retry route answers 503.

The stalled peer is an ``httpx.MockTransport`` whose body drips one byte every 20 ms,
far under any per-phase timeout, forever; the deadline is patched to a fraction of a
second. Every await of the code under test runs under a hang guard
(``asyncio.wait_for``) so a regression fails instead of hanging, and every
elapsed-time bound is loose in the safe direction (a regression never finishes at
all).

A deadline must not swallow a cancellation that is not its own: a task cancelled
while it waits, or an OUTER ``asyncio.timeout`` shorter than the inner deadline,
surfaces at the outer scope as ``CancelledError`` or the outer ``TimeoutError``, never
as the transient error (the cancellation tests at the end of each section).
"""

import asyncio
import json
import time
import uuid
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from app.database import Base
from app.models.driver_cache import DriverCache
from app.models.l1_connection_assignment import L1ConnectionAssignment
from app.services import driver_loader, nats_consumer
from app.services.driver_loader import (
    DRIVER_DOWNLOAD_DEADLINE_SECONDS,
    download_driver_package,
    driver_load_failure_text,
    is_permanent_load_failure,
    load_driver,
)
from app.services.nats_consumer import (
    INTERNAL_READ_DEADLINE_SECONDS,
    NATS_NAK_BACKOFF_SECONDS,
    TransientUpstreamError,
    _fetch_device,
    _get_internal,
    process_reservation_message,
)
from app.services.wiring_retry_service import is_retryable_failure
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

RealAsyncClient = httpx.AsyncClient

# The patched deadline, and the hang guard a regression runs into instead of hanging.
SHORT_DEADLINE = 0.3
HANG_GUARD_SECONDS = 10
# Loose upper bound on a call cut at SHORT_DEADLINE: holds pinned to one core.
ELAPSED_BOUND_SECONDS = 5
DRIP_INTERVAL = 0.02

test_engine = create_async_engine(
    "sqlite+aiosqlite:///:memory:",
    echo=False,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestSessionLocal = async_sessionmaker(test_engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
async def setup_db():
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


class DripPeer:
    """A peer that answers 200 and then drips its body one byte at a time, forever.

    Records each request's per-phase timeout extension and how many bytes it sent,
    so a test can show the stream was live (under the per-phase timeout) when the
    total deadline cut it."""

    def __init__(self) -> None:
        self.timeouts: list[dict] = []
        self.sent = 0

    async def _body(self):
        while True:
            self.sent += 1
            yield b"x"
            await asyncio.sleep(DRIP_INTERVAL)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.timeouts.append(request.extensions.get("timeout"))
        return httpx.Response(200, content=self._body())

    def client(self, *args, **kwargs) -> httpx.AsyncClient:
        """Stands in for httpx.AsyncClient(...) in the code under test."""
        return RealAsyncClient(transport=httpx.MockTransport(self.handler), **kwargs)


async def _wait_until_streaming(peer: DripPeer) -> None:
    """Return once the peer has started its body, so a cancellation lands mid-read."""
    for _ in range(HANG_GUARD_SECONDS * 100):
        if peer.sent >= 1:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the peer never started its body")


def _per_phase(seconds: float) -> dict:
    return {"connect": seconds, "read": seconds, "write": seconds, "pool": seconds}


def test_deadlines_are_the_documented_constants():
    assert DRIVER_DOWNLOAD_DEADLINE_SECONDS == 120.0
    assert INTERNAL_READ_DEADLINE_SECONDS == 20.0


# --- the driver download ----------------------------------------------------


async def test_download_drip_is_cut_at_the_total_deadline():
    peer = DripPeer()
    with (
        patch.object(driver_loader, "DRIVER_DOWNLOAD_DEADLINE_SECONDS", SHORT_DEADLINE),
        patch("app.services.driver_loader.httpx.AsyncClient", side_effect=peer.client),
    ):
        started = time.monotonic()
        with pytest.raises(TimeoutError, match=f"within {SHORT_DEADLINE:g} seconds"):
            await asyncio.wait_for(download_driver_package(uuid.uuid4()), HANG_GUARD_SECONDS)
        elapsed = time.monotonic() - started

    assert peer.timeouts == [_per_phase(30.0)], "the per-phase timeout is kept"
    assert peer.sent >= 2, "the body was still arriving when the deadline cut it"
    assert SHORT_DEADLINE <= elapsed < ELAPSED_BOUND_SECONDS


async def test_download_under_the_deadline_returns_the_body():
    def handler(request):
        return httpx.Response(200, content=b"zip-bytes")

    with patch(
        "app.services.driver_loader.httpx.AsyncClient",
        side_effect=lambda *a, **k: RealAsyncClient(transport=httpx.MockTransport(handler), **k),
    ):
        body = await asyncio.wait_for(download_driver_package(uuid.uuid4()), HANG_GUARD_SECONDS)
        assert body == b"zip-bytes"


async def test_load_driver_maps_a_download_deadline_to_the_transient_failure(tmp_path):
    """The deadline reaches load_driver's existing transient path: a RuntimeError
    naming only the class, row text `driver load failed: TimeoutError`, not a
    permanent failure; and nothing reaches the cache, on disk or in the table."""
    peer = DripPeer()
    driver_id = uuid.uuid4()
    cache_root = tmp_path / "cache"
    cache_root.mkdir()
    with (
        patch.object(driver_loader, "DRIVER_DOWNLOAD_DEADLINE_SECONDS", SHORT_DEADLINE),
        patch("app.services.driver_loader.httpx.AsyncClient", side_effect=peer.client),
        patch("app.services.driver_loader.settings") as mock_settings,
    ):
        mock_settings.allow_driver_pip_install = False
        mock_settings.driver_cache_path = str(cache_root)
        mock_settings.inventory_service_url = "http://inventory"
        mock_settings.internal_api_token = "token"
        async with TestSessionLocal() as db:
            with pytest.raises(RuntimeError) as exc:
                await asyncio.wait_for(
                    load_driver(db, driver_id, "sha", "driver.zip", "Layer 1 Switch"),
                    HANG_GUARD_SECONDS,
                )

    assert str(exc.value) == f"Failed to download driver {driver_id}: TimeoutError"
    assert driver_load_failure_text(exc.value) == "driver load failed: TimeoutError"
    assert not is_permanent_load_failure(exc.value)
    assert list(cache_root.iterdir()) == []
    async with TestSessionLocal() as db:
        assert (await db.execute(select(DriverCache))).scalars().all() == []


async def test_cancelling_a_download_propagates_and_is_not_a_deadline():
    """A cancellation from outside reaches the canceller as CancelledError; the
    download's own deadline (still 120 s here) never turns it into TimeoutError."""
    peer = DripPeer()
    with patch("app.services.driver_loader.httpx.AsyncClient", side_effect=peer.client):
        task = asyncio.create_task(download_driver_package(uuid.uuid4()))
        await _wait_until_streaming(peer)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, HANG_GUARD_SECONDS)


async def test_an_outer_timeout_shorter_than_the_download_deadline_is_the_outer_one(tmp_path):
    """An outer asyncio.timeout that expires first surfaces as its own TimeoutError at
    the outer scope, through load_driver, never as the download's TimeoutError or the
    transient RuntimeError load_driver makes of one."""
    peer = DripPeer()
    cache_root = tmp_path / "cache"
    cache_root.mkdir()

    async def load_under_outer_timeout():
        async with TestSessionLocal() as db:
            async with asyncio.timeout(SHORT_DEADLINE):
                await load_driver(db, uuid.uuid4(), "sha", "driver.zip", "Layer 1 Switch")

    with (
        patch("app.services.driver_loader.httpx.AsyncClient", side_effect=peer.client),
        patch("app.services.driver_loader.settings") as mock_settings,
    ):
        mock_settings.allow_driver_pip_install = False
        mock_settings.driver_cache_path = str(cache_root)
        mock_settings.inventory_service_url = "http://inventory"
        mock_settings.internal_api_token = "token"
        with pytest.raises(TimeoutError) as exc:
            await asyncio.wait_for(load_under_outer_timeout(), HANG_GUARD_SECONDS)

    assert type(exc.value) is TimeoutError
    assert "no complete download" not in str(exc.value)
    assert isinstance(exc.value.__cause__, asyncio.CancelledError)
    assert peer.sent >= 2
    assert list(cache_root.iterdir()) == []


# --- the internal reads -----------------------------------------------------


async def test_get_internal_drip_is_cut_at_the_total_deadline():
    peer = DripPeer()
    async with peer.client() as client:
        with patch.object(nats_consumer, "INTERNAL_READ_DEADLINE_SECONDS", SHORT_DEADLINE):
            started = time.monotonic()
            with pytest.raises(TransientUpstreamError) as exc:
                await asyncio.wait_for(
                    _get_internal(
                        client, "http://inventory/devices/x/internal", what="probe", timeout=10.0
                    ),
                    HANG_GUARD_SECONDS,
                )
            elapsed = time.monotonic() - started

    assert str(exc.value) == "probe: transport error: TimeoutError"
    assert isinstance(exc.value.__cause__, TimeoutError)
    assert peer.timeouts == [_per_phase(10.0)], "the per-phase timeout is kept"
    assert peer.sent >= 2
    assert SHORT_DEADLINE <= elapsed < ELAPSED_BOUND_SECONDS


async def test_get_internal_under_the_deadline_returns_the_response():
    def handler(request):
        return httpx.Response(404, json={"detail": "Not found"})

    async with RealAsyncClient(transport=httpx.MockTransport(handler)) as client:
        resp = await asyncio.wait_for(
            _get_internal(client, "http://inventory/x", what="probe", timeout=10.0),
            HANG_GUARD_SECONDS,
        )
    assert resp.status_code == 404


async def test_switch_device_read_deadline_is_the_transient_error_not_absent():
    """Fail closed (WIRE-ORDER-11): a stalled switch read must never read as a 404
    (which would pin the switch's rows as unresolvable); it raises."""
    peer = DripPeer()
    async with peer.client() as client:
        with patch.object(nats_consumer, "INTERNAL_READ_DEADLINE_SECONDS", SHORT_DEADLINE):
            with pytest.raises(TransientUpstreamError, match="transport error: TimeoutError"):
                await asyncio.wait_for(_fetch_device("dev-1", client), HANG_GUARD_SECONDS)


async def test_a_stalled_corroboration_read_naks_the_message():
    """The consumer's mapping end to end: the event-corroboration read stalls, the
    deadline raises TransientUpstreamError, and the message is nacked with the
    backoff, the handler never run (WIRE-GATE-3)."""
    peer = DripPeer()
    msg = MagicMock()
    msg.data = json.dumps(
        {"event": "reservation.wiring_changed", "reservation_id": str(uuid.uuid4())}
    ).encode()
    msg.metadata = type("Meta", (), {"num_delivered": 1})()
    msg.ack = AsyncMock()
    msg.nak = AsyncMock()
    js = MagicMock()
    js.publish = AsyncMock()
    handler = AsyncMock()

    with (
        patch.object(nats_consumer, "INTERNAL_READ_DEADLINE_SECONDS", SHORT_DEADLINE),
        patch("app.services.nats_consumer.httpx.AsyncClient", side_effect=peer.client),
    ):
        result = await asyncio.wait_for(
            process_reservation_message(
                msg, js, handler, session_factory=lambda: None, max_deliver=5
            ),
            HANG_GUARD_SECONDS,
        )

    assert result == "nak"
    msg.nak.assert_awaited_once_with(delay=NATS_NAK_BACKOFF_SECONDS[0])
    msg.ack.assert_not_awaited()
    handler.assert_not_awaited()


async def test_cancelling_an_internal_read_propagates_and_is_not_transient():
    peer = DripPeer()
    async with peer.client() as client:
        task = asyncio.create_task(
            _get_internal(client, "http://inventory/devices/x/internal", what="probe", timeout=10.0)
        )
        await _wait_until_streaming(peer)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, HANG_GUARD_SECONDS)


async def test_an_outer_timeout_shorter_than_the_read_deadline_is_the_outer_one():
    """The inner deadline is still 20 s; the outer scope's 0.3 s expires first and
    surfaces as its own TimeoutError, not as TransientUpstreamError."""
    peer = DripPeer()

    async def read_under_outer_timeout(client):
        async with asyncio.timeout(SHORT_DEADLINE):
            await _get_internal(
                client, "http://inventory/devices/x/internal", what="probe", timeout=10.0
            )

    async with peer.client() as client:
        with pytest.raises(TimeoutError) as exc:
            await asyncio.wait_for(read_under_outer_timeout(client), HANG_GUARD_SECONDS)

    assert type(exc.value) is TimeoutError
    assert isinstance(exc.value.__cause__, asyncio.CancelledError)
    assert peer.sent >= 2


# --- functional: the manual wiring retry route ------------------------------

RES_ID = str(uuid.uuid4())
SWITCH_ID = str(uuid.uuid4())
DRIVER_ID = str(uuid.uuid4())
SWITCH = {
    "id": SWITCH_ID,
    "name": "sw-1",
    "template_id": "tmpl-1",
    "driver_id": DRIVER_ID,
    "driver_sha256": "sha256abc",
    "driver_filename": "driver.zip",
    "connection_type": "Layer 1 Switch",
    "field_data": {"ip": "10.0.0.1"},
}
TEMPLATE = {"id": "tmpl-1", "name": "Switch Template", "sections": []}


def _db_session_factory():
    class _Ctx:
        async def __aenter__(self):
            self._session = TestSessionLocal()
            return self._session

        async def __aexit__(self, *args):
            await self._session.close()

    return lambda: _Ctx()


async def _seed_failed_release_row() -> uuid.UUID:
    """A release-direction L1 row parked FAILED by an earlier transient load failure,
    so the manual retry drives it again (a release needs no fork intent)."""
    async with TestSessionLocal() as s:
        row = L1ConnectionAssignment(
            reservation_id=uuid.UUID(RES_ID),
            switch_device_id=uuid.UUID(SWITCH_ID),
            port_a="0/0/1",
            port_b="0/0/2",
            status="FAILED",
            intended="RELEASED",
            attempts=1,
            last_error="driver load failed: ConnectError",
        )
        s.add(row)
        await s.commit()
        return row.id


async def _post_retry(stack: ExitStack) -> httpx.Response:
    from app.main import app
    from app.routers import executions as ex_router

    stack.enter_context(patch.object(ex_router, "make_session_ctx_factory", _db_session_factory))
    async with RealAsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await asyncio.wait_for(
            client.post(
                f"/internal/reservations/{RES_ID}/wiring/retry",
                headers={"X-Internal-Token": "test-token"},
            ),
            HANG_GUARD_SECONDS,
        )


async def test_wiring_retry_with_a_stalled_download_returns_the_transient_outcome(tmp_path):
    """POST /internal/reservations/{id}/wiring/retry while inventory drips the driver
    package: the call returns within the deadline, the row is still FAILED with the
    transient load text and one more attempt, still retryable, and no driver call ran."""
    row_id = await _seed_failed_release_row()
    peer = DripPeer()
    cache_root = tmp_path / "cache"
    cache_root.mkdir()
    driver_calls: list[str] = []

    with ExitStack() as stack:
        stack.enter_context(
            patch.object(driver_loader, "DRIVER_DOWNLOAD_DEADLINE_SECONDS", SHORT_DEADLINE)
        )
        mock_settings = stack.enter_context(patch("app.services.driver_loader.settings"))
        mock_settings.allow_driver_pip_install = False
        mock_settings.driver_cache_path = str(cache_root)
        mock_settings.inventory_service_url = "http://inventory"
        mock_settings.internal_api_token = "token"
        stack.enter_context(
            patch(
                "app.services.nats_consumer._fetch_device",
                new=AsyncMock(return_value=SWITCH),
            )
        )
        stack.enter_context(
            patch(
                "app.services.nats_consumer._fetch_template", new=AsyncMock(return_value=TEMPLATE)
            )
        )
        stack.enter_context(
            patch(
                "app.services.nats_consumer._fetch_fork_intended_wires",
                new=AsyncMock(return_value=[]),
            )
        )
        stack.enter_context(
            patch(
                "app.services.driver_sandbox.execute_driver_method",
                side_effect=lambda path, action, *a, **k: driver_calls.append(action),
            )
        )
        started = time.monotonic()
        # The ASGI client is built from the real class captured at import, before
        # httpx.AsyncClient is replaced for the download below.
        stack.enter_context(
            patch("app.services.driver_loader.httpx.AsyncClient", side_effect=peer.client)
        )
        resp = await _post_retry(stack)
        elapsed = time.monotonic() - started

    assert resp.status_code == 200, resp.text
    (result,) = resp.json()["results"]
    assert result["id"] == str(row_id)
    assert (result["outcome"], result["status"], result["attempts"], result["last_error"]) == (
        "still_failed",
        "FAILED",
        2,
        "driver load failed: TimeoutError",
    )
    assert is_retryable_failure(result["last_error"])
    assert driver_calls == []
    assert peer.sent >= 2, "the download was live when the deadline cut it"
    assert elapsed < ELAPSED_BOUND_SECONDS
    assert list(cache_root.iterdir()) == []


async def test_wiring_retry_with_a_stalled_switch_read_answers_503():
    """The same route when the switch's device read stalls: the deadline raises the
    transient error, which the manual route maps to 503, the row untouched."""
    await _seed_failed_release_row()
    peer = DripPeer()
    with ExitStack() as stack:
        stack.enter_context(
            patch.object(nats_consumer, "INTERNAL_READ_DEADLINE_SECONDS", SHORT_DEADLINE)
        )
        stack.enter_context(
            patch(
                "app.services.nats_consumer._fetch_fork_intended_wires",
                new=AsyncMock(return_value=[]),
            )
        )
        stack.enter_context(
            patch("app.services.nats_consumer.httpx.AsyncClient", side_effect=peer.client)
        )
        resp = await _post_retry(stack)

    assert resp.status_code == 503
    detail = resp.json()["detail"]
    assert (
        detail
        == f"Upstream service unavailable: fetch device {SWITCH_ID}: transport error: TimeoutError"
    )
    async with TestSessionLocal() as s:
        (row,) = (await s.execute(select(L1ConnectionAssignment))).scalars().all()
    assert (row.status, row.attempts, row.last_error) == (
        "FAILED",
        1,
        "driver load failed: ConnectError",
    )
