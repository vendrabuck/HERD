"""Attempt limits on the config page login (app/login_limits.py).

Unit tests drive a LoginLimiter on a fake clock; the API tests go through
POST /login with a limiter swapped into app.login_limits.LIMITER.
"""

import ast
import logging
import sys
from pathlib import Path

import app.login_limits as login_limits
import pytest
from app.login_limits import (
    LOCKED_ACTION,
    LoginLimiter,
    client_source,
    source_delay_seconds,
)
from app.main import LOGIN_LOCKED_DETAIL, app
from httpx import ASGITransport, AsyncClient

CFG_PASSWORD = "test-config-pass"
WRONG = "not-the-password"


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def limiter(clock):
    return LoginLimiter(max_attempts=20, lockout_seconds=300, clock=clock)


@pytest.fixture
def api_limiter(monkeypatch, clock):
    lim = LoginLimiter(max_attempts=20, lockout_seconds=300, clock=clock)
    monkeypatch.setattr(login_limits, "LIMITER", lim)
    return lim


@pytest.fixture
def async_client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


# -- delay schedule --


def test_source_delay_schedule_starts_after_the_third_failure_and_caps_at_60():
    assert [source_delay_seconds(n) for n in range(1, 12)] == [
        0,
        0,
        1,
        2,
        4,
        8,
        16,
        32,
        60,
        60,
        60,
    ]
    assert source_delay_seconds(10_000) == 60


def test_each_failure_sets_the_scheduled_wait(limiter, clock):
    observed = []
    for _ in range(10):
        limiter.record_failure("198.51.100.7")
        observed.append(limiter.retry_after("198.51.100.7"))
        # Let the wait run out before the next attempt, as a patient caller would.
        clock.advance(61)
    assert observed == [None, None, 1, 2, 4, 8, 16, 32, 60, 60]


def test_wait_counts_down_and_expires(limiter, clock):
    for _ in range(4):
        limiter.record_failure("198.51.100.7")
    assert limiter.retry_after("198.51.100.7") == 2
    clock.advance(0.5)
    assert limiter.retry_after("198.51.100.7") == 2  # rounded up
    clock.advance(1.0)
    assert limiter.retry_after("198.51.100.7") == 1
    clock.advance(0.5)
    assert limiter.retry_after("198.51.100.7") is None


def test_success_resets_the_source(limiter, clock):
    for _ in range(5):
        limiter.record_failure("198.51.100.7")
    clock.advance(60)
    limiter.record_success("198.51.100.7")
    limiter.record_failure("198.51.100.7")
    limiter.record_failure("198.51.100.7")
    assert limiter.retry_after("198.51.100.7") is None
    limiter.record_failure("198.51.100.7")
    assert limiter.retry_after("198.51.100.7") == 1


def test_sources_are_isolated(limiter):
    for _ in range(5):
        limiter.record_failure("198.51.100.7")
    assert limiter.retry_after("198.51.100.7") == 4
    assert limiter.retry_after("203.0.113.9") is None


def test_a_quiet_source_starts_over(limiter, clock):
    for _ in range(5):
        limiter.record_failure("198.51.100.7")
    clock.advance(login_limits.SOURCE_IDLE_SECONDS)
    limiter.record_failure("198.51.100.7")
    assert limiter.retry_after("198.51.100.7") is None


# -- cross-source lockout --


def test_global_lockout_after_max_attempts_across_sources(clock):
    lim = LoginLimiter(max_attempts=5, lockout_seconds=300, clock=clock)
    for i in range(4):
        lim.record_failure(f"192.0.2.{i}")
    assert lim.retry_after("203.0.113.9") is None
    lim.record_failure("192.0.2.4")
    # Every source waits, including one that never failed.
    assert lim.retry_after("203.0.113.9") == 300
    assert lim.retry_after("192.0.2.0") == 300
    clock.advance(299.5)
    assert lim.retry_after("203.0.113.9") == 1
    clock.advance(0.5)
    assert lim.retry_after("203.0.113.9") is None


def test_global_window_slides(clock):
    lim = LoginLimiter(max_attempts=3, lockout_seconds=100, clock=clock)
    lim.record_failure("192.0.2.1")
    clock.advance(60)
    lim.record_failure("192.0.2.2")
    clock.advance(41)  # the first failure is now outside the window
    lim.record_failure("192.0.2.3")
    assert lim.retry_after("203.0.113.9") is None
    clock.advance(10)
    lim.record_failure("192.0.2.4")
    assert lim.retry_after("203.0.113.9") == 100


def test_success_clears_the_cross_source_count(clock):
    lim = LoginLimiter(max_attempts=3, lockout_seconds=100, clock=clock)
    lim.record_failure("192.0.2.1")
    lim.record_failure("192.0.2.2")
    lim.record_success("203.0.113.9")
    lim.record_failure("192.0.2.3")
    lim.record_failure("192.0.2.4")
    assert lim.retry_after("203.0.113.9") is None


# -- memory bound --


def test_tracked_sources_are_bounded(clock):
    lim = LoginLimiter(max_attempts=1000, lockout_seconds=300, clock=clock, max_sources=3)
    for i in range(10):
        lim.record_failure(f"192.0.2.{i}")
    assert lim.tracked_sources() == 3
    # The longest-quiet sources went first; the latest are still counted.
    assert lim.retry_after("192.0.2.9") is None
    lim.record_failure("192.0.2.9")
    lim.record_failure("192.0.2.9")
    assert lim.retry_after("192.0.2.9") == 1


def test_idle_sources_are_dropped(limiter, clock):
    limiter.record_failure("192.0.2.1")
    limiter.record_failure("192.0.2.2")
    clock.advance(login_limits.SOURCE_IDLE_SECONDS)
    limiter.record_failure("192.0.2.3")
    assert limiter.tracked_sources() == 1


# -- the knobs --


def test_knobs_are_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("CONFIG_LOGIN_MAX_ATTEMPTS", "7")
    monkeypatch.setenv("CONFIG_LOGIN_LOCKOUT_SECONDS", " 45 ")
    lim = LoginLimiter.from_env()
    assert (lim.max_attempts, lim.lockout_seconds) == (7, 45)


def test_knob_defaults(monkeypatch):
    monkeypatch.delenv("CONFIG_LOGIN_MAX_ATTEMPTS", raising=False)
    monkeypatch.setenv("CONFIG_LOGIN_LOCKOUT_SECONDS", "")
    lim = LoginLimiter.from_env()
    assert (lim.max_attempts, lim.lockout_seconds) == (20, 300)


@pytest.mark.parametrize("raw", ["0", "-3", "ten", "1.5"])
def test_a_knob_that_is_not_a_positive_integer_falls_back_to_its_default(monkeypatch, caplog, raw):
    monkeypatch.setenv("CONFIG_LOGIN_MAX_ATTEMPTS", raw)
    monkeypatch.setenv("CONFIG_LOGIN_LOCKOUT_SECONDS", raw)
    with caplog.at_level(logging.WARNING, logger="app.login_limits"):
        lim = LoginLimiter.from_env()
    assert (lim.max_attempts, lim.lockout_seconds) == (20, 300)
    messages = [r.getMessage() for r in caplog.records]
    assert any("CONFIG_LOGIN_MAX_ATTEMPTS" in m for m in messages)
    assert any("CONFIG_LOGIN_LOCKOUT_SECONDS" in m for m in messages)


# -- source address --


def test_source_is_the_first_forwarded_address():
    assert client_source("203.0.113.5, 10.0.0.2", "172.18.0.4") == "203.0.113.5"
    assert client_source(" 2001:db8::1 ", "172.18.0.4") == "2001:db8::1"


def test_source_falls_back_to_the_peer():
    assert client_source(None, "172.18.0.4") == "172.18.0.4"
    assert client_source("", "172.18.0.4") == "172.18.0.4"
    assert client_source("not-an-address", "172.18.0.4") == "172.18.0.4"
    assert client_source(None, None) == "unknown"


# -- the lock log line --


def test_lock_log_line_names_source_scope_and_wait(clock, caplog):
    lim = LoginLimiter(max_attempts=3, lockout_seconds=120, clock=clock)
    with caplog.at_level(logging.WARNING, logger="app.login_limits"):
        lim.record_failure("198.51.100.7")
        lim.record_failure("198.51.100.7")
        lim.record_failure("198.51.100.7")
    locked = [r for r in caplog.records if getattr(r, "action", None) == LOCKED_ACTION]
    assert [(r.scope, r.source, r.failures, r.retry_after_seconds) for r in locked] == [
        ("source", "198.51.100.7", 3, 1),
        ("global", "198.51.100.7", 3, 120),
    ]
    assert locked[0].getMessage() == (
        "config_login_locked scope=source source=198.51.100.7 failures=3 retry_after=1"
    )
    assert locked[1].getMessage() == (
        "config_login_locked scope=global source=198.51.100.7 failures=3 retry_after=120"
    )


# -- through POST /login --


@pytest.mark.asyncio
async def test_login_waits_after_the_third_failure(async_client, api_limiter, clock):
    for _ in range(3):
        resp = await async_client.post("/login", json={"password": WRONG})
        assert resp.status_code == 401
    # Even the right password waits: the check comes before the password.
    locked = await async_client.post("/login", json={"password": CFG_PASSWORD})
    assert locked.status_code == 429
    assert locked.headers["retry-after"] == "1"
    assert locked.json() == {"detail": LOGIN_LOCKED_DETAIL}
    assert LOGIN_LOCKED_DETAIL == "Too many failed login attempts; try again later"
    clock.advance(1)
    ok = await async_client.post("/login", json={"password": CFG_PASSWORD})
    assert ok.status_code == 200
    assert "token" in ok.json()


@pytest.mark.asyncio
async def test_login_success_resets_the_count(async_client, api_limiter):
    for _ in range(2):
        assert (await async_client.post("/login", json={"password": WRONG})).status_code == 401
    assert (await async_client.post("/login", json={"password": CFG_PASSWORD})).status_code == 200
    for _ in range(2):
        assert (await async_client.post("/login", json={"password": WRONG})).status_code == 401
    assert (await async_client.post("/login", json={"password": CFG_PASSWORD})).status_code == 200


@pytest.mark.asyncio
async def test_login_sources_are_keyed_on_the_forwarded_address(async_client, api_limiter):
    first = {"X-Forwarded-For": "203.0.113.5, 172.18.0.2"}
    other = {"X-Forwarded-For": "203.0.113.6"}
    for _ in range(3):
        await async_client.post("/login", json={"password": WRONG}, headers=first)
    assert (
        await async_client.post("/login", json={"password": CFG_PASSWORD}, headers=first)
    ).status_code == 429
    assert (
        await async_client.post("/login", json={"password": CFG_PASSWORD}, headers=other)
    ).status_code == 200
    # No forwarded header: the peer address is its own source.
    assert (await async_client.post("/login", json={"password": CFG_PASSWORD})).status_code == 200


@pytest.mark.asyncio
async def test_login_global_lockout_refuses_every_source(async_client, monkeypatch, clock):
    lim = LoginLimiter(max_attempts=4, lockout_seconds=90, clock=clock)
    monkeypatch.setattr(login_limits, "LIMITER", lim)
    for i in range(4):
        resp = await async_client.post(
            "/login", json={"password": WRONG}, headers={"X-Forwarded-For": f"192.0.2.{i}"}
        )
        assert resp.status_code == 401
    fresh = {"X-Forwarded-For": "203.0.113.200"}
    locked = await async_client.post("/login", json={"password": CFG_PASSWORD}, headers=fresh)
    assert locked.status_code == 429
    assert locked.headers["retry-after"] == "90"
    clock.advance(90)
    ok = await async_client.post("/login", json={"password": CFG_PASSWORD}, headers=fresh)
    assert ok.status_code == 200


@pytest.mark.asyncio
async def test_login_lock_log_never_carries_the_password(async_client, api_limiter, caplog):
    secret_attempt = "pw-attempt-do-not-log-4417"
    with caplog.at_level(logging.DEBUG):
        for _ in range(3):
            await async_client.post(
                "/login",
                json={"password": secret_attempt},
                headers={"X-Forwarded-For": "198.51.100.23"},
            )
    locked = [r for r in caplog.records if getattr(r, "action", None) == LOCKED_ACTION]
    assert len(locked) == 1
    assert locked[0].source == "198.51.100.23"
    for record in caplog.records:
        assert secret_attempt not in record.getMessage()
        assert secret_attempt not in repr(record.__dict__)


# -- the module stays standalone --


def test_login_limits_imports_only_the_standard_library():
    """The config image ships no herd_common and no third-party package beyond
    its own dependencies; the limiter needs none of them."""
    path = Path(login_limits.__file__)
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module.split(".")[0])
    assert imported, "no imports found; the parse is wrong"
    assert imported <= set(sys.stdlib_module_names) | {"__future__"}, imported
