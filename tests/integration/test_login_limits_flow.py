"""Password-attempt limits and off-loop hashing through the gateway (IAM-LOGIN-7,
IAM-HASH-7), against a running local-mode stack.

Every request this suite sends reaches auth from one source address (Traefik sees
the test host), the same address the rest of the integration suite, the e2e
suite, and the load run sign in from. So these tests spend that one allowance
carefully. The responsiveness test uses eleven attempts. The limit test must use
all of it to see a 429, so afterwards, also when an assertion fails, it waits one
whole AUTH_LOGIN_WINDOW_SECONDS plus a margin: the allowance refills
AUTH_LOGIN_MAX_ATTEMPTS attempts per window, so one window after its last counted
attempt the source's allowance is whole again, however much of it the tests
before had used. With the defaults that wait is 63 seconds, which is why the test
carries its own timeout: `make test-integration` runs with `--timeout=30` and the
nightly with `--timeout=60`.

It runs in the default integration run on purpose, so the property is proven in
the per-PR integration job, in `make master` and `make everything`, and nightly
(docs/specs/TESTING.md).
"""

import asyncio
import os
import time
import uuid

import httpx
import pytest

pytestmark = pytest.mark.asyncio

SEED_EMAIL = os.getenv("SEED_EMAIL") or os.getenv("SUPERADMIN_EMAIL", "admin@example.com")
SEED_PASSWORD = os.getenv("SEED_PASSWORD") or os.getenv("SUPERADMIN_PASSWORD", "admin123!")
LIMITED = {"detail": "Too many attempts; try again later"}
# Enough to exhaust any allowance a stack is likely to be configured with; a stack
# that answers this many attempts without a 429 has no working limit.
MAX_ATTEMPTS_TO_EXHAUST = 400
BATCH = 10
# Tries for a probe that must be refused; see the refill note in the limit test.
PROBE_TRIES = 5


def _window_seconds() -> int:
    """The stack's AUTH_LOGIN_WINDOW_SECONDS, as auth reads it: the value the
    environment (and the repo's .env, which the integration conftest loads) gives
    compose, else the default of 60 that docker-compose.yml and auth's Settings
    share; a value below 1 is the default there too."""
    try:
        value = int(os.getenv("AUTH_LOGIN_WINDOW_SECONDS") or 60)
    except ValueError:
        return 60
    return value if value >= 1 else 60


# One whole window plus a margin for the requests in flight and clock rounding.
RESTORE_SECONDS = _window_seconds() + 3


async def _local_mode(client: httpx.AsyncClient) -> None:
    options = await client.get("/auth/login-options")
    if options.status_code != 200 or options.json().get("auth_method") != "local":
        pytest.skip("needs a stack in local auth mode")


def _wrong_login() -> dict:
    return {"email": f"limit-{uuid.uuid4().hex[:12]}@herd.example", "password": "wrong-password"}


async def _spend_until_limited(client: httpx.AsyncClient) -> None:
    """Send lone wrong-password attempts until one is answered 429, so the caller's
    next request arrives with the source's allowance just seen empty."""
    for _ in range(MAX_ATTEMPTS_TO_EXHAUST):
        answer = await client.post("/auth/login", json=_wrong_login())
        if answer.status_code == 429:
            assert answer.json() == LIMITED
            return
        assert answer.status_code == 401, answer.text
    raise AssertionError("the source's allowance never ran out")


async def test_other_auth_routes_answer_while_logins_are_hashing(base_url):
    """Eight concurrent wrong-password logins each cost one bcrypt; while they
    run, `GET /auth/login-options` keeps answering, because the checks run on
    worker threads.

    The bound is generous on purpose: the median probe must take under half of
    what one login takes alone (the median of three lone logins, so one lucky
    fast login does not tighten it). With the checks off the loop a probe takes a
    few milliseconds while a login takes about a fifth of a second, and both slow
    down together on a slow runner, so the margin holds there. With the checks on
    the event loop, a probe that arrives while a check runs waits for the rest of
    it and for the checks queued with it, so the median probe is about half a
    check or more: this test will usually, not certainly, catch that. The proof
    that the loop stays free is in-process
    (services/auth/tests/test_password_hashing_off_loop.py)."""
    async with httpx.AsyncClient(base_url=base_url, verify=False, timeout=60.0) as client:
        await _local_mode(client)
        lone = []
        for _ in range(3):
            started = time.perf_counter()
            alone = await client.post("/auth/login", json=_wrong_login())
            lone.append(time.perf_counter() - started)
            assert alone.status_code == 401
        one_login = sorted(lone)[1]

        probes: list[float] = []
        done = asyncio.Event()

        async def probe() -> None:
            while not done.is_set():
                began = time.perf_counter()
                resp = await client.get("/auth/login-options")
                assert resp.status_code == 200
                probes.append(time.perf_counter() - began)
                await asyncio.sleep(0.02)

        prober = asyncio.create_task(probe())
        answers = await asyncio.gather(
            *(client.post("/auth/login", json=_wrong_login()) for _ in range(8))
        )
        done.set()
        await prober
    assert [a.status_code for a in answers] == [401] * 8
    assert len(probes) >= 3, probes
    median = sorted(probes)[len(probes) // 2]
    assert median < one_login / 2, (median, one_login, probes)


@pytest.mark.timeout(RESTORE_SECONDS + 120)
async def test_a_source_past_its_allowance_gets_the_fixed_429_for_every_account(base_url):
    async with httpx.AsyncClient(base_url=base_url, verify=False, timeout=60.0) as client:
        await _local_mode(client)
        spent = False
        try:
            admitted = 0
            limited = None
            while limited is None and admitted < MAX_ATTEMPTS_TO_EXHAUST:
                spent = True
                answers = await asyncio.gather(
                    *(client.post("/auth/login", json=_wrong_login()) for _ in range(BATCH))
                )
                for answer in answers:
                    if answer.status_code == 429:
                        limited = limited or answer
                    else:
                        assert answer.status_code == 401, answer.text
                        admitted += 1
            assert limited is not None, f"no 429 after {admitted} attempts from one source"
            assert limited.json() == LIMITED
            assert int(limited.headers["Retry-After"]) >= 1

            # The answer is the same for the seeded admin with the right password,
            # for an unknown account, and for a client that names another source
            # in X-Forwarded-For: Traefik replaces it, so it never picks a bucket.
            #
            # The allowance refills one attempt per window/max seconds (one a
            # second by default) while this test runs, so a probe can honestly be
            # admitted with an attempt that refilled since the last 429. Each
            # probe is therefore sent right after a lone attempt that was itself
            # answered 429 (a few milliseconds apart, against a refill of about a
            # second), and is retried when a refill still slipped between the
            # two. A probe that is admitted on every try is not a refill: it is a
            # request the limit does not cover.
            for body, headers in (
                ({"email": SEED_EMAIL, "password": SEED_PASSWORD}, {}),
                (_wrong_login(), {}),
                (
                    {"email": SEED_EMAIL, "password": SEED_PASSWORD},
                    {"X-Forwarded-For": "198.51.100.77"},
                ),
            ):
                seen = []
                for _ in range(PROBE_TRIES):
                    await _spend_until_limited(client)
                    resp = await client.post("/auth/login", json=body, headers=headers)
                    seen.append(resp.status_code)
                    if resp.status_code == 429:
                        break
                assert resp.status_code == 429, (body["email"], headers, seen)
                assert resp.json() == LIMITED
                assert "access_token" not in resp.text
        finally:
            # Leave the source's allowance whole for every test that runs after
            # this one, also when an assertion above failed: a refused attempt
            # counts nothing, so one whole window after the last admitted attempt
            # the allowance has refilled completely, whatever it held before.
            if spent:
                await asyncio.sleep(RESTORE_SECONDS)

        recovered = await client.post(
            "/auth/login", json={"email": SEED_EMAIL, "password": SEED_PASSWORD}
        )
        assert recovered.status_code == 200, recovered.text
