"""Integration test fixtures for testing against a running HERD stack.

The fixtures here are self-seeding: they register a non-admin test user,
upload a driver package, create a DUT template, and spin up fresh devices on
demand. Integration tests do NOT rely on `seed_devices.py` having been run.
"""

import io
import os
import subprocess
import sys
import tarfile
import uuid
from pathlib import Path

import httpx
import pytest

# Make sibling helper modules (e.g. _ai_helpers.py) importable. pytest runs
# from the repo root, which is not in sys.path for this directory.
sys.path.insert(0, str(Path(__file__).parent))

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_repo_env() -> None:
    """Populate os.environ from the repo-root .env, matching docker-compose.

    docker-compose auto-loads .env, pytest does not; without this the host
    shell and the stack can disagree on values like AI_API_KEY and make
    wiring-check tests flap. Existing env vars win so callers can still
    override per-run.
    """
    env_path = REPO_ROOT / ".env"
    if not env_path.is_file():
        return
    for raw in env_path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


_load_repo_env()


def _psql(sql: str, *, tuples_only: bool = False) -> "subprocess.CompletedProcess[str]":
    """Run one statement inside the running stack's postgres container.

    Bare `docker compose` (no -p), so COMPOSE_PROJECT_NAME from the calling
    environment picks the target project, matching
    tests/integration/test_outbox_durability.py's `_run_compose` helper and
    the Makefile gate-phase convention (make exports command-line variable
    overrides into recipe environments). Shared here (rather than duplicated
    per-file) by tests/integration/test_ldap_auth.py and
    test_ldap_sync_admin.py, both of which need a Postgres read/write with
    no API path in LDAP mode (see their module docstrings).

    tuples_only adds psql's `-tA` (tuples-only, unaligned), for a bare value
    with no header/row-count noise, e.g. a single-column SELECT read-back.
    """
    pguser = os.getenv("POSTGRES_USER", "herd")
    pgdb = os.getenv("POSTGRES_DB", "herd")
    cmd = ["docker", "compose", "exec", "-T", "postgres", "psql", "-U", pguser, "-d", pgdb]
    if tuples_only:
        cmd.append("-tA")
    cmd += ["-c", sql]
    try:
        return subprocess.run(
            cmd,
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as exc:
        pytest.skip(f"docker compose exec not usable from this host: {exc}")


BASE_URL = os.getenv("HERD_BASE_URL", "https://localhost/api")
SEED_EMAIL = os.getenv("SEED_EMAIL") or os.getenv("SUPERADMIN_EMAIL", "admin@example.com")
SEED_PASSWORD = os.getenv("SEED_PASSWORD") or os.getenv("SUPERADMIN_PASSWORD", "admin123!")
# Generate a unique test user per session run so repeated runs against the same
# stack don't collide on username uniqueness.
_USER_SUFFIX = uuid.uuid4().hex[:8]
USER_EMAIL = os.getenv("SEED_USER_EMAIL", f"intuser-{_USER_SUFFIX}@herd.example")
USER_USERNAME = os.getenv("SEED_USER_USERNAME", f"intuser-{_USER_SUFFIX}")
USER_PASSWORD = os.getenv("SEED_USER_PASSWORD", "Password123!")


@pytest.fixture(scope="session")
def base_url():
    return BASE_URL


async def _login(base_url: str, email: str, password: str) -> dict:
    async with httpx.AsyncClient(verify=False, timeout=30.0) as client:
        resp = await client.post(
            f"{base_url}/auth/login",
            json={"email": email, "password": password},
        )
        resp.raise_for_status()
        return resp.json()


async def _ensure_user(
    admin_tokens: dict, base_url: str, email: str, username: str, password: str
) -> None:
    """Ensure a non-admin user exists; register via admin if not, ignore 409s."""
    async with httpx.AsyncClient(
        base_url=base_url,
        verify=False,
        timeout=30.0,
        headers={"Authorization": f"Bearer {admin_tokens['access_token']}"},
    ) as client:
        resp = await client.post(
            "/auth/register",
            json={"email": email, "password": password, "username": username},
        )
        if resp.status_code not in (200, 201, 409):
            resp.raise_for_status()


@pytest.fixture(scope="session")
def event_loop():
    """Create a session-scoped event loop for async fixtures."""
    import asyncio

    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(scope="session")
async def admin_tokens(base_url):
    return await _login(base_url, SEED_EMAIL, SEED_PASSWORD)


@pytest.fixture(scope="session")
async def user_tokens(base_url, admin_tokens):
    await _ensure_user(admin_tokens, base_url, USER_EMAIL, USER_USERNAME, USER_PASSWORD)
    return await _login(base_url, USER_EMAIL, USER_PASSWORD)


@pytest.fixture(scope="session")
def admin_token(admin_tokens):
    return admin_tokens["access_token"]


@pytest.fixture(scope="session")
def user_token(user_tokens):
    return user_tokens["access_token"]


@pytest.fixture
async def admin_client(base_url, admin_token):
    async with httpx.AsyncClient(
        base_url=base_url,
        verify=False,
        headers={"Authorization": f"Bearer {admin_token}"},
        timeout=30.0,
    ) as client:
        yield client


@pytest.fixture
async def user_client(base_url, user_token):
    async with httpx.AsyncClient(
        base_url=base_url,
        verify=False,
        headers={"Authorization": f"Bearer {user_token}"},
        timeout=30.0,
    ) as client:
        yield client


def _driver_tarball(body: bytes = b"class Driver:\n    pass\n") -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        info = tarfile.TarInfo("driver.py")
        info.size = len(body)
        tf.addfile(info, io.BytesIO(body))
    return buf.getvalue()


@pytest.fixture(scope="session")
async def mgmt_driver(base_url, admin_token):
    """Upload a Management-connection driver once per session for DUT tests."""
    async with httpx.AsyncClient(
        base_url=base_url,
        verify=False,
        headers={"Authorization": f"Bearer {admin_token}"},
        timeout=30.0,
    ) as client:
        files = {"file": ("driver.tar.gz", _driver_tarball(), "application/gzip")}
        data = {
            "name": f"int-seed-driver-{uuid.uuid4().hex[:8]}",
            "connection_type": "Management",
            "description": "integration test seed driver",
        }
        resp = await client.post("/inventory/drivers", files=files, data=data)
        resp.raise_for_status()
        driver = resp.json()
        yield driver
        # Best-effort cleanup at session end; may 409 if templates still reference it.
        await client.delete(f"/inventory/drivers/{driver['id']}")


@pytest.fixture(scope="session")
async def dut_template(base_url, admin_token, mgmt_driver):
    """Create a DUT-capable device template once per session."""
    async with httpx.AsyncClient(
        base_url=base_url,
        verify=False,
        headers={"Authorization": f"Bearer {admin_token}"},
        timeout=30.0,
    ) as client:
        payload = {
            "name": f"int-seed-template-{uuid.uuid4().hex[:8]}",
            "template_type": "device",
            "driver_id": mgmt_driver["id"],
            "vendor": "IntegrationVendor",
            "model": "IntegrationModel",
            "sections": [
                {
                    "name": "General",
                    "fields": [{"key": "model", "label": "Model", "type": "string"}],
                }
            ],
        }
        resp = await client.post("/inventory/templates", json=payload)
        resp.raise_for_status()
        template = resp.json()
        yield template
        await client.delete(f"/inventory/templates/{template['id']}")


@pytest.fixture(scope="session")
async def seed_group(base_url, admin_token):
    """Ensure a user group exists for ACL tests; reuse 'Not Grouped' if present."""
    async with httpx.AsyncClient(
        base_url=base_url,
        verify=False,
        headers={"Authorization": f"Bearer {admin_token}"},
        timeout=30.0,
    ) as client:
        resp = await client.get("/auth/groups")
        resp.raise_for_status()
        items = resp.json().get("items") or resp.json()
        if items:
            return items[0]
        create = await client.post(
            "/auth/groups",
            json={"name": f"int-seed-group-{uuid.uuid4().hex[:6]}", "description": "integration"},
        )
        create.raise_for_status()
        return create.json()


async def _create_fresh_device(client, template_id: str) -> dict:
    body = {
        "name": f"int-device-{uuid.uuid4().hex[:10]}",
        "template_id": template_id,
        "topology_type": "PHYSICAL",
        "status": "AVAILABLE",
        "field_data": {"model": "test"},
    }
    resp = await client.post("/inventory/devices", json=body)
    resp.raise_for_status()
    return resp.json()


@pytest.fixture
async def fresh_device(admin_client, dut_template):
    """Create a throwaway device per test and delete it during teardown."""
    device = await _create_fresh_device(admin_client, dut_template["id"])
    try:
        yield device
    finally:
        await admin_client.delete(f"/inventory/devices/{device['id']}")


@pytest.fixture
async def visible_fresh_device(admin_client, base_url, user_token, fresh_device):
    """A fresh device made visible to the non-admin intuser.

    A non-admin can only reserve a device that one of their user groups has
    been granted via a device group (see reservations create-route visibility
    enforcement). A bare `fresh_device` lands only in the default "No Pool"
    group, which carries no user-group permissions, so the intuser cannot see
    or reserve it. This fixture wires up the real-world prerequisite: an
    isolated user group containing the intuser, a device group holding the
    device, and a permission linking the two. Yields the device dict.
    """
    suffix = uuid.uuid4().hex[:8]

    # Resolve the intuser's id from their own token.
    async with httpx.AsyncClient(
        base_url=base_url,
        verify=False,
        headers={"Authorization": f"Bearer {user_token}"},
        timeout=30.0,
    ) as uclient:
        me_resp = await uclient.get("/auth/me")
        me_resp.raise_for_status()
        user_id = me_resp.json()["id"]

    user_group_id = None
    device_group_id = None
    try:
        # Isolated user group containing only the intuser.
        ug_resp = await admin_client.post(
            "/auth/groups",
            json={"name": f"int-viz-ug-{suffix}", "description": "integration visibility"},
        )
        ug_resp.raise_for_status()
        user_group_id = ug_resp.json()["id"]
        add_resp = await admin_client.post(
            f"/auth/groups/{user_group_id}/members/bulk",
            json={"user_ids": [user_id]},
        )
        add_resp.raise_for_status()

        # Device group holding the fresh device, permissioned for that user group.
        dg_resp = await admin_client.post(
            "/inventory/device-groups",
            json={"name": f"int-viz-dg-{suffix}", "description": "integration visibility"},
        )
        dg_resp.raise_for_status()
        device_group_id = dg_resp.json()["id"]
        add_dev = await admin_client.post(
            f"/inventory/device-groups/{device_group_id}/devices/bulk",
            json={"device_ids": [fresh_device["id"]]},
        )
        add_dev.raise_for_status()
        grant = await admin_client.post(
            f"/inventory/device-groups/{device_group_id}/permissions/bulk",
            json={"user_group_ids": [user_group_id]},
        )
        grant.raise_for_status()

        yield fresh_device
    finally:
        # Device deletion is owned by the fresh_device fixture; clean up the
        # group scaffolding here. Best-effort so teardown never masks a failure.
        if device_group_id:
            try:
                await admin_client.delete(f"/inventory/device-groups/{device_group_id}")
            except Exception:
                pass
        if user_group_id:
            try:
                await admin_client.delete(f"/auth/groups/{user_group_id}")
            except Exception:
                pass


@pytest.fixture
async def fresh_devices(admin_client, dut_template):
    """Factory fixture: call to create and track multiple throwaway devices."""
    created: list[dict] = []

    async def _make(count: int = 1) -> list[dict]:
        for _ in range(count):
            created.append(await _create_fresh_device(admin_client, dut_template["id"]))
        return created[-count:]

    yield _make
    for device in created:
        try:
            await admin_client.delete(f"/inventory/devices/{device['id']}")
        except Exception:
            pass
