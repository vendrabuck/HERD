"""Integration tests for ACL permission flows against a running HERD stack.

Every test builds the condition the rule acts on (issue #1147): a fresh group
holding the integration user, so a grant to that group is one the live
/acl/check (which asks auth for the caller's groups) can honor, and every check
reads the `allowed` flag, never just the status code (a denial is a 200 too).
The group and every grant are deleted on teardown.
"""

import uuid

import pytest

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def member_group(admin_client, user_client):
    """A fresh auth group whose only member is the integration user."""
    me = await user_client.get("/auth/me")
    assert me.status_code == 200, me.text
    user_id = me.json()["id"]
    group_resp = await admin_client.post(
        "/auth/groups",
        json={"name": f"int-acl-group-{uuid.uuid4().hex[:8]}", "description": "integration"},
    )
    assert group_resp.status_code == 201, group_resp.text
    group = group_resp.json()
    try:
        add_resp = await admin_client.post(
            f"/auth/groups/{group['id']}/members/bulk", json={"user_ids": [user_id]}
        )
        assert add_resp.status_code == 200, add_resp.text
        yield {"group": group, "user_id": user_id}
    finally:
        await admin_client.delete(f"/auth/groups/{group['id']}")


async def _grant(admin_client, group_id: str, resource_id: str, permission: str) -> str:
    resp = await admin_client.post(
        "/acl/grants",
        json={
            "group_id": group_id,
            "resource_type": "device",
            "resource_id": resource_id,
            "permission": permission,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _check(user_client, user_id: str, resource_id: str, permission: str) -> dict:
    resp = await user_client.post(
        "/acl/check",
        json={
            "user_id": user_id,
            "resource_type": "device",
            "resource_id": resource_id,
            "permission": permission,
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_grant_and_check_permission(admin_client, user_client, member_group, fresh_device):
    """No grant: denied. A view grant to the user's group: allowed, citing it."""
    group_id = member_group["group"]["id"]
    user_id = member_group["user_id"]

    before = await _check(user_client, user_id, fresh_device["id"], "view")
    assert before["allowed"] is False, before

    grant_id = await _grant(admin_client, group_id, fresh_device["id"], "view")
    try:
        after = await _check(user_client, user_id, fresh_device["id"], "view")
        assert after["allowed"] is True, after
        assert grant_id in [g["id"] for g in after["grants"]], after
    finally:
        await admin_client.delete(f"/acl/grants/{grant_id}")


async def test_delete_grant_denies_access(admin_client, user_client, member_group):
    """A grant that allowed access no longer does once it is deleted."""
    group_id = member_group["group"]["id"]
    user_id = member_group["user_id"]
    temp_device_id = str(uuid.uuid4())

    grant_id = await _grant(admin_client, group_id, temp_device_id, "view")
    try:
        granted = await _check(user_client, user_id, temp_device_id, "view")
        assert granted["allowed"] is True, granted
    finally:
        del_resp = await admin_client.delete(f"/acl/grants/{grant_id}")
    assert del_resp.status_code == 204, del_resp.text

    read_back = await admin_client.get(f"/acl/grants/{grant_id}")
    assert read_back.status_code == 404, read_back.text
    denied = await _check(user_client, user_id, temp_device_id, "view")
    assert denied["allowed"] is False, denied


async def test_batch_check_mixed_results(admin_client, user_client, member_group, fresh_device):
    """Batch check over one granted and one ungranted device answers each one."""
    group_id = member_group["group"]["id"]
    user_id = member_group["user_id"]
    fake_device_id = str(uuid.uuid4())

    grant_id = await _grant(admin_client, group_id, fresh_device["id"], "view")
    try:
        batch_resp = await user_client.post(
            "/acl/check/batch",
            json={
                "user_id": user_id,
                "resource_type": "device",
                "resource_ids": [fresh_device["id"], fake_device_id],
                "permission": "view",
            },
        )
        assert batch_resp.status_code == 200, batch_resp.text
        assert batch_resp.json()["results"] == {
            fresh_device["id"]: True,
            fake_device_id: False,
        }
    finally:
        await admin_client.delete(f"/acl/grants/{grant_id}")


async def test_manage_implies_view_e2e(admin_client, user_client, member_group):
    """A 'manage' grant answers a 'view' check as allowed."""
    group_id = member_group["group"]["id"]
    user_id = member_group["user_id"]
    temp_device_id = str(uuid.uuid4())

    grant_id = await _grant(admin_client, group_id, temp_device_id, "manage")
    try:
        check = await _check(user_client, user_id, temp_device_id, "view")
        assert check["allowed"] is True, check
    finally:
        await admin_client.delete(f"/acl/grants/{grant_id}")
