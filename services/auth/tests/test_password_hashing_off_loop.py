"""bcrypt runs off the event loop, on a bounded pool of its own (IAM-HASH-7).

The invariant: while any number of logins or registrations are hashing, the event
loop stays free, so every other auth route keeps answering; at most
`hash_threads` bcrypt computations run at once, never on anyio's shared default
thread pool; and a login that cannot get a hashing slot in time is answered 503
with `Retry-After`, never queued without bound and never a 401.

The loop proofs are structural, not latency thresholds: a heartbeat task sleeps
10 ms at a time while the logins run, and its longest gap must stay well under
the duration of ONE hash. A check on the loop thread holds the loop for a whole
hash, so the gap is then at least one hash long whatever the host's speed. Two
forms: one replaces bcrypt with a GIL-free 0.3 s sleep (a fixed duration, so the
bound does not depend on the CPU), and one uses real bcrypt with the bound taken
from a hash timed on the same host just before (so a slow one-core runner and a
fast host both scale it). Run pinned to one core with `taskset -c 0` too.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time

import anyio
import anyio.to_thread
import bcrypt
import pytest
from app.config import settings
from app.routers.auth import HASHING_BUSY_DETAIL
from app.services import auth_service, passwords
from app.services.auth_service import create_tokens_for_user, create_user
from app.services.passwords import HashingBusyError, HashingPool
from sqlalchemy import func, select

from tests._harness import TestSessionLocal

PASSWORD = "right-password-1"
STAND_IN_SECONDS = 0.3


@pytest.fixture
async def client(make_client):
    async with make_client() as ac:
        yield ac


@pytest.fixture
def pool(monkeypatch):
    """Install a hashing pool of `threads` threads for the test."""

    def _install(threads: int, **kwargs) -> HashingPool:
        installed = HashingPool(threads, **kwargs)
        passwords.set_hashing_pool(installed)
        return installed

    return _install


@pytest.fixture
def slow_bcrypt(monkeypatch) -> dict[str, int]:
    """bcrypt's two computations replaced by a 0.3 s sleep that releases the GIL,
    as bcrypt does; records the most that ran at once and on which threads."""
    state = {"running": 0, "peak": 0, "calls": 0}
    threads: set[int] = set()
    lock = threading.Lock()

    def _slow(result):
        with lock:
            state["running"] += 1
            state["calls"] += 1
            state["peak"] = max(state["peak"], state["running"])
            threads.add(threading.get_ident())
        time.sleep(STAND_IN_SECONDS)
        with lock:
            state["running"] -= 1
        return result

    real_hashpw = bcrypt.hashpw
    monkeypatch.setattr(passwords.bcrypt, "checkpw", lambda secret, hashed: _slow(False))
    monkeypatch.setattr(
        passwords.bcrypt, "hashpw", lambda secret, salt: _slow(real_hashpw(secret, salt))
    )
    state["threads"] = threads  # type: ignore[assignment]
    return state


class Heartbeat:
    """Sleeps 10 ms at a time on the event loop and records the longest gap."""

    def __init__(self) -> None:
        self.max_gap = 0.0
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None

    async def _run(self) -> None:
        last = time.perf_counter()
        while not self._stop.is_set():
            await asyncio.sleep(0.01)
            now = time.perf_counter()
            self.max_gap = max(self.max_gap, now - last)
            last = now

    async def __aenter__(self) -> Heartbeat:
        self._task = asyncio.create_task(self._run())
        await asyncio.sleep(0)
        return self

    async def __aexit__(self, *exc) -> None:
        self._stop.set()
        await self._task


async def _account(email="known@example.com") -> None:
    async with TestSessionLocal() as db:
        await create_user(db, email, email.split("@")[0], PASSWORD)


def _login(n: int) -> dict:
    return {"email": f"nobody{n}@example.com", "password": "wrong-password"}


# -- 1. The pool --------------------------------------------------------------------


@pytest.mark.parametrize(
    "cpus, expected", [(1, 1), (2, 1), (3, 1), (4, 2), (6, 3), (8, 4), (32, 4), (128, 4)]
)
def test_the_automatic_thread_count_is_half_the_cpus_between_1_and_4(cpus, expected):
    assert passwords.auto_hash_threads(cpus) == expected
    assert passwords.effective_hash_threads(0, cpus) == expected
    assert passwords.effective_hash_threads(-3, cpus) == expected


def test_a_configured_thread_count_wins_up_to_the_available_cpus(caplog):
    assert passwords.effective_hash_threads(1, 64) == 1
    assert passwords.effective_hash_threads(6, 8) == 6
    assert passwords.effective_hash_threads(8, 8) == 8
    assert not caplog.records
    with caplog.at_level(logging.WARNING, logger="app.services.passwords"):
        assert passwords.effective_hash_threads(16, 1) == 1
        assert passwords.effective_hash_threads(6, 4) == 4
    lowered = [r for r in caplog.records if r.action == "password_hash_threads_lowered"]
    assert [(r.configured, r.cpus) for r in lowered] == [(16, 1), (6, 4)]


def _cgroup(tmp_path, files: dict[str, str], own: str | None = None):
    for relative, text in files.items():
        path = tmp_path / "cg" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    proc = tmp_path / "proc_self_cgroup"
    if own is not None:
        proc.write_text(own)
    return passwords.cgroup_cpu_limit(tmp_path / "cg", proc)


@pytest.mark.parametrize(
    "cpu_max, expected",
    [
        ("max 100000", None),
        ("100000 100000", 1),
        ("50000 100000", 1),
        ("250000 100000", 3),
        ("400000 100000", 4),
        ("garbage", None),
        ("abc 100000", None),
        ("", None),
        ("0 100000", None),
    ],
)
def test_the_cgroup_v2_quota_is_read_rounded_up(tmp_path, cpu_max, expected):
    assert _cgroup(tmp_path, {"cpu.max": cpu_max}) == expected


def test_no_cgroup_files_means_no_quota(tmp_path):
    assert _cgroup(tmp_path, {}) is None


def test_the_processes_own_cgroup_is_read_first(tmp_path):
    files = {"cpu.max": "max 100000", "kubepods/pod1/cpu.max": "150000 100000"}
    assert _cgroup(tmp_path, files, own="0::/kubepods/pod1\n") == 2
    # A cgroup namespace shows the container's own cgroup as "/": the root is read.
    assert _cgroup(tmp_path, {"cpu.max": "200000 100000"}, own="0::/\n") == 2


@pytest.mark.parametrize(
    "quota, period, expected",
    [("-1", "100000", None), ("50000", "100000", 1), ("250000", "100000", 3), ("x", "y", None)],
)
def test_the_cgroup_v1_quota_is_read(tmp_path, quota, period, expected):
    files = {"cpu/cpu.cfs_quota_us": quota, "cpu/cpu.cfs_period_us": period}
    assert _cgroup(tmp_path, files) == expected
    other = tmp_path / "other"
    other.mkdir()
    files = {"cpu,cpuacct/cpu.cfs_quota_us": quota, "cpu,cpuacct/cpu.cfs_period_us": period}
    assert _cgroup(other, files) == expected


def test_available_cpus_is_the_affinity_lowered_to_the_quota(monkeypatch):
    monkeypatch.setattr(passwords.os, "sched_getaffinity", lambda pid: set(range(8)))
    monkeypatch.setattr(passwords, "cgroup_cpu_limit", lambda: None)
    assert passwords.available_cpus() == 8
    monkeypatch.setattr(passwords, "cgroup_cpu_limit", lambda: 2)
    assert passwords.available_cpus() == 2
    monkeypatch.setattr(passwords, "cgroup_cpu_limit", lambda: 32)
    assert passwords.available_cpus() == 8


def test_a_quota_lowers_the_automatic_and_a_configured_count(monkeypatch):
    monkeypatch.setattr(passwords.os, "sched_getaffinity", lambda pid: set(range(32)))
    monkeypatch.setattr(passwords, "cgroup_cpu_limit", lambda: 1)
    monkeypatch.setattr(settings, "auth_password_hash_threads", 0)
    passwords.set_hashing_pool(None)
    assert passwords.hashing_pool().threads == 1
    monkeypatch.setattr(settings, "auth_password_hash_threads", 16)
    passwords.set_hashing_pool(None)
    assert passwords.hashing_pool().threads == 1


def test_the_pool_is_built_from_the_setting(monkeypatch):
    monkeypatch.setattr(passwords, "available_cpus", lambda: 8)
    monkeypatch.setattr(settings, "auth_password_hash_threads", 3)
    passwords.set_hashing_pool(None)
    built = passwords.hashing_pool()
    assert built.threads == 3
    assert built.max_waiting == 3 * passwords.HASH_MAX_WAITING_PER_THREAD
    assert built.wait_seconds == passwords.HASH_WAIT_SECONDS == 5.0
    assert passwords.HASH_RETRY_AFTER_SECONDS == 5
    assert passwords.hashing_pool() is built


def test_the_default_setting_is_automatic():
    assert settings.auth_password_hash_threads == 0


def test_a_pool_needs_a_thread():
    with pytest.raises(ValueError):
        HashingPool(0)


async def test_at_most_threads_computations_run_at_once_never_on_the_default_pool(
    pool, slow_bcrypt
):
    installed = pool(2)
    default_limiter = anyio.to_thread.current_default_thread_limiter()
    seen_default: list[float] = []
    loop_thread = threading.get_ident()

    async def one() -> None:
        async with passwords.hashing_slot():
            seen_default.append(default_limiter.borrowed_tokens)
            await passwords.run_hashing(passwords.verify_password, "x", passwords.DUMMY_HASH)

    started = time.perf_counter()
    await asyncio.gather(*(one() for _ in range(6)))
    elapsed = time.perf_counter() - started
    assert slow_bcrypt["calls"] == 6
    assert slow_bcrypt["peak"] == 2
    assert loop_thread not in slow_bcrypt["threads"]
    assert set(seen_default) == {0}
    assert installed.busy_slots() == installed.busy_threads() == 0
    assert elapsed >= 3 * STAND_IN_SECONDS * 0.9  # six sleeps, two at a time


async def test_the_worker_limiter_binds_without_a_slot(pool, slow_bcrypt):
    """The startup seed calls run_hashing without a slot; it still never runs
    more than `threads` computations at once."""
    pool(1)
    await asyncio.gather(
        *(
            passwords.run_hashing(passwords.verify_password, "x", passwords.DUMMY_HASH)
            for _ in range(3)
        )
    )
    assert slow_bcrypt["peak"] == 1


async def test_a_slot_that_does_not_come_free_in_time_is_busy(pool):
    installed = pool(1, wait_seconds=0.2)
    async with installed.slot():
        started = time.perf_counter()
        with pytest.raises(HashingBusyError):
            async with installed.slot():
                pass
        waited = time.perf_counter() - started
    assert 0.15 <= waited < 2.0
    async with installed.slot():  # the holder released it, and the timed-out waiter took none
        assert installed.busy_slots() == 1
    assert installed.busy_slots() == 0


async def test_past_the_waiting_bound_a_request_is_busy_at_once(pool):
    installed = pool(1, wait_seconds=30, max_waiting=1)
    async with installed.slot():
        waiter = asyncio.create_task(installed.slot().__aenter__())
        await asyncio.sleep(0.05)
        started = time.perf_counter()
        with pytest.raises(HashingBusyError):
            async with installed.slot():
                pass
        assert time.perf_counter() - started < 1.0
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
    assert installed.busy_slots() == 0


async def test_a_slot_is_released_when_its_section_raises(pool):
    installed = pool(1)
    with pytest.raises(RuntimeError):
        async with installed.slot():
            raise RuntimeError("boom")
    assert installed.busy_slots() == 0


# -- 2. The event loop stays free -----------------------------------------------------


async def test_concurrent_logins_never_block_the_event_loop(client, pool, slow_bcrypt):
    """Four logins, each one 0.3 s computation, two threads: a heartbeat on the
    loop never waits anywhere near one computation, and a cheap route answers
    while they run. With the check on the loop thread the gap is at least 0.3 s."""
    pool(2)
    async with Heartbeat() as heartbeat:
        logins = [asyncio.create_task(client.post("/login", json=_login(n))) for n in range(4)]
        await asyncio.sleep(0.05)
        probe_started = time.perf_counter()
        options = await client.get("/login-options")
        probe = time.perf_counter() - probe_started
        answers = await asyncio.gather(*logins)
    assert [a.status_code for a in answers] == [401] * 4
    assert slow_bcrypt["calls"] == 4
    assert options.status_code == 200
    assert probe < STAND_IN_SECONDS / 2
    assert heartbeat.max_gap < STAND_IN_SECONDS / 2


async def test_a_registration_never_blocks_the_event_loop(client, pool, slow_bcrypt):
    pool(1)
    async with Heartbeat() as heartbeat:
        response = await client.post(
            "/register",
            json={"email": "new@example.com", "username": "newuser", "password": PASSWORD},
        )
    assert response.status_code == 201
    assert slow_bcrypt["calls"] == 1
    assert heartbeat.max_gap < STAND_IN_SECONDS / 2


async def test_the_break_glass_login_never_blocks_the_event_loop(
    client, pool, slow_bcrypt, monkeypatch
):
    monkeypatch.setattr(settings, "auth_method", "oidc")
    for switch in (False, True):
        monkeypatch.setattr(settings, "oidc_break_glass_local_superadmin", switch)
        pool(1)
        slow_bcrypt["calls"] = 0
        async with Heartbeat() as heartbeat:
            response = await client.post("/login", json=_login(1))
        assert response.status_code == 401
        assert slow_bcrypt["calls"] == 1
        assert heartbeat.max_gap < STAND_IN_SECONDS / 2


async def test_real_bcrypt_releases_the_event_loop(client, pool):
    """The same proof with the real computation: bcrypt must release the GIL in
    its worker thread. The bound is half of one cost-12 check timed on this host
    just before, so it scales with the host's speed."""
    started = time.perf_counter()
    bcrypt.checkpw(b"x", passwords.DUMMY_HASH.encode())
    one_hash = time.perf_counter() - started
    pool(1)
    async with Heartbeat() as heartbeat:
        answers = await asyncio.gather(*(client.post("/login", json=_login(n)) for n in range(3)))
    assert [a.status_code for a in answers] == [401] * 3
    assert heartbeat.max_gap < one_hash / 2, (heartbeat.max_gap, one_hash)


async def test_another_route_answers_while_every_slot_is_taken(client, pool):
    """`GET /me` (a query and a token check) is untouched by a full pool."""
    async with TestSessionLocal() as db:
        user = await create_user(db, "me@example.com", "meuser", PASSWORD)
        access_token, _ = await create_tokens_for_user(db, user)
    installed = pool(1, wait_seconds=0.5)
    async with installed.slot():
        response = await client.get("/me", headers={"Authorization": f"Bearer {access_token}"})
    assert response.status_code == 200


# -- 3. Saturation: 503 with Retry-After ------------------------------------------------


@pytest.fixture
def cost_spy(monkeypatch) -> dict[str, int]:
    calls = {"checkpw": 0, "hashpw": 0, "query": 0}
    real_by_email = auth_service.get_user_by_email

    def checkpw(secret, hashed):
        calls["checkpw"] += 1
        return False

    def hashpw(secret, salt):
        calls["hashpw"] += 1
        return b"$2b$12$" + b"a" * 53

    async def by_email(db, email):
        calls["query"] += 1
        return await real_by_email(db, email)

    monkeypatch.setattr(passwords.bcrypt, "checkpw", checkpw)
    monkeypatch.setattr(passwords.bcrypt, "hashpw", hashpw)
    monkeypatch.setattr(auth_service, "get_user_by_email", by_email)
    return calls


def _busy_shape(response) -> tuple:
    return (
        response.status_code,
        response.json(),
        response.headers.get("retry-after"),
        response.headers.get("www-authenticate"),
    )


BUSY = (503, {"detail": HASHING_BUSY_DETAIL}, "5", None)


@pytest.mark.parametrize("mode", ["local", "oidc-off", "oidc-on"])
async def test_a_login_with_no_free_slot_is_503_with_retry_after_and_checks_nothing(
    mode, client, pool, cost_spy, monkeypatch
):
    await _account()
    monkeypatch.setattr(settings, "auth_method", "oidc" if mode.startswith("oidc") else mode)
    monkeypatch.setattr(settings, "oidc_break_glass_local_superadmin", mode == "oidc-on")
    for key in cost_spy:
        cost_spy[key] = 0
    installed = pool(1, wait_seconds=0.05)
    async with installed.slot():
        known = await client.post("/login", json={"email": "known@example.com", "password": "x"})
        unknown = await client.post("/login", json=_login(1))
    assert _busy_shape(known) == _busy_shape(unknown) == BUSY
    assert HASHING_BUSY_DETAIL == "Too many password checks in progress; try again shortly"
    # The slot comes before the account query: a waiting login holds no
    # database connection and checks nothing.
    assert cost_spy == {"checkpw": 0, "hashpw": 0, "query": 0}


async def test_a_registration_with_no_free_slot_is_503_and_creates_nothing(client, pool, cost_spy):
    installed = pool(1, wait_seconds=0.05)
    async with installed.slot():
        response = await client.post(
            "/register",
            json={"email": "new@example.com", "username": "newuser", "password": PASSWORD},
        )
    assert _busy_shape(response) == BUSY
    assert cost_spy["hashpw"] == 0
    async with TestSessionLocal() as db:
        from app.models.user import User

        assert (await db.execute(select(func.count()).select_from(User))).scalar_one() == 0


async def test_ldap_mode_takes_no_hashing_slot(client, pool, monkeypatch):
    from unittest.mock import AsyncMock

    from app.services import ldap_service

    monkeypatch.setattr(settings, "auth_method", "ldap")
    monkeypatch.setattr(ldap_service, "bind_user", AsyncMock(return_value=None))
    installed = pool(1, wait_seconds=0.05)
    async with installed.slot():
        response = await client.post("/login", json={"email": "alice", "password": "pw"})
    assert response.status_code == 401


async def test_a_login_waits_for_a_slot_within_the_bound_and_then_signs_in(client, pool):
    await _account()
    installed = pool(1, wait_seconds=10)

    async def hold() -> None:
        async with installed.slot():
            await asyncio.sleep(0.3)

    holder = asyncio.create_task(hold())
    await asyncio.sleep(0.05)
    response = await client.post(
        "/login", json={"email": "known@example.com", "password": PASSWORD}
    )
    await holder
    assert response.status_code == 200
