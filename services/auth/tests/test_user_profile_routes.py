"""Functional tests for the user profile routes (issue #1252).

PATCH /me/profile and PATCH /users/{user_id}/profile, driven over HTTP with
REAL signed access tokens (make_client() with no user leaves get_current_user
and the effective-role path un-overridden), plus direct handler calls where a
test must see that a refusal makes no lookup at all. Every write assertion
reads the STORED row back through its own session, never only the response.

Rule ids are the specification's IAM-PROFILE-1 to IAM-PROFILE-8 in
docs/specs/identity-and-access.md.
"""

import contextlib
import json
import logging
import re
import uuid
from unittest.mock import patch

import pytest
from app.models.user import Role, User
from app.services.auth_service import create_ldap_user, create_user, update_user_profile
from app.utils.jwt import create_access_token
from fastapi import HTTPException
from herd_common.logging import JSONFormatter
from sqlalchemy import event

from tests._harness import TestSessionLocal, engine

_NOT_FOUND = {"detail": "User not found"}


async def _make_user(
    role: Role = Role.USER, *, username: str, is_active: bool = True, full_name: str | None = None
) -> User:
    async with TestSessionLocal() as session:
        user = User(
            id=uuid.uuid4(),
            email=f"{username}@test.com",
            username=username,
            hashed_password="fake",
            is_active=is_active,
            role=role,
            **({"full_name": full_name} if full_name is not None else {}),
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
    return user


def _auth(user: User, role_claim: Role | None = None) -> dict:
    claim = (role_claim or user.role).value
    token = create_access_token(
        {"sub": str(user.id), "username": user.username, "email": user.email, "role": claim}
    )
    return {"Authorization": f"Bearer {token}"}


async def _stored(user_id: uuid.UUID) -> User:
    async with TestSessionLocal() as session:
        return await session.get(User, user_id)


@pytest.fixture
async def client(make_client):
    async with make_client() as ac:
        yield ac


# --- PATCH /me/profile ---


@pytest.mark.asyncio
async def test_self_edit_writes_all_three_fields_and_sets_modified_by(client):
    user = await _make_user(username="selfie")
    resp = await client.patch(
        "/me/profile",
        json={
            "full_name": "  Selma Fie ",
            "contact_email": "selma@mail.example.com",
            "phone": "+1 (555) 010-0101",
        },
        headers=_auth(user),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["full_name"] == "Selma Fie"
    assert body["contact_email"] == "selma@mail.example.com"
    assert body["phone"] == "+1 (555) 010-0101"
    row = await _stored(user.id)
    assert (row.full_name, row.contact_email, row.phone) == (
        "Selma Fie",
        "selma@mail.example.com",
        "+1 (555) 010-0101",
    )
    assert row.modified_by == user.id
    # The login identity is untouched.
    assert (row.email, row.username) == ("selfie@test.com", "selfie")


@pytest.mark.asyncio
async def test_me_reports_the_profile_fields_after_a_self_edit(client):
    user = await _make_user(username="reader")
    before = await client.get("/me", headers=_auth(user))
    assert before.status_code == 200
    assert (before.json()["full_name"], before.json()["contact_email"], before.json()["phone"]) == (
        "reader",
        None,
        None,
    )
    await client.patch(
        "/me/profile",
        json={"full_name": "Rea Der", "contact_email": "rd@example.com", "phone": "555 0102"},
        headers=_auth(user),
    )
    after = (await client.get("/me", headers=_auth(user))).json()
    assert (after["full_name"], after["contact_email"], after["phone"]) == (
        "Rea Der",
        "rd@example.com",
        "555 0102",
    )


@pytest.mark.asyncio
async def test_omitted_keys_are_left_unchanged(client):
    user = await _make_user(username="partial")
    await client.patch(
        "/me/profile",
        json={"full_name": "Par Tial", "contact_email": "p@example.com", "phone": "555"},
        headers=_auth(user),
    )
    resp = await client.patch("/me/profile", json={"phone": "666"}, headers=_auth(user))
    assert resp.status_code == 200
    row = await _stored(user.id)
    assert (row.full_name, row.contact_email, row.phone) == ("Par Tial", "p@example.com", "666")


@pytest.mark.asyncio
async def test_empty_string_and_null_clear_contact_email_and_phone(client):
    user = await _make_user(username="clearer")
    await client.patch(
        "/me/profile",
        json={"contact_email": "c@example.com", "phone": "555"},
        headers=_auth(user),
    )
    resp = await client.patch(
        "/me/profile", json={"contact_email": "", "phone": None}, headers=_auth(user)
    )
    assert resp.status_code == 200
    assert (resp.json()["contact_email"], resp.json()["phone"]) == (None, None)
    row = await _stored(user.id)
    assert (row.contact_email, row.phone) == (None, None)
    assert row.full_name == "clearer"


@pytest.mark.asyncio
async def test_null_full_name_is_a_422_naming_the_field_and_writes_nothing(client):
    user = await _make_user(username="nonull")
    resp = await client.patch("/me/profile", json={"full_name": None}, headers=_auth(user))
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert [(d["loc"], d["msg"]) for d in detail] == [
        (["body", "full_name"], "Value error, full_name may not be null")
    ]
    assert (await _stored(user.id)).full_name == "nonull"


@pytest.mark.asyncio
async def test_empty_full_name_is_a_422_naming_the_field(client):
    user = await _make_user(username="noempty")
    resp = await client.patch("/me/profile", json={"full_name": "   "}, headers=_auth(user))
    assert resp.status_code == 422
    assert [(d["loc"], d["msg"]) for d in resp.json()["detail"]] == [
        (["body", "full_name"], "String should have at least 1 character")
    ]
    assert (await _stored(user.id)).full_name == "noempty"


@pytest.mark.parametrize(
    "key,value",
    [("role", "admin"), ("email", "new@test.com"), ("username", "newname"), ("is_active", False)],
)
@pytest.mark.asyncio
async def test_non_profile_key_is_a_422_and_the_account_is_unchanged(client, key, value):
    user = await _make_user(username="forbid")
    resp = await client.patch(
        "/me/profile", json={"full_name": "Changed", key: value}, headers=_auth(user)
    )
    assert resp.status_code == 422
    assert [(d["loc"], d["type"]) for d in resp.json()["detail"]] == [
        (["body", key], "extra_forbidden")
    ]
    row = await _stored(user.id)
    assert (row.full_name, row.role, row.email, row.username, row.is_active) == (
        "forbid",
        Role.USER,
        "forbid@test.com",
        "forbid",
        True,
    )


@pytest.mark.asyncio
async def test_me_profile_requires_a_signed_in_caller(client):
    resp = await client.patch("/me/profile", json={"full_name": "Nobody"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_ldap_account_may_edit_its_profile(client):
    async with TestSessionLocal() as db:
        user = await create_ldap_user(db, "dir@example.com", "diruser")
    assert user.full_name == "diruser"
    resp = await client.patch(
        "/me/profile",
        json={"full_name": "Dir User", "contact_email": "dir.alt@example.com"},
        headers=_auth(user),
    )
    assert resp.status_code == 200
    row = await _stored(user.id)
    assert (row.full_name, row.contact_email, row.auth_source, row.email) == (
        "Dir User",
        "dir.alt@example.com",
        "ldap",
        "dir@example.com",
    )


# --- PATCH /users/{user_id}/profile: the self-or-admin rule ---


@pytest.mark.asyncio
async def test_user_naming_another_id_gets_404_byte_identical_to_an_unknown_id(client):
    caller = await _make_user(username="nosy")
    other = await _make_user(username="private", full_name="Private Person")
    other_resp = await client.patch(
        f"/users/{other.id}/profile", json={"full_name": "Hacked"}, headers=_auth(caller)
    )
    unknown_resp = await client.patch(
        f"/users/{uuid.uuid4()}/profile", json={"full_name": "Hacked"}, headers=_auth(caller)
    )
    assert other_resp.status_code == 404
    assert unknown_resp.status_code == 404
    assert other_resp.content == unknown_resp.content
    assert other_resp.json() == _NOT_FOUND
    row = await _stored(other.id)
    assert row.full_name == "Private Person"
    assert row.modified_by is None


@pytest.mark.asyncio
async def test_user_naming_another_inactive_id_gets_the_same_404(client):
    caller = await _make_user(username="nosy2")
    other = await _make_user(username="gone", is_active=False)
    resp = await client.patch(
        f"/users/{other.id}/profile", json={"phone": "555"}, headers=_auth(caller)
    )
    assert resp.status_code == 404
    assert resp.json() == _NOT_FOUND
    assert (await _stored(other.id)).phone is None


@pytest.mark.asyncio
async def test_user_naming_own_id_on_the_id_route_edits_own_profile(client):
    user = await _make_user(username="ownid")
    resp = await client.patch(
        f"/users/{user.id}/profile", json={"full_name": "Own Id"}, headers=_auth(user)
    )
    assert resp.status_code == 200
    row = await _stored(user.id)
    assert row.full_name == "Own Id"
    assert row.modified_by == user.id


@pytest.mark.asyncio
async def test_admin_edits_another_users_profile_and_is_recorded_as_modifier(client):
    admin = await _make_user(Role.ADMIN, username="adm")
    target = await _make_user(username="tgt")
    resp = await client.patch(
        f"/users/{target.id}/profile",
        json={"full_name": "Tar Get", "contact_email": "tg@example.com", "phone": "(555) 0103"},
        headers=_auth(admin),
    )
    assert resp.status_code == 200
    assert resp.json()["id"] == str(target.id)
    row = await _stored(target.id)
    assert (row.full_name, row.contact_email, row.phone) == (
        "Tar Get",
        "tg@example.com",
        "(555) 0103",
    )
    assert row.modified_by == admin.id
    assert (await _stored(admin.id)).modified_by is None


@pytest.mark.asyncio
async def test_admin_edits_an_inactive_users_profile(client):
    admin = await _make_user(Role.ADMIN, username="adm2")
    target = await _make_user(username="dormant", is_active=False)
    resp = await client.patch(
        f"/users/{target.id}/profile", json={"full_name": "Dor Mant"}, headers=_auth(admin)
    )
    assert resp.status_code == 200
    row = await _stored(target.id)
    assert (row.full_name, row.is_active, row.modified_by) == ("Dor Mant", False, admin.id)


@pytest.mark.asyncio
async def test_superadmin_edits_an_admins_profile(client):
    superadmin = await _make_user(Role.SUPERADMIN, username="root")
    admin = await _make_user(Role.ADMIN, username="adm3")
    resp = await client.patch(
        f"/users/{admin.id}/profile", json={"phone": "555 0104"}, headers=_auth(superadmin)
    )
    assert resp.status_code == 200
    row = await _stored(admin.id)
    assert (row.phone, row.modified_by, row.role) == ("555 0104", superadmin.id, Role.ADMIN)


@pytest.mark.asyncio
async def test_admin_edits_the_superadmins_profile(client):
    admin = await _make_user(Role.ADMIN, username="adm4")
    superadmin = await _make_user(Role.SUPERADMIN, username="root2")
    resp = await client.patch(
        f"/users/{superadmin.id}/profile",
        json={"contact_email": "root.alerts@example.com"},
        headers=_auth(admin),
    )
    assert resp.status_code == 200
    row = await _stored(superadmin.id)
    assert (row.contact_email, row.modified_by, row.role) == (
        "root.alerts@example.com",
        admin.id,
        Role.SUPERADMIN,
    )


@pytest.mark.asyncio
async def test_admin_naming_an_unknown_id_gets_404(client):
    admin = await _make_user(Role.ADMIN, username="adm5")
    resp = await client.patch(
        f"/users/{uuid.uuid4()}/profile", json={"full_name": "Ghost"}, headers=_auth(admin)
    )
    assert resp.status_code == 404
    assert resp.json() == _NOT_FOUND


@pytest.mark.asyncio
async def test_admin_account_holding_a_user_claim_token_is_judged_as_user(client):
    """The decision uses the EFFECTIVE role: an admin account presenting a token
    whose role claim is user (an API-token exchange clamp, or a token minted
    before a promotion) may not edit another account."""
    admin = await _make_user(Role.ADMIN, username="clamped")
    target = await _make_user(username="tgt2")
    resp = await client.patch(
        f"/users/{target.id}/profile",
        json={"full_name": "Hacked"},
        headers=_auth(admin, role_claim=Role.USER),
    )
    assert resp.status_code == 404
    assert resp.json() == _NOT_FOUND
    assert (await _stored(target.id)).full_name == "tgt2"


@pytest.mark.asyncio
async def test_demoted_account_holding_an_admin_claim_token_is_judged_as_user(client):
    """The other direction: a token minted while the account was admin no longer
    grants admin once the database role is user."""
    demoted = await _make_user(Role.USER, username="demoted")
    target = await _make_user(username="tgt3")
    resp = await client.patch(
        f"/users/{target.id}/profile",
        json={"full_name": "Hacked"},
        headers=_auth(demoted, role_claim=Role.ADMIN),
    )
    assert resp.status_code == 404
    assert (await _stored(target.id)).full_name == "tgt3"


@pytest.mark.asyncio
async def test_id_route_refuses_a_non_profile_key_even_for_an_admin(client):
    admin = await _make_user(Role.ADMIN, username="adm6")
    target = await _make_user(username="tgt4")
    resp = await client.patch(
        f"/users/{target.id}/profile", json={"role": "admin"}, headers=_auth(admin)
    )
    assert resp.status_code == 422
    assert [(d["loc"], d["type"]) for d in resp.json()["detail"]] == [
        (["body", "role"], "extra_forbidden")
    ]
    assert (await _stored(target.id)).role == Role.USER


# --- direct handler calls: no lookup before the refusal ---


@contextlib.contextmanager
def _users_table_statements():
    """Record every SQL statement the test engine sends that names the users table.

    Pinned at the database, not at a Python name: a lookup added anywhere in the
    handler, through any import binding, still reaches the engine as a statement.
    """
    seen: list[str] = []

    def _record(conn, cursor, statement, parameters, context, executemany):
        if _USERS_TABLE.search(statement):
            seen.append(statement)

    sync_engine = engine.sync_engine
    event.listen(sync_engine, "before_cursor_execute", _record)
    try:
        yield seen
    finally:
        event.remove(sync_engine, "before_cursor_execute", _record)


_USERS_TABLE = re.compile(r"\busers\b")


@pytest.mark.asyncio
async def test_user_refusal_on_another_id_makes_no_lookup():
    """The 404 for a user naming another id is decided before any read of the
    named account, so its timing and answer cannot depend on the target. The
    caller is already resolved (passed in, as get_current_user would hand it
    over), so the handler must send NO statement naming the users table."""
    from app.routers.admin import update_user_profile_by_id
    from app.schemas.auth import ProfileUpdateRequest

    caller = await _make_user(username="direct")
    other = await _make_user(username="direct-other")
    with patch("app.routers.admin.update_user_profile", wraps=update_user_profile) as write:
        for target_id in (other.id, uuid.uuid4()):
            async with TestSessionLocal() as db:
                with _users_table_statements() as seen:
                    with pytest.raises(HTTPException) as exc:
                        await update_user_profile_by_id(
                            target_id,
                            ProfileUpdateRequest(full_name="X"),
                            db=db,
                            current_user=caller,
                            caller_role=Role.USER,
                        )
            assert (exc.value.status_code, exc.value.detail) == (404, "User not found")
            assert seen == []
    write.assert_not_called()
    assert (await _stored(other.id)).full_name == "direct-other"


@pytest.mark.asyncio
async def test_user_refusal_over_http_reads_no_users_row_after_the_caller(make_client):
    """The same invariant through the real route and dependency chain. The caller
    is supplied by the get_current_user override, so every statement naming the
    users table during the request would be a read of the named account."""
    caller = await _make_user(username="http-caller")
    other = await _make_user(username="http-other")
    async with make_client(caller) as ac:
        with _users_table_statements() as seen:
            resp = await ac.patch(f"/users/{other.id}/profile", json={"full_name": "Hacked"})
    assert resp.status_code == 404
    assert resp.json() == _NOT_FOUND
    assert seen == []
    # Control: the same harness DOES see the lookup an allowed self edit makes,
    # so an empty list above is not a listener that records nothing.
    async with make_client(caller) as ac:
        with _users_table_statements() as seen_self:
            ok = await ac.patch(f"/users/{caller.id}/profile", json={"full_name": "Self"})
    assert ok.status_code == 200
    assert seen_self != []


@pytest.mark.asyncio
async def test_update_user_profile_refuses_a_non_profile_column_and_writes_nothing():
    """The second line behind extra="forbid": the service function itself
    refuses any column outside PROFILE_FIELDS before reading or writing."""
    target = await _make_user(username="svc-target")
    admin = await _make_user(Role.ADMIN, username="svc-admin")
    async with TestSessionLocal() as db:
        with pytest.raises(ValueError) as exc:
            await update_user_profile(
                db, target.id, {"role": Role.ADMIN, "full_name": "X"}, modified_by=admin.id
            )
    assert str(exc.value) == "Not profile fields: ['role']"
    row = await _stored(target.id)
    assert (row.role, row.full_name, row.modified_by) == (Role.USER, "svc-target", None)


@pytest.mark.asyncio
async def test_admin_direct_call_writes_with_the_caller_as_modifier():
    from app.routers.admin import update_user_profile_by_id
    from app.schemas.auth import ProfileUpdateRequest

    admin = await _make_user(Role.ADMIN, username="direct-adm")
    target = await _make_user(username="direct-tgt")
    async with TestSessionLocal() as db:
        result = await update_user_profile_by_id(
            target.id,
            ProfileUpdateRequest(full_name="Direct Target"),
            db=db,
            current_user=admin,
            caller_role=Role.ADMIN,
        )
    assert result.full_name == "Direct Target"
    assert (await _stored(target.id)).modified_by == admin.id


# --- register ---


@pytest.mark.asyncio
async def test_register_without_full_name_stores_the_username(client):
    resp = await client.post(
        "/register",
        json={"email": "nofn@example.com", "username": "nofn", "password": "password123"},
    )
    assert resp.status_code == 201
    assert resp.json()["full_name"] == "nofn"
    row = await _stored(uuid.UUID(resp.json()["id"]))
    assert row.full_name == "nofn"


@pytest.mark.asyncio
async def test_register_with_full_name_stores_it_stripped(client):
    resp = await client.post(
        "/register",
        json={
            "email": "withfn@example.com",
            "username": "withfn",
            "password": "password123",
            "full_name": "  With Name ",
        },
    )
    assert resp.status_code == 201
    row = await _stored(uuid.UUID(resp.json()["id"]))
    assert row.full_name == "With Name"
    assert (row.contact_email, row.phone) == (None, None)


@pytest.mark.asyncio
async def test_create_user_without_a_name_defaults_to_the_username():
    """The superadmin seed and seedtools call create_user with no name."""
    async with TestSessionLocal() as db:
        user = await create_user(db, "seed@example.com", "seeded", "password123", Role.SUPERADMIN)
    assert (await _stored(user.id)).full_name == "seeded"


# --- the one INFO line ---


@pytest.mark.asyncio
async def test_profile_write_logs_one_line_with_changed_field_names_and_no_values(client, caplog):
    user = await _make_user(username="logged")
    await client.patch(
        "/me/profile", json={"contact_email": "first@example.com"}, headers=_auth(user)
    )
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="app.services.auth_service"):
        resp = await client.patch(
            "/me/profile",
            json={
                "full_name": "Log Ged",
                "contact_email": "first@example.com",
                "phone": "555 0199",
            },
            headers=_auth(user),
        )
    assert resp.status_code == 200
    records = [r for r in caplog.records if getattr(r, "action", None) == "user_profile_updated"]
    assert len(records) == 1
    rendered = JSONFormatter("auth").format(records[0])
    line = json.loads(rendered)
    assert line["level"] == "INFO"
    assert line["action"] == "user_profile_updated"
    assert line["user_id"] == str(user.id)
    # contact_email was sent unchanged, so it is not a changed field.
    assert line["changed_fields"] == ["full_name", "phone"]
    for value in ("Log Ged", "first@example.com", "555 0199"):
        assert value not in rendered
