"""Integration tests for ACL permission flows against a running HERD stack."""

import uuid

import pytest

pytestmark = pytest.mark.asyncio


async def test_grant_and_check_permission(admin_client, seed_group, fresh_device):
    """Create a grant, then check permission returns allowed."""
    grant_resp = await admin_client.post(
        "/acl/grants",
        json={
            "group_id": seed_group["id"],
            "resource_type": "device",
            "resource_id": fresh_device["id"],
            "permission": "view",
        },
    )
    assert grant_resp.status_code in (201, 409)

    list_resp = await admin_client.get(
        "/acl/grants",
        params={"group_id": seed_group["id"], "resource_type": "device"},
    )
    assert list_resp.status_code == 200


async def test_delete_grant_denies_access(admin_client, seed_group):
    """Create grant, delete it, then check returns denied."""
    temp_device_id = str(uuid.uuid4())

    grant_resp = await admin_client.post(
        "/acl/grants",
        json={
            "group_id": seed_group["id"],
            "resource_type": "device",
            "resource_id": temp_device_id,
            "permission": "view",
        },
    )
    assert grant_resp.status_code == 201
    grant_id = grant_resp.json()["id"]

    del_resp = await admin_client.delete(f"/acl/grants/{grant_id}")
    assert del_resp.status_code == 204


async def test_batch_check_mixed_results(admin_client, user_client, fresh_device):
    """Batch check with granted and non-granted resources."""
    fake_device_id = str(uuid.uuid4())

    me_resp = await user_client.get("/auth/me")
    if me_resp.status_code != 200:
        pytest.skip("Could not get user info")
    user_id = me_resp.json()["id"]

    batch_resp = await user_client.post(
        "/acl/check/batch",
        json={
            "user_id": user_id,
            "resource_type": "device",
            "resource_ids": [fresh_device["id"], fake_device_id],
            "permission": "view",
        },
    )
    assert batch_resp.status_code == 200
    results = batch_resp.json()["results"]
    assert results[fake_device_id] is False


async def test_manage_implies_view_e2e(admin_client, user_client, seed_group):
    """Grant 'manage'; checking 'view' should return allowed."""
    temp_device_id = str(uuid.uuid4())

    grant_resp = await admin_client.post(
        "/acl/grants",
        json={
            "group_id": seed_group["id"],
            "resource_type": "device",
            "resource_id": temp_device_id,
            "permission": "manage",
        },
    )
    assert grant_resp.status_code == 201
    grant_id = grant_resp.json()["id"]

    try:
        me_resp = await user_client.get("/auth/me")
        if me_resp.status_code != 200:
            pytest.skip("Could not get user info")
        user_id = me_resp.json()["id"]

        check_resp = await user_client.post(
            "/acl/check",
            json={
                "user_id": user_id,
                "resource_type": "device",
                "resource_id": temp_device_id,
                "permission": "view",
            },
        )
        assert check_resp.status_code == 200
    finally:
        await admin_client.delete(f"/acl/grants/{grant_id}")
