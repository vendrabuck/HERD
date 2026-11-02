"""Integration tests for the user profile (issue #1252) against a running stack.

Through the gateway: a self edit read back through /auth/me, a user naming
another account's id refused with the same 404 as an unknown id, an admin edit
read back through the admin user list, and auth's internal contact route (the
one notifications calls) answering contact_email when set and the login email
after it is cleared. Every edit is restored in a finally.

There is no SMTP sink in the stack, so "the email is addressed to
contact_email" is proven at the functional level in
services/notifications/tests/test_email_dispatcher.py, not here.
"""

import os
import uuid

import httpx
import pytest

_NOT_FOUND = {"detail": "User not found"}
_PROFILE_KEYS = ("full_name", "contact_email", "phone")


def _profile_of(user: dict) -> dict:
    return {key: user[key] for key in _PROFILE_KEYS}


async def _me(client: httpx.AsyncClient) -> dict:
    resp = await client.get("/auth/me")
    resp.raise_for_status()
    return resp.json()


async def _restore_self(client: httpx.AsyncClient, baseline: dict) -> None:
    resp = await client.patch("/auth/me/profile", json=baseline)
    assert resp.status_code == 200, resp.text
    assert _profile_of(resp.json()) == baseline


async def _find_user(admin_client: httpx.AsyncClient, user_id: str) -> dict:
    skip = 0
    while True:
        resp = await admin_client.get("/auth/users", params={"skip": skip, "limit": 500})
        resp.raise_for_status()
        page = resp.json()
        for item in page["items"]:
            if item["id"] == user_id:
                return item
        skip += len(page["items"])
        if not page["items"] or skip >= page["total"]:
            raise AssertionError(f"user {user_id} not in GET /auth/users")


@pytest.mark.asyncio
async def test_self_profile_edit_reads_back_through_me(user_client):
    before = await _me(user_client)
    baseline = _profile_of(before)
    suffix = uuid.uuid4().hex[:8]
    wanted = {
        "full_name": f"Integration User {suffix}",
        "contact_email": f"it-{suffix}@mail.example.com",
        "phone": "+1 (555) 010-0142",
    }
    try:
        resp = await user_client.patch("/auth/me/profile", json=wanted)
        assert resp.status_code == 200, resp.text
        after = await _me(user_client)
        assert _profile_of(after) == wanted
        assert after["modified_by"] == before["id"]
        assert (after["email"], after["username"]) == (before["email"], before["username"])
    finally:
        await _restore_self(user_client, baseline)


@pytest.mark.asyncio
async def test_user_naming_another_id_gets_the_unknown_id_404(user_client, admin_client):
    admin_before = await _me(admin_client)
    other = await user_client.patch(
        f"/auth/users/{admin_before['id']}/profile", json={"full_name": "Not Allowed"}
    )
    unknown = await user_client.patch(
        f"/auth/users/{uuid.uuid4()}/profile", json={"full_name": "Not Allowed"}
    )
    assert other.status_code == 404
    assert unknown.status_code == 404
    assert other.content == unknown.content
    assert other.json() == _NOT_FOUND
    assert _profile_of(await _me(admin_client)) == _profile_of(admin_before)


@pytest.mark.asyncio
async def test_admin_edits_a_users_profile_and_reads_it_back_from_the_user_list(
    admin_client, user_client
):
    user = await _me(user_client)
    admin = await _me(admin_client)
    baseline = _profile_of(user)
    suffix = uuid.uuid4().hex[:8]
    wanted = {
        "full_name": f"Admin Edited {suffix}",
        "contact_email": None,
        "phone": "555 0143",
    }
    try:
        resp = await admin_client.patch(f"/auth/users/{user['id']}/profile", json=wanted)
        assert resp.status_code == 200, resp.text
        listed = await _find_user(admin_client, user["id"])
        assert _profile_of(listed) == wanted
        assert listed["modified_by"] == admin["id"]
    finally:
        restore = await admin_client.patch(f"/auth/users/{user['id']}/profile", json=baseline)
        assert restore.status_code == 200, restore.text
        assert _profile_of(restore.json()) == baseline


@pytest.mark.asyncio
async def test_contact_route_answers_contact_email_then_the_login_email(base_url, user_client):
    internal_token = os.getenv("INTERNAL_API_TOKEN")
    if not internal_token:
        pytest.skip("INTERNAL_API_TOKEN not set in the test environment")
    user = await _me(user_client)
    baseline = _profile_of(user)
    contact = f"it-contact-{uuid.uuid4().hex[:8]}@mail.example.com"
    try:
        async with httpx.AsyncClient(
            base_url=base_url,
            verify=False,
            headers={"X-Internal-Token": internal_token},
            timeout=30.0,
        ) as internal_client:
            path = f"/auth/internal/users/{user['id']}/contact"
            resp = await user_client.patch("/auth/me/profile", json={"contact_email": contact})
            assert resp.status_code == 200, resp.text
            with_contact = await internal_client.get(path)
            assert with_contact.status_code == 200, with_contact.text
            assert with_contact.json() == {
                "user_id": user["id"],
                "email": contact,
                "username": user["username"],
            }

            resp = await user_client.patch("/auth/me/profile", json={"contact_email": ""})
            assert resp.status_code == 200, resp.text
            cleared = await internal_client.get(path)
            assert cleared.status_code == 200, cleared.text
            assert cleared.json() == {
                "user_id": user["id"],
                "email": user["email"],
                "username": user["username"],
            }
    finally:
        await _restore_self(user_client, baseline)
