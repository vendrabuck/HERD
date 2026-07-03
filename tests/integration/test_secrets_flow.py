"""Integration tests for the secrets service against a running HERD stack (issue #39).

Covers the ADR 0003 acceptance flow end to end through the gateway: admin
create (metadata-only response), the ACL grant path (a group member with a
`manage` grant on the secret can reveal; an ungranted user cannot see it at
all), DEK rotation preserving plaintext, and the X-Internal-Token retrieval
surface.

Self-seeding per the integration-test convention: the group, membership, and
grant are created here, not by the seed script.
"""

import os
import uuid

import httpx
import pytest

pytestmark = pytest.mark.asyncio

CANARY = "integration-hunter2-canary"


def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


@pytest.fixture
async def secret(admin_client):
    """A fresh secret; deleted on teardown so reruns stay clean."""
    resp = await admin_client.post(
        "/secrets/secrets",
        json={
            "name": _unique("int-secret"),
            "type": "password",
            "description": "integration test secret",
            "data": {"username": "svc", "password": CANARY},
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    yield body
    await admin_client.delete(f"/secrets/secrets/{body['id']}")


async def test_create_returns_metadata_only(secret):
    assert secret["key_version"] >= 1
    assert "data" not in secret
    assert CANARY not in str(secret)


async def test_admin_reveal_round_trips(admin_client, secret):
    resp = await admin_client.get(f"/secrets/secrets/{secret['id']}/value")
    assert resp.status_code == 200
    assert resp.json()["data"]["password"] == CANARY


async def test_duplicate_name_is_409(admin_client, secret):
    resp = await admin_client.post(
        "/secrets/secrets",
        json={"name": secret["name"], "data": {"k": "v"}},
    )
    assert resp.status_code == 409


async def test_ungranted_user_sees_nothing(user_client, secret):
    """No grant: metadata and value are both 404 (existence not confirmed)."""
    resp = await user_client.get(f"/secrets/secrets/{secret['id']}")
    assert resp.status_code == 404
    resp = await user_client.get(f"/secrets/secrets/{secret['id']}/value")
    assert resp.status_code == 404
    resp = await user_client.get("/secrets/secrets")
    assert resp.status_code == 200
    assert secret["id"] not in [s["id"] for s in resp.json()]


async def test_manage_grant_lets_a_member_reveal(admin_client, user_client, secret):
    """Group + membership + manage grant: the member can list, read, reveal."""
    me = await user_client.get("/auth/me")
    assert me.status_code == 200
    user_id = me.json()["id"]

    group_resp = await admin_client.post(
        "/auth/groups",
        json={"name": _unique("int-secret-group"), "description": "integration"},
    )
    assert group_resp.status_code in (200, 201), group_resp.text
    group_id = group_resp.json()["id"]
    add_resp = await admin_client.post(
        f"/auth/groups/{group_id}/members/bulk", json={"user_ids": [user_id]}
    )
    assert add_resp.status_code in (200, 201, 204), add_resp.text

    grant_resp = await admin_client.post(
        "/acl/grants",
        json={
            "group_id": group_id,
            "resource_type": "secret",
            "resource_id": secret["id"],
            "permission": "manage",
        },
    )
    assert grant_resp.status_code == 201, grant_resp.text

    resp = await user_client.get("/secrets/secrets")
    assert secret["id"] in [s["id"] for s in resp.json()]
    resp = await user_client.get(f"/secrets/secrets/{secret['id']}")
    assert resp.status_code == 200
    assert "data" not in resp.json()
    resp = await user_client.get(f"/secrets/secrets/{secret['id']}/value")
    assert resp.status_code == 200
    assert resp.json()["data"]["password"] == CANARY


async def test_non_admin_cannot_create_or_rotate(user_client):
    resp = await user_client.post(
        "/secrets/secrets", json={"name": _unique("nope"), "data": {"k": "v"}}
    )
    assert resp.status_code == 403
    resp = await user_client.post("/secrets/keys/rotate")
    assert resp.status_code == 403


async def test_rotation_preserves_plaintext(admin_client, secret):
    before = (await admin_client.get(f"/secrets/secrets/{secret['id']}")).json()["key_version"]
    rotate = await admin_client.post("/secrets/keys/rotate")
    assert rotate.status_code == 200
    assert rotate.json()["new_version"] == before + 1
    resp = await admin_client.get(f"/secrets/secrets/{secret['id']}/value")
    assert resp.json()["data"]["password"] == CANARY


async def test_internal_token_retrieval(base_url, secret):
    internal_token = os.getenv("INTERNAL_API_TOKEN")
    if not internal_token:
        pytest.skip("INTERNAL_API_TOKEN not set in this environment")
    async with httpx.AsyncClient(base_url=base_url, verify=False, timeout=30.0) as client:
        resp = await client.get(
            f"/secrets/internal/secrets/{secret['id']}/value",
            headers={"X-Internal-Token": internal_token},
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["password"] == CANARY

        by_name = await client.get(
            f"/secrets/internal/secrets/by-name/{secret['name']}/value",
            headers={"X-Internal-Token": internal_token},
        )
        assert by_name.status_code == 200
        assert by_name.json()["id"] == secret["id"]

        denied = await client.get(
            f"/secrets/internal/secrets/{secret['id']}/value",
            headers={"X-Internal-Token": "definitely-wrong"},
        )
        assert denied.status_code == 403
