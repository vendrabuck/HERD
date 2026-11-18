"""The per-source limit on password attempts (IAM-LOGIN-7).

Unit level: the token bucket's arithmetic on a fake clock (burst, even refill,
whole-second Retry-After, refusals that do not extend the wait), the memory
bounds (refilled sources dropped, a cap on tracked sources, least recent first),
the settings fallback, the one-line-per-run log, and the source key a request is
counted against, with hostile X-Forwarded-For values.

Functional level, through auth's real routes: the 429's fixed body and
Retry-After, identical for a known account, a known account with the right
password, and an unknown one; a refused attempt runs no query, no bcrypt, and no
directory call; successes count as attempts; every login mode is limited; an
unauthenticated registration is limited and an admin's is not; the key is the
last forwarded entry, so a client's own entries never pick a bucket.
"""

from __future__ import annotations

import logging
import re
from unittest.mock import AsyncMock

import pytest
from app.config import settings
from app.models.user import Role, User
from app.routers.auth import ATTEMPTS_LIMITED_DETAIL
from app.services import auth_service, ldap_service, login_limits, passwords
from app.services.auth_service import create_tokens_for_user, create_user, set_user_active
from app.services.ldap_service import LdapIdentity
from app.services.login_limits import SourceLimiter, client_source
from sqlalchemy import func, select

from tests._harness import TestSessionLocal

PASSWORD = "right-password-1"
LIMITED = {"detail": "Too many attempts; try again later"}
LOGGER = "app.services.login_limits"


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _limiter(max_attempts=5, window_seconds=10, **kwargs) -> tuple[SourceLimiter, FakeClock]:
    clock = FakeClock()
    return (
        SourceLimiter(
            max_attempts=max_attempts, window_seconds=window_seconds, clock=clock, **kwargs
        ),
        clock,
    )


# -- 1. The bucket ------------------------------------------------------------------


def test_a_burst_of_max_attempts_is_admitted_then_refused_with_the_refill_wait():
    limiter, _ = _limiter(max_attempts=5, window_seconds=10)  # one attempt per 2 s
    assert [limiter.acquire("a") for _ in range(5)] == [None] * 5
    assert limiter.acquire("a") == 2


def test_the_allowance_refills_evenly_one_attempt_per_interval():
    limiter, clock = _limiter(max_attempts=5, window_seconds=10)
    for _ in range(5):
        limiter.acquire("a")
    clock.advance(1.9)
    assert limiter.acquire("a") == 1  # 0.1 s short, rounded up to a whole second
    clock.advance(0.1)
    assert limiter.acquire("a") is None
    assert limiter.acquire("a") == 2
    clock.advance(10)  # a whole window refills the whole burst, and no more
    assert [limiter.acquire("a") for _ in range(6)] == [None] * 5 + [2]


@pytest.mark.parametrize(
    "max_attempts, window_seconds", [(1, 1), (3, 7), (5, 10), (60, 60), (7, 3), (1000, 1)]
)
def test_waiting_the_retry_after_always_admits_the_next_attempt(max_attempts, window_seconds):
    limiter, clock = _limiter(max_attempts=max_attempts, window_seconds=window_seconds)
    for _ in range(max_attempts):
        assert limiter.acquire("a") is None
    for _ in range(5):
        wait = limiter.acquire("a")
        while wait is None:  # a whole-second wait can refill more than one
            wait = limiter.acquire("a")
        assert isinstance(wait, int) and wait >= 1
        clock.advance(wait)
        assert limiter.acquire("a") is None


def test_refusals_do_not_extend_the_wait():
    limiter, clock = _limiter(max_attempts=2, window_seconds=10)  # one per 5 s
    limiter.acquire("a")
    limiter.acquire("a")
    assert limiter.acquire("a") == 5
    for _ in range(4):
        clock.advance(0.25)
        assert limiter.acquire("a") is not None
    clock.advance(5 - 1.0)
    assert limiter.acquire("a") is None


def test_sources_are_isolated():
    limiter, _ = _limiter(max_attempts=2)
    limiter.acquire("a")
    limiter.acquire("a")
    assert limiter.acquire("a") is not None
    assert limiter.acquire("b") is None


def test_a_source_whose_allowance_refilled_is_forgotten():
    limiter, clock = _limiter(max_attempts=5, window_seconds=10)
    limiter.acquire("a")
    limiter.acquire("b")
    assert limiter.tracked_sources() == 2
    clock.advance(10)
    limiter.acquire("c")
    assert limiter.tracked_sources() == 1


def test_tracked_sources_are_bounded_least_recently_seen_first():
    limiter, _ = _limiter(max_attempts=1, max_sources=3)
    for source in ("a", "b", "c"):
        assert limiter.acquire(source) is None
    assert limiter.acquire("a") is not None  # "a" is now the most recently seen
    limiter.acquire("d")
    assert limiter.tracked_sources() == 3
    # "b" was the quietest and was dropped: it starts over with a full allowance.
    assert limiter.acquire("b") is None
    assert limiter.acquire("a") is not None


def test_a_flood_of_distinct_sources_never_holds_more_than_the_cap():
    limiter, _ = _limiter(max_attempts=60, window_seconds=60, max_sources=100)
    for n in range(5000):
        limiter.acquire(f"10.0.{n // 256}.{n % 256}")
    assert limiter.tracked_sources() == 100


def test_the_limits_must_be_positive():
    for kwargs in ({"max_attempts": 0}, {"window_seconds": 0}, {"max_sources": 0}):
        with pytest.raises(ValueError):
            SourceLimiter(**kwargs)


def test_defaults_are_60_attempts_a_minute():
    assert (settings.auth_login_max_attempts, settings.auth_login_window_seconds) == (60, 60)
    limiter = SourceLimiter.from_settings()
    assert (limiter.max_attempts, limiter.window_seconds) == (60, 60)


def test_a_setting_below_one_falls_back_to_its_default_with_a_warning(monkeypatch, caplog):
    monkeypatch.setattr(settings, "auth_login_max_attempts", 0)
    monkeypatch.setattr(settings, "auth_login_window_seconds", -5)
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        limiter = SourceLimiter.from_settings()
    assert (limiter.max_attempts, limiter.window_seconds) == (60, 60)
    named = {r.setting for r in caplog.records if r.action == "auth_login_limit_setting_ignored"}
    assert named == {"AUTH_LOGIN_MAX_ATTEMPTS", "AUTH_LOGIN_WINDOW_SECONDS"}


def test_the_settings_are_read(monkeypatch):
    monkeypatch.setattr(settings, "auth_login_max_attempts", 7)
    monkeypatch.setattr(settings, "auth_login_window_seconds", 30)
    limiter = SourceLimiter.from_settings()
    assert (limiter.max_attempts, limiter.window_seconds) == (7, 30)


def test_one_warning_per_run_of_refusals(caplog):
    limiter, clock = _limiter(max_attempts=1, window_seconds=10)
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        limiter.acquire("203.0.113.9")
        for _ in range(20):
            limiter.acquire("203.0.113.9")
        clock.advance(10)
        limiter.acquire("203.0.113.9")  # admitted: the run ends
        limiter.acquire("203.0.113.9")  # a new run
    lines = [r for r in caplog.records if getattr(r, "action", None) == "auth_login_limited"]
    assert len(lines) == 2
    assert lines[0].source == "203.0.113.9" and lines[0].retry_after_seconds == 10


# -- 2. The source key --------------------------------------------------------------


def test_the_peer_is_the_source_without_a_forwarded_header():
    assert client_source([], "192.0.2.10") == "192.0.2.10"


def test_the_last_forwarded_entry_is_the_source_never_one_the_client_wrote():
    # Traefik appends the address it saw after anything a trusted client sent,
    # so only the last entry is not the client's own.
    assert client_source(["6.6.6.6, 203.0.113.9"], "172.18.0.5") == "203.0.113.9"
    assert client_source(["6.6.6.6", "203.0.113.9"], "172.18.0.5") == "203.0.113.9"
    assert client_source(["1.1.1.1, 2.2.2.2 ,  203.0.113.9  "], "172.18.0.5") == "203.0.113.9"


def test_a_last_entry_that_is_not_an_address_falls_back_to_the_peer_not_an_earlier_entry():
    for value in ("203.0.113.9, garbage", "6.6.6.6, unknown", "6.6.6.6, [2001:db8::1]:443"):
        assert client_source([value], "172.18.0.5") == "172.18.0.5"


def test_empty_entries_are_skipped():
    assert client_source(["203.0.113.9,", " , "], "172.18.0.5") == "203.0.113.9"
    assert client_source([" ", ""], "172.18.0.5") == "172.18.0.5"


def test_a_huge_hostile_header_still_yields_the_last_entry():
    value = ", ".join(f"10.{n % 256}.0.1" for n in range(20000)) + ", 203.0.113.9"
    assert client_source([value], "172.18.0.5") == "203.0.113.9"


def test_an_ipv6_source_is_keyed_by_its_64_network():
    one = client_source(["2001:db8:1:2:aaaa::1"], None)
    other = client_source(["2001:db8:1:2:bbbb:cccc:dddd:2"], None)
    assert one == other == "2001:db8:1:2::/64"
    assert client_source(["2001:db8:1:3::1"], None) == "2001:db8:1:3::/64"


def test_an_ipv4_mapped_address_is_keyed_as_ipv4():
    assert client_source(["::ffff:203.0.113.9"], None) == "203.0.113.9"
    assert client_source([], "::ffff:192.0.2.1") == "192.0.2.1"


def test_no_header_and_no_peer_is_one_shared_unknown_source():
    assert client_source([], None) == "unknown"


# -- 3. Through the routes ----------------------------------------------------------


@pytest.fixture
async def client(make_client):
    async with make_client() as ac:
        yield ac


@pytest.fixture
def small_limit(monkeypatch):
    """Install a limiter of `n` attempts with a frozen clock; returns its clock."""

    def _install(n: int = 2, window_seconds: int = 60) -> FakeClock:
        clock = FakeClock()
        monkeypatch.setattr(
            login_limits,
            "LIMITER",
            SourceLimiter(max_attempts=n, window_seconds=window_seconds, clock=clock),
        )
        return clock

    return _install


@pytest.fixture
def cost_spy(monkeypatch) -> dict[str, int]:
    """Counts every bcrypt computation, account query, and directory login."""
    calls = {"checkpw": 0, "hashpw": 0, "query": 0, "bind_user": 0}
    real_checkpw, real_hashpw = passwords.bcrypt.checkpw, passwords.bcrypt.hashpw
    real_by_email = auth_service.get_user_by_email

    def checkpw(secret, hashed):
        calls["checkpw"] += 1
        return real_checkpw(secret, hashed)

    def hashpw(secret, salt):
        calls["hashpw"] += 1
        return real_hashpw(secret, salt)

    async def by_email(db, email):
        calls["query"] += 1
        return await real_by_email(db, email)

    async def bind_user(username, password):
        calls["bind_user"] += 1
        return None

    monkeypatch.setattr(passwords.bcrypt, "checkpw", checkpw)
    monkeypatch.setattr(passwords.bcrypt, "hashpw", hashpw)
    monkeypatch.setattr(auth_service, "get_user_by_email", by_email)
    monkeypatch.setattr(ldap_service, "bind_user", bind_user)
    return calls


async def _account(email="known@example.com", role=Role.USER) -> User:
    async with TestSessionLocal() as db:
        return await create_user(db, email, email.split("@")[0], PASSWORD, role=role)


async def _user_count() -> int:
    async with TestSessionLocal() as db:
        return (await db.execute(select(func.count()).select_from(User))).scalar_one()


def _limited_shape(response) -> tuple:
    return (
        response.status_code,
        response.text,
        response.headers.get("retry-after"),
        response.headers.get("www-authenticate"),
    )


LIMITED_TEXT = '{"detail":"Too many attempts; try again later"}'


async def test_the_429_has_the_fixed_body_and_a_whole_second_retry_after(client, small_limit):
    small_limit(2, 60)  # one attempt every 30 s
    body = {"email": "nobody@example.com", "password": "wrong-password"}
    assert (await client.post("/login", json=body)).status_code == 401
    assert (await client.post("/login", json=body)).status_code == 401
    response = await client.post("/login", json=body)
    assert ATTEMPTS_LIMITED_DETAIL == LIMITED["detail"]
    assert _limited_shape(response) == (429, LIMITED_TEXT, "30", None)


async def test_the_429_is_identical_for_known_and_unknown_accounts(client, small_limit):
    await _account()
    small_limit(1)
    await client.post("/login", json={"email": "warmup@example.com", "password": "x"})
    shapes = set()
    for body in (
        {"email": "known@example.com", "password": PASSWORD},
        {"email": "known@example.com", "password": "wrong-password"},
        {"email": "nobody@example.com", "password": PASSWORD},
    ):
        response = await client.post("/login", json=body)
        shapes.add(_limited_shape(response))
    assert shapes == {(429, LIMITED_TEXT, "60", None)}


async def test_a_refused_attempt_runs_no_query_no_bcrypt_and_no_directory_call(
    client, small_limit, cost_spy
):
    await _account()
    small_limit(1)
    await client.post("/login", json={"email": "known@example.com", "password": PASSWORD})
    for key in cost_spy:
        cost_spy[key] = 0
    response = await client.post("/login", json={"email": "known@example.com", "password": "x"})
    assert response.status_code == 429
    assert cost_spy == {"checkpw": 0, "hashpw": 0, "query": 0, "bind_user": 0}


async def test_successful_logins_count_as_attempts(client, small_limit):
    await _account()
    small_limit(3)
    body = {"email": "known@example.com", "password": PASSWORD}
    statuses = [(await client.post("/login", json=body)).status_code for _ in range(4)]
    assert statuses == [200, 200, 200, 429]


@pytest.mark.parametrize("mode", ["local", "ldap", "oidc-off", "oidc-on"])
async def test_every_login_mode_is_limited_before_its_judge(
    mode, client, small_limit, cost_spy, monkeypatch
):
    await _account("root@example.com", role=Role.SUPERADMIN)
    method = "oidc" if mode.startswith("oidc") else mode
    monkeypatch.setattr(settings, "auth_method", method)
    monkeypatch.setattr(settings, "oidc_break_glass_local_superadmin", mode == "oidc-on")
    small_limit(1)
    body = {"email": "root@example.com", "password": "wrong-password"}
    assert (await client.post("/login", json=body)).status_code == 401
    judged = dict(cost_spy)
    assert judged["checkpw"] + judged["bind_user"] == 1
    response = await client.post("/login", json=body)
    assert (response.status_code, response.json()) == (429, LIMITED)
    assert cost_spy == judged


async def test_an_unauthenticated_registration_counts_and_a_refused_one_creates_nothing(
    client, small_limit, cost_spy
):
    small_limit(2)
    first = {"email": "a@example.com", "username": "aaa", "password": PASSWORD}
    assert (await client.post("/register", json=first)).status_code == 201
    assert (await client.post("/register", json=first)).status_code == 409
    hashes = cost_spy["hashpw"]
    second = {"email": "b@example.com", "username": "bbb", "password": PASSWORD}
    response = await client.post("/register", json=second)
    assert _limited_shape(response) == (429, LIMITED_TEXT, "30", None)
    assert cost_spy["hashpw"] == hashes
    assert await _user_count() == 1


async def test_login_and_registration_share_one_allowance(client, small_limit):
    small_limit(2)
    body = {"email": "a@example.com", "username": "aaa", "password": PASSWORD}
    assert (await client.post("/register", json=body)).status_code == 201
    assert (await client.post("/login", json=body)).status_code == 200
    assert (await client.post("/login", json=body)).status_code == 429


async def _bearer(user: User) -> dict[str, str]:
    async with TestSessionLocal() as db:
        db.add(user)
        access_token, _ = await create_tokens_for_user(db, user)
    return {"Authorization": f"Bearer {access_token}"}


async def test_a_registration_by_an_admin_is_not_counted(client, small_limit):
    clock = small_limit(1)
    for role in (Role.ADMIN, Role.SUPERADMIN):
        admin = await _account(f"{role.value}@example.com", role=role)
        headers = await _bearer(admin)
        for n in range(3):
            body = {
                "email": f"{role.value}-made-{n}@example.com",
                "username": f"{role.value}-made-{n}",
                "password": PASSWORD,
            }
            response = await client.post("/register", json=body, headers=headers)
            assert response.status_code == 201, response.text
    clock.advance(0)
    # The source's own allowance is untouched.
    assert login_limits.LIMITER.acquire("127.0.0.1") is None


async def test_a_registration_by_anyone_else_is_counted(client, small_limit):
    user = await _account("plain@example.com")
    inactive_admin = await _account("gone@example.com", role=Role.ADMIN)
    async with TestSessionLocal() as db:
        await set_user_active(db, inactive_admin.id, False)
    headers_by_case = {
        "a user's token": await _bearer(user),
        "a deactivated admin's token": await _bearer(inactive_admin),
        "a token that does not verify": {"Authorization": "Bearer not-a-token"},
        "another scheme": {"Authorization": "Basic YWRtaW46YWRtaW4="},
    }
    for case, headers in headers_by_case.items():
        small_limit(1)
        body = {"email": f"x-{len(case)}@example.com", "username": f"x{len(case)}"}
        body["password"] = PASSWORD
        first = await client.post("/register", json=body, headers=headers)
        assert first.status_code in (201, 409), case
        second = await client.post("/register", json=body, headers=headers)
        assert (second.status_code, second.json()) == (429, LIMITED), case


async def test_registration_in_ldap_or_oidc_mode_stays_409_and_counts_nothing(
    client, small_limit, monkeypatch
):
    small_limit(1)
    body = {"email": "a@example.com", "username": "aaa", "password": PASSWORD}
    for method in ("ldap", "oidc"):
        monkeypatch.setattr(settings, "auth_method", method)
        for _ in range(3):
            assert (await client.post("/register", json=body)).status_code == 409
    assert login_limits.LIMITER.acquire("127.0.0.1") is None


async def test_the_source_is_the_last_forwarded_entry_through_the_route(client, small_limit):
    small_limit(1)
    body = {"email": "nobody@example.com", "password": "wrong-password"}
    # What the trusted hop appended is the last entry; the entries before it are
    # whatever the client sent, and changing them never earns a fresh allowance.
    first = await client.post("/login", json=body, headers={"X-Forwarded-For": "203.0.113.9"})
    assert first.status_code == 401
    for spoofed in ("198.51.100.1, 203.0.113.9", "10.0.0.1, 10.0.0.2, 203.0.113.9"):
        response = await client.post("/login", json=body, headers={"X-Forwarded-For": spoofed})
        assert response.status_code == 429, spoofed
    other = await client.post("/login", json=body, headers={"X-Forwarded-For": "203.0.113.10"})
    assert other.status_code == 401


async def test_the_limited_log_line_holds_no_email_and_no_password(client, small_limit, caplog):
    small_limit(1)
    secret = "do-not-log-this-password"
    body = {"email": "identifying@example.com", "password": secret}
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        await client.post("/login", json=body)
        await client.post("/login", json=body)
        await client.post("/login", json=body)
    lines = [r for r in caplog.records if getattr(r, "action", None) == "auth_login_limited"]
    assert len(lines) == 1
    text = lines[0].getMessage() + repr(vars(lines[0]))
    assert secret not in text and "identifying" not in text
    assert re.search(r"source 127\.0\.0\.1", lines[0].getMessage())


async def test_the_ldap_mode_login_counts_and_is_refused_before_the_directory(
    client, small_limit, monkeypatch
):
    monkeypatch.setattr(settings, "auth_method", "ldap")
    bind = AsyncMock(
        return_value=LdapIdentity(
            username="alice", email="alice@example.com", dn="CN=alice,DC=example,DC=com"
        )
    )
    monkeypatch.setattr(ldap_service, "bind_user", bind)
    small_limit(1)
    body = {"email": "alice", "password": "pw"}
    assert (await client.post("/login", json=body)).status_code == 200
    assert (await client.post("/login", json=body)).status_code == 429
    assert bind.await_count == 1
