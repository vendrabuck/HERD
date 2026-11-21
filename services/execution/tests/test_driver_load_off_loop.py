"""The config-schema extraction on a cache miss runs off the event loop (issue #1498).

``load_driver`` used to call ``extract_config_schema_json`` (a synchronous sandbox
``subprocess.run``, up to the status timeout, plus a ``pip install`` of up to 60
seconds when ALLOW_DRIVER_PIP_INSTALL is on) directly on the event loop, so no other
coroutine in the process ran until it returned: not the consumer's heartbeat, the
retry tick, the health scheduler, or an HTTP request. It now runs on a worker thread
through ``_SchemaExtraction``.

What moving it to a thread changes, and what these tests pin:

- The loop keeps running while the extraction runs (the ticker test).
- Two first loads of one driver now extract at the same time, each in its own
  directory, and both still succeed with one cache row (the overlap test).
- A coroutine cancelled while the worker runs cannot stop the worker, which may still
  write into the attempt's directory (pip's ``_deps``); the directory must be removed
  once, after the worker is done, and never left behind (the cancellation tests and the
  state-machine tests of ``_SchemaExtraction``).

No test here waits on wall-clock time for its verdict: each blocking stub waits on a
threading primitive with a generous timeout that only a regression reaches.
"""

import asyncio
import io
import os
import threading
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager, suppress
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from app.database import Base
from app.models.driver_cache import DriverCache
from app.services import driver_loader
from app.services.driver_loader import _SchemaExtraction, load_driver
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

# Long enough that only a regression waits it out; short enough that one fails fast.
STUB_WAIT_SECONDS = 5

VALID_L1_DRIVER = """
class Driver:
    def __init__(self, context):
        self.context = context
    def login(self): return {"success": True}
    def logout(self): return {"success": True}
    def connect_ports(self, a, b): return {"success": True}
    def disconnect_ports(self, a, b): return {"success": True}
    def status(self): return {"reachable": True}
"""


def _make_zip(driver_code: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("driver.py", driver_code)
    return buf.getvalue()


@pytest.fixture
async def sessions(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'cache.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest.fixture
def cache_root(tmp_path):
    root = tmp_path / "cache"
    root.mkdir()
    return root


@contextmanager
def _cold_load(cache_root: Path, extraction):
    """A cold-cache load of a valid L1 package with the schema extraction replaced."""
    with ExitStack() as stack:
        mock_settings = stack.enter_context(patch("app.services.driver_loader.settings"))
        mock_settings.driver_cache_path = str(cache_root)
        mock_settings.inventory_service_url = "http://test"
        mock_settings.internal_api_token = "token"
        stack.enter_context(
            patch(
                "app.services.driver_loader.download_driver_package",
                new=AsyncMock(return_value=_make_zip(VALID_L1_DRIVER)),
            )
        )
        stack.enter_context(
            patch("app.services.driver_loader.extract_config_schema_json", side_effect=extraction)
        )
        yield


async def _load(sessions, driver_id, cache_root, extraction):
    with _cold_load(cache_root, extraction):
        async with sessions() as session:
            return await load_driver(session, driver_id, "sha-1", "driver.zip", "Layer 1 Switch")


async def _rows(sessions, driver_id):
    async with sessions() as session:
        result = await session.execute(
            select(DriverCache).where(DriverCache.driver_id == driver_id)
        )
        return result.scalars().all()


async def _wait_until_empty(root: Path) -> bool:
    """True once root holds nothing; False if something is still there after the wait.

    The worker removes the directory in its own `finally`, after the stub returns, so
    the test cannot join it directly; it polls instead, and a regression that leaves a
    directory behind exhausts the wait."""
    for _ in range(STUB_WAIT_SECONDS * 100):
        if not any(root.iterdir()):
            return True
        await asyncio.sleep(0.01)
    return False


# --- the loop keeps running -------------------------------------------------


async def test_load_driver_keeps_the_event_loop_running_during_schema_extraction(
    sessions, cache_root
):
    """The stub blocks until a second coroutine has ticked three times. On the loop
    (the code before #1498) the ticker cannot run, the wait times out, and the stub
    records False; off the loop the ticks arrive and the stub records True."""
    loop_ticked = threading.Event()
    seen: list[tuple[bool, bool]] = []

    def slow_extraction(driver_dir):
        released = loop_ticked.wait(timeout=STUB_WAIT_SECONDS)
        seen.append((released, threading.current_thread() is threading.main_thread()))
        return None

    async def ticker():
        ticks = 0
        while True:
            await asyncio.sleep(0.01)
            ticks += 1
            if ticks >= 3:
                loop_ticked.set()

    tick_task = asyncio.create_task(ticker())
    try:
        driver_id = uuid.uuid4()
        path = await _load(sessions, driver_id, cache_root, slow_extraction)
    finally:
        tick_task.cancel()
        with suppress(asyncio.CancelledError):
            await tick_task

    assert seen == [(True, False)], "the loop stalled while the extraction ran"
    assert Path(path).is_dir()
    assert [r.local_path for r in await _rows(sessions, driver_id)] == [path]


# --- two first loads at once ------------------------------------------------


async def test_two_first_loads_extract_concurrently_and_both_succeed(sessions, cache_root):
    """Both extractions must be inside the stub at once (a two-party barrier) before
    either returns, which only happens off the loop; both loads then succeed with ONE
    cache row, the same directory, and the loser's extraction removed (CFG-LOAD-6)."""
    both_inside = threading.Barrier(2, timeout=STUB_WAIT_SECONDS)
    extracted_dirs: list[Path] = []

    def overlapping_extraction(driver_dir):
        extracted_dirs.append(Path(driver_dir))
        both_inside.wait()
        return '{"type": "object"}'

    driver_id = uuid.uuid4()

    async def one_load():
        async with sessions() as session:
            return await load_driver(session, driver_id, "sha-1", "driver.zip", "Layer 1 Switch")

    with _cold_load(cache_root, overlapping_extraction):
        first, second = await asyncio.wait_for(
            asyncio.gather(one_load(), one_load()), STUB_WAIT_SECONDS * 2
        )

    assert len(set(extracted_dirs)) == 2, "each load extracts into its own directory"
    assert first == second
    assert [p.name for p in cache_root.iterdir()] == [Path(first).name]
    rows = await _rows(sessions, driver_id)
    assert [(r.local_path, r.config_schema_json) for r in rows] == [(first, '{"type": "object"}')]


# --- cancellation -----------------------------------------------------------


async def test_cancel_while_the_worker_runs_removes_the_directory_after_the_worker(
    sessions, cache_root
):
    """The load is cancelled while the stub is inside the worker. The stub then
    writes `_deps` into the attempt's directory, as pip does. Removing the directory
    at the cancellation (a plain to_thread with the old `except BaseException` rmtree)
    lets that write recreate it, and nothing ever removes it; the handoff removes it
    after the worker is done."""
    entered = threading.Event()
    release = threading.Event()
    seen_dir: list[Path] = []

    def extraction_that_writes_late(driver_dir):
        seen_dir.append(Path(driver_dir))
        entered.set()
        release.wait(timeout=STUB_WAIT_SECONDS)
        deps = Path(driver_dir) / "_deps"
        os.makedirs(deps, exist_ok=True)
        (deps / "installed.txt").write_text("pip wrote this after the cancel")
        return None

    driver_id = uuid.uuid4()
    load_task = asyncio.create_task(
        _load(sessions, driver_id, cache_root, extraction_that_writes_late)
    )
    assert await asyncio.to_thread(entered.wait, STUB_WAIT_SECONDS)
    load_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await load_task
    # The worker is still running: its directory has not been pulled from under it.
    assert seen_dir[0].is_dir()

    release.set()
    assert await _wait_until_empty(cache_root), sorted(
        str(p.relative_to(cache_root)) for p in cache_root.rglob("*")
    )
    assert await _rows(sessions, driver_id) == []


async def test_cancel_while_queued_removes_the_directory_and_never_runs_the_extraction(
    sessions, cache_root
):
    """The loop's default executor has one worker, held busy, so the extraction job
    waits in its queue. Cancelling the load cancels the queued job: the extraction
    never runs, so the coroutine itself must remove the directory at once."""
    executor = ThreadPoolExecutor(max_workers=1)
    loop = asyncio.get_running_loop()
    loop.set_default_executor(executor)
    blocker_release = threading.Event()
    executor.submit(blocker_release.wait, STUB_WAIT_SECONDS)

    calls: list[Path] = []
    queued = asyncio.Event()
    real_to_thread = asyncio.to_thread

    async def recording_to_thread(func, *args, **kwargs):
        queued.set()
        return await real_to_thread(func, *args, **kwargs)

    driver_id = uuid.uuid4()
    try:
        with patch("app.services.driver_loader.asyncio.to_thread", new=recording_to_thread):
            load_task = asyncio.create_task(
                _load(sessions, driver_id, cache_root, lambda d: calls.append(Path(d)))
            )
            # to_thread submits the job synchronously, before its first await, so
            # once the task has set `queued` and yielded, the job sits behind the
            # blocker in the executor's queue.
            await asyncio.wait_for(queued.wait(), STUB_WAIT_SECONDS)
            load_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await load_task
        assert list(cache_root.iterdir()) == []
    finally:
        blocker_release.set()
        executor.shutdown(wait=True)

    assert calls == [], "a cancelled queued extraction never runs"
    assert list(cache_root.iterdir()) == []
    assert await _rows(sessions, driver_id) == []


async def test_an_extraction_that_raises_removes_the_directory_and_reraises(sessions, cache_root):
    """Exception propagation across the thread: the worker's exception reaches the
    caller unchanged (so a transient failure stays transient), and the directory is
    removed."""
    boom = RuntimeError("schema extraction crashed")

    def raising_extraction(driver_dir):
        raise boom

    with pytest.raises(RuntimeError) as exc:
        await _load(sessions, uuid.uuid4(), cache_root, raising_extraction)
    assert exc.value is boom
    assert list(cache_root.iterdir()) == []


# --- the handoff's state machine, every ordering ----------------------------


def _attempt_dir(tmp_path) -> Path:
    d = tmp_path / f"drv-{uuid.uuid4().hex}"
    d.mkdir()
    (d / "driver.py").write_text(VALID_L1_DRIVER)
    return d


def test_handoff_completed_unabandoned_keeps_the_directory(tmp_path):
    d = _attempt_dir(tmp_path)
    with patch.object(driver_loader, "extract_config_schema_json", return_value="{}"):
        assert _SchemaExtraction(d).run() == "{}"
    assert d.is_dir()


def test_handoff_abandon_after_the_worker_finished_removes_the_directory(tmp_path):
    d = _attempt_dir(tmp_path)
    handoff = _SchemaExtraction(d)
    with patch.object(driver_loader, "extract_config_schema_json", return_value=None):
        handoff.run()
    assert d.is_dir()
    handoff.abandon()
    assert not d.exists()


def test_handoff_abandon_before_the_worker_starts_removes_it_and_skips_the_extraction(tmp_path):
    d = _attempt_dir(tmp_path)
    handoff = _SchemaExtraction(d)
    handoff.abandon()
    assert not d.exists()
    with patch.object(driver_loader, "extract_config_schema_json") as extraction:
        assert handoff.run() is None
    extraction.assert_not_called()
    assert not d.exists()


def test_handoff_abandon_while_the_worker_runs_leaves_removal_to_the_worker(tmp_path):
    d = _attempt_dir(tmp_path)
    handoff = _SchemaExtraction(d)
    during: list[bool] = []

    def extraction(driver_dir):
        handoff.abandon()
        during.append(Path(driver_dir).is_dir())
        os.makedirs(Path(driver_dir) / "_deps", exist_ok=True)
        return None

    with patch.object(driver_loader, "extract_config_schema_json", side_effect=extraction):
        handoff.run()
    assert during == [True], "abandon must not remove the directory under a running worker"
    assert not d.exists()


def test_handoff_worker_exception_still_marks_it_done(tmp_path):
    """A raising extraction still records DONE, so a later abandon removes the
    directory instead of waiting on a worker that has already left."""
    d = _attempt_dir(tmp_path)
    handoff = _SchemaExtraction(d)
    with patch.object(driver_loader, "extract_config_schema_json", side_effect=ValueError("x")):
        with pytest.raises(ValueError):
            handoff.run()
    assert d.is_dir()
    handoff.abandon()
    assert not d.exists()
