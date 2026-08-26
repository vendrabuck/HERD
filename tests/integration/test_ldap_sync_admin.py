"""Integration: the ADR 0011 LDAP directory-sync admin surface against a real
HERD stack running in LDAP mode (issue #572).

Gated with the same pytestmark trio as test_ldap_auth.py: HERD_INTEGRATION_LDAP=1
and a reachable LDAP server. Run together (see the Makefile's
_gate-ldap-stack-tests), this file is test_ldap_auth.py's companion: that file
proves LOGIN works against the directory; this one proves the admin sync
surface (mapping create, sync-now, run polling, group-membership reconcile)
works too, coverage no gate exercised before _gate-ldap-stack-tests existed.

Dedicated ldapit-* identities, not the userN/herd-eng fixtures: a gate stack
seeded via `make seed` (seed_devices_public.py) already holds LOCAL users
user1..user1000 and admin1..admin50 in the same users table (auth_source=
"local"). JIT-provisioning an LDAP login for one of those usernames collides
(auth_service.py's username_collision path refuses it), and separately,
ldap_sync_service's reconciler only ever touches LDAP-sourced users, so the
seeded local user1..user3 rows would silently be skipped from a
sync-driven membership assertion even where provisioning did not collide.
infra/ldap-test/ldif/70-seed-integration.ldif adds ldapit-admin and
ldapit-eng1..3 (uids that can never match the seed script's `user[0-9]+` /
`admin[0-9]+` patterns) plus their own directory group, cn=herd-it-eng, so
this file's assertions hold whether or not the stack has been seeded.

Bootstrapping problem this file works around: in LDAP mode, authenticate_user
(services/auth/app/services/auth_service.py) consults ONLY the directory, so
the stack's seeded local superadmin cannot log in, and there is therefore no
way to obtain an admin-role JWT through the public API. This file instead:
  1. Logs in as ldapit-admin (JIT-provisioned on first login, auth_source=
     "ldap", role defaults to "user").
  2. Promotes that ONE row directly in Postgres via `docker compose exec
     postgres psql` (subprocess; honors COMPOSE_PROJECT_NAME from the calling
     environment exactly like tests/integration/test_outbox_durability.py's
     compose helper, and the bare `docker compose exec postgres psql -U ...`
     form docs/TROUBLESHOOTING.md and docs/OPERATIONS.md already document).
  3. Re-logs in so the new JWT actually carries the superadmin role. The role
     check itself (services/auth/app/dependencies/auth.py's require_role) is
     DB-fresh on every request, not JWT-cached, so this step is not strictly
     required for the role gate to pass; it is kept anyway so the token this
     file authenticates with matches what an admin would actually be issued.

Concurrency note: this stack runs a single auth replica (services/auth's
Dockerfile has no --workers flag), so the two sync-now requests in
test_concurrent_sync_now_one_wins race the IN-PROCESS asyncio.Lock in
ldap_sync_service._SyncSlot, never the cross-replica Postgres advisory-lock
branch (SyncBusyError("replica")). That branch needs two separate auth
processes contending for the same advisory lock and is covered directly
against real Postgres by services/auth/tests/test_ldap_sync_service_live_pg.py
(run by the Makefile's sibling _gate-pg-live-tests phase, not from here).

Cleanup: the mapping and group this file creates are deleted via the API
first (mapped_group's teardown), then every ldapit-% row in auth.users is
deleted directly in Postgres (superadmin_token's teardown, which runs after,
per fixture reverse-dependency order): this covers both the promoted
ldapit-admin row and the ldapit-eng1..3 rows the sync run JIT-provisions
while reconciling group membership, so a re-run of this file (or a later
gate phase: seeding, load tests) sees the stack's baseline state again.
"""

from __future__ import annotations

import asyncio
import os
import socket
import time
import uuid

import httpx
import pytest
from conftest import _psql

LDAP_HOST = os.getenv("HERD_TEST_LDAP_HOST", "127.0.0.1")
LDAP_PORT = int(os.getenv("HERD_TEST_LDAP_PORT", "389"))
# Dedicated admin identity, distinct from test_ldap_auth.py's HERD_TEST_LDAP_USER
# (ldapit-eng1): this file promotes whoever it logs in as to superadmin (see
# the module docstring), so it gets its own env override rather than sharing
# one knob for two different roles.
LDAP_SYNC_ADMIN_USER = os.getenv("HERD_TEST_LDAP_SYNC_ADMIN_USER", "ldapit-admin")
LDAP_PASSWORD = os.getenv("HERD_TEST_LDAP_PASSWORD", "Password1")

BASE_URL = os.getenv("HERD_BASE_URL", "https://localhost/api")

# The herd-it-eng directory group (infra/ldap-test/ldif/70-seed-integration.ldif):
# a fully-resolvable groupOfNames with three dedicated ldapit-eng members (see
# the module docstring for why these, and not the seeded userN/herd-eng
# fixtures), none of the per-member skip cases the mixed/stale fixture groups
# exercise.
HERD_IT_ENG_DN = "cn=herd-it-eng,ou=groups,dc=company,dc=local"
HERD_IT_ENG_MEMBERS = {"ldapit-eng1", "ldapit-eng2", "ldapit-eng3"}

_RUN_IN_PROGRESS_DETAIL = "A sync run is already in progress"


def _ldap_reachable() -> bool:
    try:
        with socket.create_connection((LDAP_HOST, LDAP_PORT), timeout=1.0):
            return True
    except OSError:
        return False


pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("HERD_INTEGRATION_LDAP") != "1",
        reason="HERD_INTEGRATION_LDAP not set; stack is not configured for LDAP auth.",
    ),
    pytest.mark.skipif(
        not _ldap_reachable(),
        reason=f"No LDAP server reachable at ldap://{LDAP_HOST}:{LDAP_PORT}",
    ),
]


def _login(email: str, password: str) -> dict:
    """Sync login helper for fixture setup/teardown (see the module note by
    the fixtures below on why fixtures never use asyncio.run)."""
    with httpx.Client(verify=False, timeout=15.0) as client:
        resp = client.post(
            f"{BASE_URL}/auth/login",
            json={"email": email, "password": password},
        )
        resp.raise_for_status()
        return resp.json()


async def _poll_run(client: httpx.AsyncClient, run_id: str, timeout: float = 60.0) -> dict:
    """Poll GET /auth/admin/ldap-sync/runs/{id} until it leaves status 'running'."""
    deadline = time.monotonic() + timeout
    while True:
        resp = await client.get(f"/auth/admin/ldap-sync/runs/{run_id}")
        assert resp.status_code == 200, (
            f"GET admin/ldap-sync/runs/{run_id} failed: {resp.status_code} {resp.text}"
        )
        run = resp.json()
        if run["status"] != "running":
            return run
        if time.monotonic() >= deadline:
            pytest.fail(f"Sync run {run_id} did not finish within {timeout}s: {run}")
        await asyncio.sleep(1.0)


# Fixture setup/teardown below is deliberately SYNC (httpx.Client, no
# asyncio.run), never AsyncClient: these calls are sequential anyway (login,
# then promote, then re-login; create group, then create mapping; delete
# mapping, then delete group), so async buys nothing here, and a raw
# asyncio.run() inside a sync fixture used by a pytest-asyncio async test
# creates and tears down its own throwaway event loop alongside
# pytest-asyncio's own per-test loop, an interaction the live gate saw
# leave behind unclosed AF_UNIX event-loop self-pipe sockets (issue #572
# follow-up, 2026-08-26): every test still passed, but pytest exited 1 on
# an ExceptionGroup of PytestUnraisableExceptionWarning at session end.
# httpx.AsyncClient stays reserved for the async test bodies themselves,
# which run under pytest-asyncio's own properly-managed loop.


@pytest.fixture(scope="module")
def superadmin_token():
    """Promote LDAP_SYNC_ADMIN_USER (ldapit-admin) to superadmin out-of-band
    (see module docstring) and return a fresh JWT for it.

    Module teardown sweeps every ldapit-% row in auth.users, not just this
    one: the sync run in test_sync_run_reconciles_group_membership_from_
    directory JIT-provisions ldapit-eng1..3 too (auth_source='ldap', since
    they resolve as new local rows the first time the reconciler adds them
    to the mapped HERD group), and those would otherwise linger. This runs
    after mapped_group's own teardown has removed the mapping and group
    (fixture teardown runs in reverse dependency order, so that happens
    first; the FKs from group_members/refresh_tokens to users.id are all
    ON DELETE CASCADE or SET NULL per the models, so the wildcard delete
    below needs no separate dependent-row cleanup either way).
    """
    _login(LDAP_SYNC_ADMIN_USER, LDAP_PASSWORD)  # JIT-provisions the row

    result = _psql(
        f"UPDATE auth.users SET role='SUPERADMIN' WHERE username='{LDAP_SYNC_ADMIN_USER}'"
    )
    assert result.returncode == 0, f"promote failed: {result.stdout} {result.stderr}"
    assert "UPDATE 1" in result.stdout, f"expected exactly one row updated: {result.stdout!r}"

    tokens = _login(LDAP_SYNC_ADMIN_USER, LDAP_PASSWORD)
    yield tokens["access_token"]

    _psql("DELETE FROM auth.users WHERE username LIKE 'ldapit-%'")


@pytest.fixture(scope="module")
def mapped_group(superadmin_token):
    """Create a throwaway HERD group mapped to the herd-it-eng directory group.

    Yields (group, mapping) dicts from their respective create responses.
    """
    headers = {"Authorization": f"Bearer {superadmin_token}"}

    with httpx.Client(base_url=BASE_URL, verify=False, timeout=30.0, headers=headers) as client:
        group_resp = client.post(
            "/auth/groups",
            json={
                "name": f"int-ldap-sync-{uuid.uuid4().hex[:8]}",
                "description": "issue #572 integration test",
            },
        )
        group_resp.raise_for_status()
        group = group_resp.json()
        mapping_resp = client.post(
            "/auth/admin/ldap-sync/mappings",
            json={"group_dn": HERD_IT_ENG_DN, "herd_group_id": group["id"]},
        )
        mapping_resp.raise_for_status()
        mapping = mapping_resp.json()

    yield group, mapping

    with httpx.Client(base_url=BASE_URL, verify=False, timeout=30.0, headers=headers) as client:
        try:
            client.delete(f"/auth/admin/ldap-sync/mappings/{mapping['id']}")
        except httpx.HTTPError:
            pass
        try:
            client.delete(f"/auth/groups/{group['id']}")
        except httpx.HTTPError:
            pass


async def test_sync_run_reconciles_group_membership_from_directory(superadmin_token, mapped_group):
    """POST /admin/ldap-sync/run reconciles the mapped HERD group's members
    to match the herd-it-eng directory group, read back through
    GET /groups/{id} (not just the run's own success status).
    """
    group, _mapping = mapped_group
    headers = {"Authorization": f"Bearer {superadmin_token}"}
    async with httpx.AsyncClient(
        base_url=BASE_URL, verify=False, timeout=30.0, headers=headers
    ) as client:
        run_resp = await client.post("/auth/admin/ldap-sync/run")
        assert run_resp.status_code == 202, run_resp.text
        run_id = run_resp.json()["run_id"]

        run = await _poll_run(client, run_id)
        assert run["status"] in ("success", "partial"), run
        assert run["error"] is None, run

        detail_resp = await client.get(f"/auth/groups/{group['id']}")
        detail_resp.raise_for_status()
        members = {m["username"] for m in detail_resp.json()["members"]}
        assert members == HERD_IT_ENG_MEMBERS, members


async def test_concurrent_sync_now_one_wins(superadmin_token):
    """Two sync-now requests fired concurrently: exactly one is accepted
    (202) and the other 409s with the in-process busy detail (see module
    docstring for why this is always the in_process reason, never replica,
    on this single-auth-replica stack).
    """
    headers = {"Authorization": f"Bearer {superadmin_token}"}
    async with httpx.AsyncClient(
        base_url=BASE_URL, verify=False, timeout=30.0, headers=headers
    ) as client:
        first, second = await asyncio.gather(
            client.post("/auth/admin/ldap-sync/run"),
            client.post("/auth/admin/ldap-sync/run"),
            return_exceptions=True,
        )
        for resp in (first, second):
            assert not isinstance(resp, Exception), resp

        codes = sorted([first.status_code, second.status_code])
        assert codes == [202, 409], (
            first.status_code,
            second.status_code,
            first.text,
            second.text,
        )
        winner, loser = (first, second) if first.status_code == 202 else (second, first)
        assert loser.json()["detail"] == _RUN_IN_PROGRESS_DETAIL, loser.text

        run = await _poll_run(client, winner.json()["run_id"])
        assert run["status"] in ("success", "partial"), run
