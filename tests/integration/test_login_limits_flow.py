"""Password-attempt limits and off-loop hashing through the gateway (IAM-LOGIN-7,
IAM-HASH-7), against a running local-mode stack.

Every request this suite sends reaches auth from one source address (Traefik sees
the test host), the same address the rest of the integration suite, the e2e
suite, and the load run sign in from. So these tests spend that one allowance
carefully: the responsiveness test uses a handful of attempts, and the limit test,
which must use all of it to see a 429, waits for the whole allowance to refill
before it returns, so the tests that run after it start as if it never ran. That
wait is the allowance's refill time, about a minute with the defaults.
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


async def _local_mode(client: httpx.AsyncClient) -> None:
    options = await client.get("/auth/login-options")
    if options.status_code != 200 or options.json().get("auth_method") != "local":
        pytest.skip("needs a stack in local auth mode")


def _wrong_login() -> dict:
    return {"email": f"limit-{uuid.uuid4().hex[:12]}@herd.example", "password": "wrong-password"}


async def test_other_auth_routes_answer_while_logins_are_hashing(base_url):
    """Eight concurrent wrong-password logins each cost one bcrypt; while they
    run, `GET /auth/login-options` keeps answering, because the checks run on
    worker threads. The bound is structural: half the time one login takes
    alone (about one check), timed first on the same stack. With the checks on
    the event loop, nearly every probe waited behind at least one whole check,
    so the median probe was at least that long."""
    async with httpx.AsyncClient(base_url=base_url, verify=False, timeout=60.0) as client:
        await _local_mode(client)
        started = time.perf_counter()
        alone = await client.post("/auth/login", json=_wrong_login())
        one_login = time.perf_counter() - started
        assert alone.status_code == 401

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


async def test_a_source_past_its_allowance_gets_the_fixed_429_for_every_account(base_url):
    async with httpx.AsyncClient(base_url=base_url, verify=False, timeout=60.0) as client:
        await _local_mode(client)
        admitted = 0
        limited = None
        while limited is None and admitted < MAX_ATTEMPTS_TO_EXHAUST:
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
        try:
            assert limited.json() == LIMITED
            retry_after = int(limited.headers["Retry-After"])
            assert retry_after >= 1

            # The answer is the same for the seeded admin with the right password,
            # for an unknown account, and for a client that names another source
            # in X-Forwarded-For: Traefik replaces it, so it never picks a bucket.
            for body, headers in (
                ({"email": SEED_EMAIL, "password": SEED_PASSWORD}, {}),
                (_wrong_login(), {}),
                (
                    {"email": SEED_EMAIL, "password": SEED_PASSWORD},
                    {"X-Forwarded-For": "198.51.100.77"},
                ),
            ):
                resp = await client.post("/auth/login", json=body, headers=headers)
                assert (resp.status_code, resp.json()) == (429, LIMITED)
                assert "access_token" not in resp.text
        finally:
            # Leave the source's allowance whole for every test that runs after
            # this one: the full refill takes about admitted * retry_after seconds
            # (retry_after rounds the refill interval up, so this over-waits).
            wait = admitted * int(limited.headers.get("Retry-After", "1")) + 2 if limited else 0
            await asyncio.sleep(min(wait, 600))

        recovered = await client.post(
            "/auth/login", json={"email": SEED_EMAIL, "password": SEED_PASSWORD}
        )
        assert recovered.status_code == 200, recovered.text
