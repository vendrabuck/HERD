"""The superadmin federated-identity routes through the real auth app (issue #1399).

Functional level: `POST /admin/federated-identities/rewrite-issuer` and
`PUT /users/{user_id}/federated-identity` over HTTP (ASGI transport, in-memory
SQLite) with REAL access tokens through the real decode and effective-role
path, plus the recovery stories end to end through the single sign-on routes of
ADR 0016 phase 3 against the fake identity provider of `test_oidc_routes.py`:

- an `OIDC_ISSUER` change (the trailing-slash case the issue names): the next
  sign-in is refused and creates nothing, the rewrite runs, and the next sign-in
  lands in the SAME account;
- a reissued subject: the sign-in is refused, the relink runs, the next sign-in
  lands in the same account, and the old subject is refused from then on;
- the D4 threat: a provider account holding the victim's email but another
  subject is refused before and after a relink that names a different subject.

Also pinned: the role gate for every caller class (no token, user, admin, an
admin claim on a superadmin row, an inactive superadmin, and a superadmin from
the OIDC break-glass login and from an API-token exchange), the gate running
before the body is validated and before any lookup, each refusal's exact status
and detail, the 422 body shape, both routes answering in every auth mode, the
audit line of every outcome with no issuer or subject in any record, and a
database error answering a fixed 500 logged by class name only.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from app.config import _absolute_url_ok, settings
from app.models.federated_identity import FederatedIdentity
from app.models.federated_relink_candidate import (
    FederatedRelinkCandidate,
    FederatedRelinkCaptureTotal,
)
from app.models.user import Role, User
from app.routers import federated_identities as routes
from app.schemas.federated_identity import FederatedIdentityLinkRequest
from app.services import auth_service, federated_identity_service, oidc_service
from app.services.api_token_service import create_api_token
from app.services.auth_service import FederatedLogin, get_password_hash, resolve_federated_user
from app.services.federated_identity_service import identity_fingerprint
from app.utils.jwt import create_access_token
from cryptography.hazmat.primitives.asymmetric import rsa
from herd_common.logging import JSONFormatter
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError

from tests._harness import TestSessionLocal
from tests.test_oidc_routes import (
    CLIENT_ID,
    CLIENT_SECRET,
    EMAIL,
    FAILED,
    ISSUER,
    REDIRECT_URI,
    SUBJECT,
    FakeIdP,
    begin,
    complete_callback,
    exchange,
    sign_in,
)

REWRITE = "/admin/federated-identities/rewrite-issuer"
NEW_ISSUER = ISSUER + "/"
REISSUED = "SENTINEL-subject-reissued"
FORBIDDEN = {"detail": "You do not have permission to perform this action"}
NOT_AUTHENTICATED = {"detail": "Not authenticated"}
BAD_CREDENTIALS = {"detail": "Could not validate credentials"}
SA_PASSWORD = "superadmin-password-1"
NO_COLLISIONS = {"collisions": [], "collisions_truncated": False}


def relink_path(user_id) -> str:
    return f"/users/{user_id}/federated-identity"


async def make_user(
    name: str,
    *,
    role: Role = Role.USER,
    source: str = "local",
    is_active: bool = True,
    password: str | None = None,
) -> User:
    async with TestSessionLocal() as db:
        user = User(
            email=f"{name}@example.test",
            username=name,
            full_name=name,
            hashed_password=get_password_hash(password) if password else None,
            auth_source=source,
            role=role,
            is_active=is_active,
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)
        return user


async def make_oidc(name: str, issuer: str = ISSUER, subject: str | None = None) -> User:
    user = await make_user(name, source="oidc")
    async with TestSessionLocal() as db:
        db.add(FederatedIdentity(user_id=user.id, issuer=issuer, subject=subject or f"sub-{name}"))
        await db.commit()
    return user


def bearer(user: User, role_claim: Role | None = None) -> dict[str, str]:
    token = create_access_token(
        {
            "sub": str(user.id),
            "username": user.username,
            "email": user.email,
            "role": (role_claim or user.role).value,
        }
    )
    return {"Authorization": f"Bearer {token}"}


async def identities() -> dict[uuid.UUID, tuple[str, str]]:
    async with TestSessionLocal() as db:
        rows = (await db.execute(select(FederatedIdentity))).scalars().all()
    return {r.user_id: (r.issuer, r.subject) for r in rows}


async def counts() -> tuple[int, int]:
    async with TestSessionLocal() as db:
        users = (await db.execute(select(func.count()).select_from(User))).scalar_one()
        ids = (await db.execute(select(func.count()).select_from(FederatedIdentity))).scalar_one()
    return users, ids


def audit_records(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == routes.logger.name]


def assert_no_identity_text(caplog, *values: str) -> None:
    formatter = JSONFormatter("auth")
    for record in audit_records(caplog):
        text = formatter.format(record)
        for value in values:
            assert value not in text, f"{value!r} logged in {text}"


@pytest.fixture
async def client(make_client):
    async with make_client() as ac:
        yield ac


@pytest.fixture
async def superadmin() -> User:
    return await make_user("root", role=Role.SUPERADMIN, password=SA_PASSWORD)


@pytest.fixture
def audit(caplog):
    caplog.set_level(logging.DEBUG, logger=routes.logger.name)
    return caplog


# --- who may call ---


def _requests(target: uuid.UUID) -> list[tuple[str, str, dict]]:
    return [
        ("POST", REWRITE, {"old_issuer": ISSUER, "new_issuer": NEW_ISSUER, "dry_run": False}),
        ("PUT", relink_path(target), {"issuer": ISSUER, "subject": REISSUED}),
    ]


@pytest.mark.asyncio
async def test_only_a_superadmin_effective_role_may_call_either_route(client):
    victim = await make_oidc("victim")
    user = await make_user("plain")
    admin = await make_user("adm", role=Role.ADMIN)
    sa = await make_user("sa", role=Role.SUPERADMIN)
    inactive_sa = await make_user("sa-off", role=Role.SUPERADMIN, is_active=False)
    before = await identities()
    callers = [
        ({}, 401, NOT_AUTHENTICATED),
        (bearer(user), 403, FORBIDDEN),
        (bearer(admin), 403, FORBIDDEN),
        # An API-token exchange clamped to admin on a superadmin row (IAM-ROLE-5).
        (bearer(sa, Role.ADMIN), 403, FORBIDDEN),
        (bearer(inactive_sa), 401, BAD_CREDENTIALS),
        # A user claim cannot be raised by the row, nor a row by a superadmin claim.
        (bearer(user, Role.SUPERADMIN), 403, FORBIDDEN),
    ]
    for method, path, body in _requests(victim.id):
        for headers, code, detail in callers:
            resp = await client.request(method, path, json=body, headers=headers)
            assert (resp.status_code, resp.json()) == (code, detail), (method, headers)
    assert await identities() == before
    # The same requests as the superadmin succeed.
    for method, path, body in _requests(victim.id):
        resp = await client.request(method, path, json=body, headers=bearer(sa))
        assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_the_role_gate_runs_before_the_body_is_validated_and_before_any_lookup(client):
    admin = await make_user("adm", role=Role.ADMIN)
    for method, path in (("POST", REWRITE), ("PUT", relink_path(uuid.uuid4()))):
        for body in ({"bogus": 1}, {}, None):
            resp = await client.request(method, path, json=body, headers=bearer(admin))
            assert (resp.status_code, resp.json()) == (403, FORBIDDEN)
    resp = await client.put("/users/not-a-uuid/federated-identity", json={}, headers=bearer(admin))
    assert (resp.status_code, resp.json()) == (403, FORBIDDEN)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["local", "ldap", "oidc"])
async def test_both_routes_answer_in_every_auth_mode(monkeypatch, client, superadmin, mode):
    monkeypatch.setattr(settings, "auth_method", mode, raising=False)
    victim = await make_oidc("victim")
    resp = await client.post(
        REWRITE,
        json={"old_issuer": ISSUER, "new_issuer": NEW_ISSUER, "dry_run": False},
        headers=bearer(superadmin),
    )
    assert resp.json() == {
        "dry_run": False,
        "matched": 1,
        "conflicts": 0,
        "rewritten": 1,
        **NO_COLLISIONS,
    }
    resp = await client.put(
        relink_path(victim.id),
        json={"issuer": ISSUER, "subject": REISSUED},
        headers=bearer(superadmin),
    )
    assert resp.status_code == 200, resp.text
    assert await identities() == {victim.id: (ISSUER, REISSUED)}


@pytest.mark.asyncio
async def test_a_break_glass_local_superadmin_can_sign_in_and_recover_in_oidc_mode(
    monkeypatch, client, superadmin
):
    """D5 and D10: with every SSO user locked out, the local superadmin signs in
    through the break-glass login and runs the rewrite."""
    monkeypatch.setattr(settings, "auth_method", "oidc", raising=False)
    monkeypatch.setattr(settings, "oidc_break_glass_local_superadmin", True)
    await make_oidc("victim")
    login = await client.post("/login", json={"email": superadmin.email, "password": SA_PASSWORD})
    assert login.status_code == 200, login.text
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    resp = await client.post(
        REWRITE,
        json={"old_issuer": ISSUER, "new_issuer": NEW_ISSUER, "dry_run": False},
        headers=headers,
    )
    assert resp.json()["rewritten"] == 1


@pytest.mark.asyncio
async def test_a_superadmin_api_token_minted_before_the_switch_can_recover_in_oidc_mode(
    monkeypatch, client, superadmin
):
    """With break-glass off, the other documented path: a superadmin API token,
    exchanged at `POST /tokens/exchange`, which ignores the mode (IAM-USER-9)."""
    async with TestSessionLocal() as db:
        _, raw = await create_api_token(
            db,
            name="recovery",
            principal=superadmin,
            role=Role.SUPERADMIN,
            expires_at=None,
            created_by=superadmin.id,
        )
    monkeypatch.setattr(settings, "auth_method", "oidc", raising=False)
    monkeypatch.setattr(settings, "oidc_break_glass_local_superadmin", False)
    refused = await client.post("/login", json={"email": superadmin.email, "password": SA_PASSWORD})
    assert refused.status_code == 401
    exchanged = await client.post("/tokens/exchange", json={"token": raw})
    assert exchanged.status_code == 200, exchanged.text
    headers = {"Authorization": f"Bearer {exchanged.json()['access_token']}"}
    await make_oidc("victim")
    resp = await client.post(
        REWRITE,
        json={"old_issuer": ISSUER, "new_issuer": NEW_ISSUER, "dry_run": True},
        headers=headers,
    )
    assert resp.json() == {
        "dry_run": True,
        "matched": 1,
        "conflicts": 0,
        "rewritten": 0,
        **NO_COLLISIONS,
    }


# --- the issuer rewrite ---


@pytest.mark.asyncio
async def test_rewrite_dry_run_then_write_answer_the_counts(client, superadmin, audit):
    a = await make_oidc("a")
    b = await make_oidc("b")
    other = await make_oidc("other", issuer="https://other.example.test")
    body = {"old_issuer": ISSUER, "new_issuer": NEW_ISSUER, "dry_run": True}
    resp = await client.post(REWRITE, json=body, headers=bearer(superadmin))
    assert (resp.status_code, resp.json()) == (
        200,
        {"dry_run": True, "matched": 2, "conflicts": 0, "rewritten": 0, **NO_COLLISIONS},
    )
    assert (await identities())[a.id][0] == ISSUER
    resp = await client.post(REWRITE, json={**body, "dry_run": False}, headers=bearer(superadmin))
    assert (resp.status_code, resp.json()) == (
        200,
        {"dry_run": False, "matched": 2, "conflicts": 0, "rewritten": 2, **NO_COLLISIONS},
    )
    after = await identities()
    assert (after[a.id][0], after[b.id][0], after[other.id][0]) == (
        NEW_ISSUER,
        NEW_ISSUER,
        "https://other.example.test",
    )
    outcomes = [(r.outcome, r.levelname) for r in audit_records(audit)]
    assert outcomes == [("dry_run", "INFO"), ("rewritten", "WARNING")]
    last = audit_records(audit)[-1]
    assert (last.action, last.caller_id, last.matched, last.conflicts, last.rewritten) == (
        "oidc_issuer_rewrite",
        str(superadmin.id),
        2,
        0,
        2,
    )
    assert_no_identity_text(audit, ISSUER, "sub-a", "sub-b", "other.example.test")


@pytest.mark.asyncio
async def test_rewrite_with_a_conflict_is_refused_409_with_the_count_and_changes_nothing(
    client, superadmin, audit
):
    a = await make_oidc("a")
    b = await make_oidc("b")
    held_a = await make_oidc("held-a", issuer=NEW_ISSUER, subject="sub-a")
    before = await identities()
    body = {"old_issuer": ISSUER, "new_issuer": NEW_ISSUER, "dry_run": False}
    resp = await client.post(REWRITE, json=body, headers=bearer(superadmin))
    assert resp.status_code == 409
    assert resp.json() == {
        "detail": "Rewrite refused: 1 federated identity would collide with an identity "
        "already under the new issuer; nothing was changed",
        "conflicts": 1,
        "collisions": [
            {"old_issuer_account_id": str(a.id), "new_issuer_account_id": str(held_a.id)}
        ],
        "collisions_truncated": False,
    }
    held_b = await make_oidc("held-b", issuer=NEW_ISSUER, subject="sub-b")
    resp = await client.post(REWRITE, json=body, headers=bearer(superadmin))
    pairs = sorted(
        [
            {"old_issuer_account_id": str(a.id), "new_issuer_account_id": str(held_a.id)},
            {"old_issuer_account_id": str(b.id), "new_issuer_account_id": str(held_b.id)},
        ],
        key=lambda p: p["old_issuer_account_id"],
    )
    assert resp.json() == {
        "detail": "Rewrite refused: 2 federated identities would collide with an identity "
        "already under the new issuer; nothing was changed",
        "conflicts": 2,
        "collisions": pairs,
        "collisions_truncated": False,
    }
    dry = await client.post(REWRITE, json={**body, "dry_run": True}, headers=bearer(superadmin))
    assert dry.json() == {
        "dry_run": True,
        "matched": 2,
        "conflicts": 2,
        "rewritten": 0,
        "collisions": pairs,
        "collisions_truncated": False,
    }
    after = await identities()
    assert {k: v for k, v in after.items() if k in before} == before
    refusals = [r for r in audit_records(audit) if r.outcome == "refused_conflict"]
    assert [(r.levelname, r.conflicts) for r in refusals] == [("WARNING", 1), ("WARNING", 2)]
    assert_no_identity_text(audit, ISSUER, "sub-a", "sub-b")


@pytest.mark.asyncio
async def test_rewrite_to_the_same_issuer_is_refused_400(client, superadmin, audit):
    await make_oidc("a")
    for dry_run in (True, False):
        resp = await client.post(
            REWRITE,
            json={"old_issuer": ISSUER, "new_issuer": ISSUER, "dry_run": dry_run},
            headers=bearer(superadmin),
        )
        assert (resp.status_code, resp.json()) == (
            400,
            {"detail": "The new issuer equals the old issuer"},
        )
    assert [r.outcome for r in audit_records(audit)] == ["refused_unchanged"] * 2
    assert (await counts()) == (2, 1)


REWRITE_422_BODIES = {
    "unknown key": (
        {"old_issuer": ISSUER, "new_issuer": NEW_ISSUER, "dry_run": True, "force": True},
        ["body", "force"],
        "extra_forbidden",
    ),
    "dry_run missing": (
        {"old_issuer": ISSUER, "new_issuer": NEW_ISSUER},
        ["body", "dry_run"],
        "missing",
    ),
    "dry_run a string": (
        {"old_issuer": ISSUER, "new_issuer": NEW_ISSUER, "dry_run": "false"},
        ["body", "dry_run"],
        "bool_type",
    ),
    "dry_run a number": (
        {"old_issuer": ISSUER, "new_issuer": NEW_ISSUER, "dry_run": 0},
        ["body", "dry_run"],
        "bool_type",
    ),
    "blank old issuer": (
        {"old_issuer": "  ", "new_issuer": NEW_ISSUER, "dry_run": True},
        ["body", "old_issuer"],
        "identity_key_invalid",
    ),
    "new issuer with a newline": (
        {"old_issuer": ISSUER, "new_issuer": NEW_ISSUER + "\n", "dry_run": True},
        ["body", "new_issuer"],
        "identity_key_invalid",
    ),
    "new issuer too long": (
        {"old_issuer": ISSUER, "new_issuer": "h" * 256, "dry_run": True},
        ["body", "new_issuer"],
        "identity_key_invalid",
    ),
    "issuer not a string": (
        {"old_issuer": 5, "new_issuer": NEW_ISSUER, "dry_run": True},
        ["body", "old_issuer"],
        "string_type",
    ),
}

IDENTITY_KEY_MSG = "must be a non-blank string of at most 255 characters with no control character"


@pytest.mark.asyncio
@pytest.mark.parametrize("case", sorted(REWRITE_422_BODIES))
async def test_rewrite_body_refusals_are_422_with_the_auth_shape(client, superadmin, case):
    await make_oidc("a")
    body, loc, error_type = REWRITE_422_BODIES[case]
    resp = await client.post(REWRITE, json=body, headers=bearer(superadmin))
    assert resp.status_code == 422
    errors = resp.json()["detail"]
    assert [sorted(e) for e in errors] == [["loc", "msg", "type"]] * len(errors)
    assert [(e["loc"], e["type"]) for e in errors] == [(loc, error_type)]
    if error_type == "identity_key_invalid":
        assert errors[0]["msg"] == IDENTITY_KEY_MSG
    assert (await identities()) and next(iter((await identities()).values()))[0] == ISSUER


# --- the relink ---


@pytest.mark.asyncio
async def test_relink_answers_the_new_identity_and_keeps_the_account(client, superadmin, audit):
    victim = await make_oidc("victim")
    bystander = await make_oidc("bystander")
    resp = await client.put(
        relink_path(victim.id),
        json={"issuer": ISSUER, "subject": REISSUED},
        headers=bearer(superadmin),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert sorted(body) == ["created_at", "issuer", "last_login_at", "subject", "user_id"]
    assert (body["user_id"], body["issuer"], body["subject"], body["last_login_at"]) == (
        str(victim.id),
        ISSUER,
        REISSUED,
        None,
    )
    assert datetime.fromisoformat(body["created_at"]).year >= 2026
    after = await identities()
    assert after == {victim.id: (ISSUER, REISSUED), bystander.id: (ISSUER, "sub-bystander")}
    async with TestSessionLocal() as db:
        row = await db.get(User, victim.id)
    assert (row.email, row.username, row.auth_source, row.role) == (
        victim.email,
        victim.username,
        "oidc",
        Role.USER,
    )
    # Again with the same pair: 200, nothing written, logged at INFO.
    again = await client.put(
        relink_path(victim.id),
        json={"issuer": ISSUER, "subject": REISSUED},
        headers=bearer(superadmin),
    )
    assert again.json() == body
    records = audit_records(audit)
    assert [(r.outcome, r.levelname) for r in records] == [
        ("relinked", "WARNING"),
        ("unchanged", "INFO"),
    ]
    assert (
        records[0].action,
        records[0].caller_id,
        records[0].user_id,
        records[0].issuer_changed,
        records[0].subject_changed,
    ) == ("oidc_identity_relink", str(superadmin.id), str(victim.id), False, True)
    assert_no_identity_text(audit, ISSUER, REISSUED, "sub-victim", victim.email)


@pytest.mark.asyncio
async def test_relink_refusals(client, superadmin, audit):
    victim = await make_oidc("victim")
    holder = await make_oidc("holder")
    local = await make_user("local-one")
    ldap = await make_user("ldap-one", source="ldap")
    before = await identities()
    cases = [
        (uuid.uuid4(), "sub-new", 404, "User not found", "refused_user_missing"),
        (local.id, "sub-new", 409, routes.NOT_OIDC_DETAIL, "refused_not_oidc"),
        (ldap.id, "sub-new", 409, routes.NOT_OIDC_DETAIL, "refused_not_oidc"),
        (superadmin.id, "sub-new", 409, routes.NOT_OIDC_DETAIL, "refused_not_oidc"),
        (victim.id, "sub-holder", 409, routes.IDENTITY_TAKEN_DETAIL, "refused_identity_taken"),
    ]
    for target, subject, code, detail, outcome in cases:
        resp = await client.put(
            relink_path(target),
            json={"issuer": ISSUER, "subject": subject},
            headers=bearer(superadmin),
        )
        assert (resp.status_code, resp.json()) == (code, {"detail": detail})
    assert routes.NOT_OIDC_DETAIL == "Only a single sign-on account can be linked to an identity"
    assert routes.IDENTITY_TAKEN_DETAIL == "This identity is already linked to another account"
    assert await identities() == before
    assert holder.id in before
    assert [(r.outcome, r.levelname) for r in audit_records(audit)] == [
        (outcome, "WARNING") for *_, outcome in cases
    ]
    assert_no_identity_text(audit, ISSUER, "sub-new", "sub-holder")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "loc", "error_type"),
    [
        ({"issuer": ISSUER}, ["body"], "relink_target"),
        (
            {"issuer": ISSUER, "subject": "s", "user_id": "x"},
            ["body", "user_id"],
            "extra_forbidden",
        ),
        ({"issuer": ISSUER, "subject": ""}, ["body", "subject"], "identity_key_invalid"),
        ({"issuer": ISSUER, "subject": "a\tb"}, ["body", "subject"], "identity_key_invalid"),
        ({"issuer": ISSUER, "subject": "s" * 256}, ["body", "subject"], "identity_key_invalid"),
        ({"issuer": "", "subject": "s"}, ["body", "issuer"], "identity_key_invalid"),
    ],
)
async def test_relink_body_refusals_are_422(client, superadmin, body, loc, error_type):
    victim = await make_oidc("victim")
    resp = await client.put(relink_path(victim.id), json=body, headers=bearer(superadmin))
    assert resp.status_code == 422
    assert [(e["loc"], e["type"]) for e in resp.json()["detail"]] == [(loc, error_type)]
    assert await identities() == {victim.id: (ISSUER, "sub-victim")}


@pytest.mark.asyncio
async def test_relink_accepts_a_subject_at_the_column_width_with_inner_spaces(client, superadmin):
    """The routes take what a login takes (IAM-OIDC-32), no stricter: inner and
    edge spaces and 255 characters pass, stored exactly as sent."""
    victim = await make_oidc("victim")
    subject = " " + "s" * 253 + " "
    resp = await client.put(
        relink_path(victim.id),
        json={"issuer": ISSUER, "subject": subject},
        headers=bearer(superadmin),
    )
    assert resp.status_code == 200
    assert (await identities())[victim.id] == (ISSUER, subject)


@pytest.mark.asyncio
async def test_a_database_error_answers_a_fixed_500_logged_by_class_name_only(
    monkeypatch, client, superadmin, audit
):
    victim = await make_oidc("victim")

    async def broken(*args, **kwargs):
        raise OperationalError("UPDATE ... " + REISSUED, {"subject": REISSUED}, Exception(REISSUED))

    monkeypatch.setattr(routes, "rewrite_issuer", broken)
    monkeypatch.setattr(routes, "relink_identity", broken)
    for method, path, body in _requests(victim.id):
        resp = await client.request(method, path, json=body, headers=bearer(superadmin))
        assert (resp.status_code, resp.json()) == (
            500,
            {"detail": "Federated identity change failed"},
        )
    errors = [r for r in audit_records(audit) if r.outcome == "error"]
    assert [(r.levelname, r.exception_class) for r in errors] == [
        ("ERROR", "OperationalError"),
        ("ERROR", "OperationalError"),
    ]
    assert all(r.exc_info is None for r in errors)
    assert_no_identity_text(audit, REISSUED, ISSUER)


# --- the recovery stories through the single sign-on routes ---


@pytest.fixture(scope="module")
def rsa_keys() -> dict:
    return {
        "rsa": rsa.generate_private_key(public_exponent=65537, key_size=2048),
        "attacker": rsa.generate_private_key(public_exponent=65537, key_size=2048),
    }


@pytest.fixture
def idp(monkeypatch, rsa_keys):
    """OIDC mode against the fake provider, as `test_oidc_routes.py` sets it up."""
    for name, value in {
        "auth_method": "oidc",
        "oidc_issuer": ISSUER,
        "oidc_client_id": CLIENT_ID,
        "oidc_client_secret": CLIENT_SECRET,
        "oidc_redirect_uri": REDIRECT_URI,
        "oidc_scopes": "openid email profile",
        "oidc_id_token_algorithm": "RS256",
        "oidc_username_claim": "preferred_username",
        "oidc_email_claim": "email",
        "oidc_require_https": True,
        "oidc_ca_cert": "",
        "oidc_provider_name": "Example IdP",
        "oidc_break_glass_local_superadmin": False,
    }.items():
        monkeypatch.setattr(settings, name, value, raising=False)
    fake = FakeIdP(rsa_keys)
    monkeypatch.setattr(oidc_service, "transport", httpx.MockTransport(fake.handler))
    oidc_service.reset_caches()
    yield fake
    oidc_service.reset_caches()


async def signed_in_account(client, idp: FakeIdP) -> str:
    """A full sign-in through the phase 3 routes; the signed-in account's id."""
    _, _, handoff = await sign_in(client, idp)
    resp = await exchange(client, handoff)
    assert resp.status_code == 200, resp.text
    me = await client.get("/me", headers={"Authorization": f"Bearer {resp.json()['access_token']}"})
    assert me.status_code == 200, me.text
    return me.json()["id"]


async def refused_sign_in(client, idp: FakeIdP) -> None:
    before = await counts()
    response = await complete_callback(client, idp, await begin(client, idp))
    assert response.headers["location"] == FAILED
    assert await counts() == before


def change_issuer(monkeypatch, idp: FakeIdP, issuer: str) -> None:
    """The operator sets OIDC_ISSUER to what the provider now publishes and puts in `iss`."""
    monkeypatch.setattr(settings, "oidc_issuer", issuer, raising=False)
    idp.discovery["issuer"] = issuer
    idp.claim_changes["iss"] = issuer
    oidc_service.reset_caches()


@pytest.mark.asyncio
async def test_story_an_issuer_change_is_refused_then_recovered_by_the_rewrite(
    monkeypatch, client, superadmin, idp
):
    account = await signed_in_account(client, idp)
    change_issuer(monkeypatch, idp, NEW_ISSUER)
    # Every returning user is now an unknown (issuer, subject) whose email their
    # own account holds: refused (IAM-OIDC-10), nothing created.
    await refused_sign_in(client, idp)

    body = {"old_issuer": ISSUER, "new_issuer": NEW_ISSUER, "dry_run": True}
    dry = await client.post(REWRITE, json=body, headers=bearer(superadmin))
    assert dry.json() == {
        "dry_run": True,
        "matched": 1,
        "conflicts": 0,
        "rewritten": 0,
        **NO_COLLISIONS,
    }
    await refused_sign_in(client, idp)
    done = await client.post(REWRITE, json={**body, "dry_run": False}, headers=bearer(superadmin))
    assert done.json()["rewritten"] == 1

    assert await signed_in_account(client, idp) == account
    assert await identities() == {uuid.UUID(account): (NEW_ISSUER, SUBJECT)}


@pytest.mark.asyncio
async def test_story_a_reissued_subject_is_refused_then_recovered_by_the_relink(
    client, superadmin, idp
):
    account = await signed_in_account(client, idp)
    idp.claim_changes["sub"] = REISSUED
    await refused_sign_in(client, idp)

    resp = await client.put(
        relink_path(account),
        json={"issuer": ISSUER, "subject": REISSUED},
        headers=bearer(superadmin),
    )
    assert resp.status_code == 200, resp.text
    assert await signed_in_account(client, idp) == account

    # The old subject is no longer linked: its sign-in is a first login whose
    # email the account holds, so it is refused and links nothing.
    idp.claim_changes["sub"] = SUBJECT
    await refused_sign_in(client, idp)
    assert await identities() == {uuid.UUID(account): (ISSUER, REISSUED)}


@pytest.mark.asyncio
async def test_story_a_provider_account_with_the_victims_email_never_gets_the_account(
    client, superadmin, idp
):
    """D4's threat: someone who holds the victim's email at the provider, under
    another subject, is refused before the relink and after it, because the
    relink names the victim's own new subject, not an email."""
    account = await signed_in_account(client, idp)
    idp.claim_changes["sub"] = "SENTINEL-attacker"
    await refused_sign_in(client, idp)
    await client.put(
        relink_path(account),
        json={"issuer": ISSUER, "subject": REISSUED},
        headers=bearer(superadmin),
    )
    await refused_sign_in(client, idp)
    assert idp.claims()["email"] == EMAIL
    assert await identities() == {uuid.UUID(account): (ISSUER, REISSUED)}


@pytest.mark.asyncio
async def test_story_the_rewrite_is_refused_when_a_returning_user_was_provisioned_twice(
    monkeypatch, client, superadmin, idp
):
    """The hazard the runbook's order avoids: after the issuer change, a person
    whose provider email changed signs in before the rewrite and gets a second
    account under the new issuer; the rewrite then refuses, all or nothing."""
    account = await signed_in_account(client, idp)
    change_issuer(monkeypatch, idp, NEW_ISSUER)
    idp.claim_changes.update(email="changed@example.test", preferred_username="changed")
    stray = await signed_in_account(client, idp)
    assert stray != account
    resp = await client.post(
        REWRITE,
        json={"old_issuer": ISSUER, "new_issuer": NEW_ISSUER, "dry_run": False},
        headers=bearer(superadmin),
    )
    assert resp.status_code == 409
    assert (await identities())[uuid.UUID(account)] == (ISSUER, SUBJECT)
    # The documented way out (docs/OPERATIONS.md, "If the rewrite refuses"):
    # relink the stray account to the reserved issuer urn:herd:retired with its
    # own id as the subject. Auth never accepts that string as OIDC_ISSUER, so no
    # token can carry it, and the person's pair under the new issuer is freed.
    assert not _absolute_url_ok("urn:herd:retired", False)
    retired = await client.put(
        relink_path(stray),
        json={"issuer": "urn:herd:retired", "subject": stray},
        headers=bearer(superadmin),
    )
    assert retired.status_code == 200
    assert (await identities())[uuid.UUID(stray)] == ("urn:herd:retired", stray)
    resp = await client.post(
        REWRITE,
        json={"old_issuer": ISSUER, "new_issuer": NEW_ISSUER, "dry_run": False},
        headers=bearer(superadmin),
    )
    assert resp.json()["rewritten"] == 1
    assert await signed_in_account(client, idp) == account


def test_the_route_constants_are_the_specified_wording():
    assert routes.ISSUER_UNCHANGED_DETAIL == "The new issuer equals the old issuer"
    assert routes.USER_NOT_FOUND_DETAIL == "User not found"
    assert routes.STORE_FAILED_DETAIL == "Federated identity change failed"
    assert routes.rewrite_conflict_detail(3) == (
        "Rewrite refused: 3 federated identities would collide with an identity already "
        "under the new issuer; nothing was changed"
    )
    assert (routes.REWRITE_ACTION, routes.RELINK_ACTION) == (
        "oidc_issuer_rewrite",
        "oidc_identity_relink",
    )


# --- relink candidates (owner decision A, 2026-11-18) ---

ATTACKER = "SENTINEL-subject-attacker"


def candidates_path(user_id) -> str:
    return f"/users/{user_id}/federated-identity/candidates"


async def candidate_rows() -> list[tuple]:
    async with TestSessionLocal() as db:
        rows = (await db.execute(select(FederatedRelinkCandidate))).scalars().all()
        return sorted((r.user_id, r.issuer, r.subject, r.attempts) for r in rows)


async def captured_total(user_id) -> int | None:
    async with TestSessionLocal() as db:
        return (
            await db.execute(
                select(FederatedRelinkCaptureTotal.captured_total).where(
                    FederatedRelinkCaptureTotal.user_id == uuid.UUID(str(user_id))
                )
            )
        ).scalar_one_or_none()


async def read_candidates(client, superadmin, user_id) -> dict:
    resp = await client.get(candidates_path(user_id), headers=bearer(superadmin))
    assert resp.status_code == 200, resp.text
    return resp.json()


def login_as(email: str, subject: str, *, username: str = "someone") -> FederatedLogin:
    return FederatedLogin(
        issuer=ISSUER,
        subject=subject,
        email=email,
        email_verified=True,
        username=username,
        full_name=None,
    )


async def refuse(login: FederatedLogin) -> None:
    async with TestSessionLocal() as db:
        assert await resolve_federated_user(db, login) is None
        assert not db.in_transaction()


_CLOCK_BASE = datetime.now(timezone.utc)


def shift_capture_clock(monkeypatch, seconds: float, base: datetime = _CLOCK_BASE) -> None:
    monkeypatch.setattr(
        federated_identity_service, "_utcnow", lambda: base + timedelta(seconds=seconds)
    )


@pytest.mark.asyncio
async def test_a_refusal_on_an_oidc_accounts_email_captures_a_candidate(
    client, superadmin, idp, caplog
):
    caplog.set_level(logging.INFO)
    account = await signed_in_account(client, idp)
    idp.claim_changes["sub"] = REISSUED
    await refused_sign_in(client, idp)
    listing = await read_candidates(client, superadmin, account)
    assert listing["user_id"] == account and listing["captured_total"] == 1
    [candidate] = listing["candidates"]
    assert sorted(candidate) == [
        "attempts",
        "created_at",
        "expires_at",
        "fingerprint",
        "id",
        "issuer",
        "last_seen_at",
    ]
    assert candidate["issuer"] == ISSUER and candidate["attempts"] == 1
    assert candidate["fingerprint"] == identity_fingerprint(ISSUER, REISSUED)
    created = datetime.fromisoformat(candidate["created_at"])
    expires = datetime.fromisoformat(candidate["expires_at"])
    assert expires - created == timedelta(hours=24)
    assert REISSUED not in json.dumps(listing)
    # The refused sign-in still answered exactly the generic failure, and the
    # capture's own log line names the account and the candidate only.
    captured = [
        r for r in caplog.records if getattr(r, "action", None) == "oidc_relink_candidate_captured"
    ]
    assert [(r.user_id, r.candidate_id, r.new) for r in captured] == [
        (account, candidate["id"], True)
    ]
    formatter = JSONFormatter("auth")
    for record in caplog.records:
        if record.name in ("aiosqlite",) or (
            record.name == "httpx" and " http://test/" in record.getMessage()
        ):
            continue
        assert REISSUED not in formatter.format(record)


@pytest.mark.asyncio
@pytest.mark.parametrize("holder", ["local", "ldap", "local-superadmin", "oidc-superadmin"])
async def test_no_candidate_when_the_email_holder_is_not_an_ordinary_oidc_account(holder):
    source = "oidc" if holder.startswith("oidc") else holder.split("-")[0]
    role = Role.SUPERADMIN if holder.endswith("superadmin") else Role.USER
    user = await make_user(f"h-{holder}", source=source, role=role)
    await refuse(login_as(user.email, "sub-x", username="unique-x"))
    assert await candidate_rows() == []
    assert await captured_total(user.id) is None


@pytest.mark.asyncio
async def test_no_candidate_for_a_username_only_collision_or_any_other_refusal():
    holder = await make_oidc("holder")
    # Username taken, email free: refused as username_taken, no candidate.
    await refuse(login_as("free@example.test", "sub-x", username=holder.username))
    # Email unverified: refused before the collision check, no candidate.
    async with TestSessionLocal() as db:
        login = login_as(holder.email, "sub-y")
        assert (
            await resolve_federated_user(
                db,
                FederatedLogin(**{**login.__dict__, "email_verified": False}),
            )
            is None
        )
    assert await candidate_rows() == []


@pytest.mark.asyncio
async def test_no_candidate_when_the_login_succeeds(client, superadmin, idp):
    account = await signed_in_account(client, idp)
    assert await signed_in_account(client, idp) == account
    assert await candidate_rows() == []
    listing = await read_candidates(client, superadmin, account)
    assert listing == {"user_id": account, "captured_total": 0, "candidates": []}


@pytest.mark.asyncio
async def test_a_repeated_pair_is_one_candidate_with_more_attempts(monkeypatch, client, superadmin):
    holder = await make_oidc("holder")
    shift_capture_clock(monkeypatch, 0)
    await refuse(login_as(holder.email, ATTACKER))
    shift_capture_clock(monkeypatch, 60)
    await refuse(login_as(holder.email, ATTACKER))
    assert await candidate_rows() == [(holder.id, ISSUER, ATTACKER, 2)]
    [candidate] = (await read_candidates(client, superadmin, holder.id))["candidates"]
    assert candidate["attempts"] == 2
    assert datetime.fromisoformat(candidate["last_seen_at"]) - datetime.fromisoformat(
        candidate["created_at"]
    ) == timedelta(seconds=60)
    assert await captured_total(holder.id) == 1


@pytest.mark.asyncio
async def test_a_candidate_expires_after_24_hours(monkeypatch, client, superadmin):
    holder = await make_oidc("holder")
    shift_capture_clock(monkeypatch, 0)
    await refuse(login_as(holder.email, ATTACKER))
    candidate_id = (await read_candidates(client, superadmin, holder.id))["candidates"][0]["id"]
    shift_capture_clock(monkeypatch, 24 * 3600 - 1)
    assert len((await read_candidates(client, superadmin, holder.id))["candidates"]) == 1
    shift_capture_clock(monkeypatch, 24 * 3600)
    listing = await read_candidates(client, superadmin, holder.id)
    assert listing["candidates"] == [] and listing["captured_total"] == 1
    resp = await client.put(
        relink_path(holder.id), json={"candidate_id": candidate_id}, headers=bearer(superadmin)
    )
    assert (resp.status_code, resp.json()) == (
        404,
        {"detail": "Relink candidate not found for this account"},
    )
    # The next capture prunes it.
    await refuse(login_as(holder.email, "sub-later"))
    assert await candidate_rows() == [(holder.id, ISSUER, "sub-later", 1)]


@pytest.mark.asyncio
async def test_the_per_account_cap_keeps_the_newest_five_and_the_total_keeps_counting(
    monkeypatch, client, superadmin
):
    holder = await make_oidc("holder")
    other = await make_oidc("other")
    await refuse(login_as(other.email, "x-other"))
    for i in range(7):
        shift_capture_clock(monkeypatch, i)
        await refuse(login_as(holder.email, f"flood-{i}"))
    listing = await read_candidates(client, superadmin, holder.id)
    assert listing["captured_total"] == 7
    assert [c["fingerprint"] for c in listing["candidates"]] == [
        identity_fingerprint(ISSUER, f"flood-{i}") for i in (6, 5, 4, 3, 2)
    ]
    # Another account's candidate is untouched by this account's cap.
    assert (other.id, ISSUER, "x-other", 1) in await candidate_rows()


@pytest.mark.asyncio
async def test_the_global_cap_drops_the_oldest_candidates_in_the_table(monkeypatch):
    monkeypatch.setattr(federated_identity_service, "CANDIDATES_GLOBAL_CAP", 3)
    a = await make_oidc("a")
    b = await make_oidc("b")
    for i, (holder, subject) in enumerate(
        [(a, "a-1"), (b, "b-1"), (a, "a-2"), (b, "b-2"), (a, "a-3")]
    ):
        shift_capture_clock(monkeypatch, i)
        await refuse(login_as(holder.email, subject))
    assert sorted(r[2] for r in await candidate_rows()) == ["a-2", "a-3", "b-2"]
    assert (await captured_total(a.id), await captured_total(b.id)) == (3, 2)


@pytest.mark.asyncio
async def test_a_capture_failure_never_changes_the_refusal(monkeypatch, client, idp, caplog):
    caplog.set_level(logging.INFO)
    account = await signed_in_account(client, idp)

    async def broken(db, **kwargs):
        raise OperationalError("INSERT ... " + REISSUED, {"subject": REISSUED}, Exception(REISSUED))

    monkeypatch.setattr(auth_service, "capture_relink_candidate", broken)
    idp.claim_changes["sub"] = REISSUED
    await refused_sign_in(client, idp)
    failures = [
        r
        for r in caplog.records
        if getattr(r, "action", None) == "oidc_relink_candidate_capture_failed"
    ]
    assert [(r.levelname, r.exception_class) for r in failures] == [("WARNING", "OperationalError")]
    assert all(r.exc_info is None and REISSUED not in r.getMessage() for r in failures)
    refusals = [r.reason for r in caplog.records if getattr(r, "action", None) == "login_failure"]
    assert refusals == ["email_taken"]
    assert await candidate_rows() == []
    assert account


@pytest.mark.asyncio
async def test_only_a_superadmin_may_read_candidates_and_the_gate_runs_before_the_lookup(client):
    holder = await make_oidc("holder")
    user = await make_user("plain")
    admin = await make_user("adm", role=Role.ADMIN)
    sa = await make_user("sa", role=Role.SUPERADMIN)
    for target in (holder.id, uuid.uuid4()):
        for headers, code, detail in (
            ({}, 401, NOT_AUTHENTICATED),
            (bearer(user), 403, FORBIDDEN),
            (bearer(admin), 403, FORBIDDEN),
            (bearer(sa, Role.ADMIN), 403, FORBIDDEN),
        ):
            resp = await client.get(candidates_path(target), headers=headers)
            assert (resp.status_code, resp.json()) == (code, detail)
    resp = await client.get(candidates_path(uuid.uuid4()), headers=bearer(sa))
    assert (resp.status_code, resp.json()) == (404, {"detail": "User not found"})
    resp = await client.get(candidates_path(user.id), headers=bearer(sa))
    assert (resp.status_code, resp.json()) == (409, {"detail": routes.NOT_OIDC_DETAIL})


@pytest.mark.asyncio
async def test_story_relink_by_candidate_then_the_next_login_succeeds(
    client, superadmin, idp, caplog
):
    caplog.set_level(logging.INFO)
    account = await signed_in_account(client, idp)
    idp.claim_changes["sub"] = REISSUED
    await refused_sign_in(client, idp)
    [candidate] = (await read_candidates(client, superadmin, account))["candidates"]
    resp = await client.put(
        relink_path(account), json={"candidate_id": candidate["id"]}, headers=bearer(superadmin)
    )
    assert resp.status_code == 200, resp.text
    assert (resp.json()["issuer"], resp.json()["subject"]) == (ISSUER, REISSUED)
    # Consumed: no live candidate is left, and the total keeps its count.
    listing = await read_candidates(client, superadmin, account)
    assert (listing["candidates"], listing["captured_total"]) == ([], 1)
    assert await signed_in_account(client, idp) == account
    [line] = [r for r in audit_records(caplog) if r.outcome == "relinked"]
    assert (line.via, line.candidate_id, line.candidates_discarded) == (
        "candidate",
        candidate["id"],
        1,
    )
    assert line.new_fingerprint == candidate["fingerprint"]
    assert line.old_fingerprint == identity_fingerprint(ISSUER, SUBJECT)


@pytest.mark.asyncio
async def test_story_a_planted_candidate_never_links_without_the_superadmin(
    client, superadmin, idp
):
    """Anyone who signs in at the provider with an account carrying the
    victim's email plants a candidate. Two live candidates; nothing links; the
    superadmin, having confirmed with the person which attempt was theirs,
    relinks by that candidate; the attacker stays refused."""
    account = await signed_in_account(client, idp)
    idp.claim_changes["sub"] = ATTACKER
    await refused_sign_in(client, idp)
    idp.claim_changes["sub"] = REISSUED
    await refused_sign_in(client, idp)
    listing = await read_candidates(client, superadmin, account)
    assert (len(listing["candidates"]), listing["captured_total"]) == (2, 2)
    # Nothing linked on its own: both still refused, the identity unchanged.
    await refused_sign_in(client, idp)
    idp.claim_changes["sub"] = ATTACKER
    await refused_sign_in(client, idp)
    assert await identities() == {uuid.UUID(account): (ISSUER, SUBJECT)}
    person = next(
        c
        for c in listing["candidates"]
        if c["fingerprint"] == identity_fingerprint(ISSUER, REISSUED)
    )
    resp = await client.put(
        relink_path(account), json={"candidate_id": person["id"]}, headers=bearer(superadmin)
    )
    assert resp.status_code == 200
    await refused_sign_in(client, idp)  # the attacker
    idp.claim_changes["sub"] = REISSUED
    assert await signed_in_account(client, idp) == account
    # The attacker's refused attempt after the relink is a fresh candidate.
    assert [
        c["fingerprint"] for c in (await read_candidates(client, superadmin, account))["candidates"]
    ] == [identity_fingerprint(ISSUER, ATTACKER)]


@pytest.mark.asyncio
async def test_a_candidate_of_another_account_is_refused_and_changes_nothing(
    monkeypatch, client, superadmin
):
    victim = await make_oidc("victim")
    other = await make_oidc("other")
    await refuse(login_as(other.email, "sub-for-other"))
    [foreign] = (await read_candidates(client, superadmin, other.id))["candidates"]
    before = (await identities(), await candidate_rows())
    for target in (foreign["id"], str(uuid.uuid4())):
        resp = await client.put(
            relink_path(victim.id), json={"candidate_id": target}, headers=bearer(superadmin)
        )
        assert (resp.status_code, resp.json()) == (
            404,
            {"detail": "Relink candidate not found for this account"},
        )
    assert (await identities(), await candidate_rows()) == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {},
        {"issuer": ISSUER},
        {"subject": "s"},
        {"issuer": ISSUER, "subject": "s", "candidate_id": str(uuid.UUID(int=1))},
        {"subject": "s", "candidate_id": str(uuid.UUID(int=1))},
        {"issuer": None, "subject": None, "candidate_id": None},
    ],
)
async def test_the_relink_takes_exactly_one_of_the_pair_and_a_candidate_id(
    client, superadmin, body
):
    victim = await make_oidc("victim")
    resp = await client.put(relink_path(victim.id), json=body, headers=bearer(superadmin))
    assert resp.status_code == 422
    assert resp.json()["detail"] == [
        {
            "type": "relink_target",
            "loc": ["body"],
            "msg": "send either issuer and subject, or candidate_id, and not both",
        }
    ]


@pytest.mark.asyncio
async def test_a_typed_relink_also_discards_the_accounts_candidates(client, superadmin):
    holder = await make_oidc("holder")
    await refuse(login_as(holder.email, ATTACKER))
    resp = await client.put(
        relink_path(holder.id),
        json={"issuer": ISSUER, "subject": REISSUED},
        headers=bearer(superadmin),
    )
    assert resp.status_code == 200
    assert await candidate_rows() == []


# --- the colliding account pairs (owner decision B) ---


@pytest.mark.asyncio
async def test_the_collision_list_is_capped_with_a_flag_and_the_total(
    monkeypatch, client, superadmin
):
    monkeypatch.setattr(federated_identity_service, "COLLISIONS_LIST_CAP", 2)
    for name in ("a", "b", "c"):
        await make_oidc(name)
        await make_oidc(f"held-{name}", issuer=NEW_ISSUER, subject=f"sub-{name}")
    body = {"old_issuer": ISSUER, "new_issuer": NEW_ISSUER, "dry_run": True}
    dry = (await client.post(REWRITE, json=body, headers=bearer(superadmin))).json()
    assert (dry["conflicts"], len(dry["collisions"]), dry["collisions_truncated"]) == (3, 2, True)
    resp = await client.post(REWRITE, json={**body, "dry_run": False}, headers=bearer(superadmin))
    refused = resp.json()
    assert (resp.status_code, refused["conflicts"], refused["collisions_truncated"]) == (
        409,
        3,
        True,
    )
    assert refused["collisions"] == dry["collisions"]
    assert all(
        sorted(p) == ["new_issuer_account_id", "old_issuer_account_id"]
        for p in refused["collisions"]
    )


# --- keyed fingerprints in the audit (owner decision C) ---


def test_the_fingerprint_is_a_truncated_hmac_under_a_key_derived_from_the_jwt_secret(monkeypatch):
    monkeypatch.setattr(settings, "secret_key", "k" * 64)
    key = hmac.new(b"k" * 64, b"herd-auth/oidc-identity-fingerprint/v1", hashlib.sha256).digest()
    expected = hmac.new(key, json.dumps([ISSUER, "s"]).encode(), hashlib.sha256).hexdigest()[:16]
    assert identity_fingerprint(ISSUER, "s") == expected
    assert len(expected) == 16
    raw = hmac.new(b"k" * 64, json.dumps([ISSUER, "s"]).encode(), hashlib.sha256).hexdigest()[:16]
    assert expected != raw
    assert identity_fingerprint(ISSUER) != identity_fingerprint(ISSUER, "")
    monkeypatch.setattr(settings, "secret_key", "j" * 64)
    assert identity_fingerprint(ISSUER, "s") != expected


@pytest.mark.asyncio
async def test_audit_lines_carry_fingerprints_and_never_the_pairs(client, superadmin, audit):
    victim = await make_oidc("victim")
    await client.put(
        relink_path(victim.id),
        json={"issuer": ISSUER, "subject": REISSUED},
        headers=bearer(superadmin),
    )
    await client.post(
        REWRITE,
        json={"old_issuer": ISSUER, "new_issuer": NEW_ISSUER, "dry_run": True},
        headers=bearer(superadmin),
    )
    relink, rewrite = audit_records(audit)
    assert (relink.old_fingerprint, relink.new_fingerprint, relink.via) == (
        identity_fingerprint(ISSUER, "sub-victim"),
        identity_fingerprint(ISSUER, REISSUED),
        "pair",
    )
    assert (rewrite.old_issuer_fingerprint, rewrite.new_issuer_fingerprint) == (
        identity_fingerprint(ISSUER),
        identity_fingerprint(NEW_ISSUER),
    )
    assert_no_identity_text(audit, ISSUER, REISSUED, "sub-victim")


# --- a sign-in in flight when a relink commits (review finding 5) ---


@pytest.mark.asyncio
async def test_a_login_in_flight_when_a_relink_commits_is_refused(monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    victim = await make_oidc("victim")
    real_get = auth_service.get_user_by_id

    async def relink_lands_between_read_and_write(db, user_id):
        async with TestSessionLocal() as other:
            await federated_identity_service.relink_identity(
                other, victim.id, issuer=ISSUER, subject=REISSUED
            )
        return await real_get(db, user_id)

    monkeypatch.setattr(auth_service, "get_user_by_id", relink_lands_between_read_and_write)
    async with TestSessionLocal() as db:
        assert await resolve_federated_user(db, login_as(victim.email, "sub-victim")) is None
        assert not db.in_transaction()
    reasons = [r.reason for r in caplog.records if getattr(r, "action", None) == "login_failure"]
    assert reasons == ["identity_changed"]
    assert await identities() == {victim.id: (ISSUER, REISSUED)}


@pytest.mark.asyncio
async def test_reactivating_an_account_restores_its_refresh_tokens(client, superadmin, idp):
    """What the runbook says about sessions: neither a relink nor a deactivation
    revokes a refresh token; a deactivated account cannot refresh, and after a
    reactivation the old refresh token works again."""
    _, _, handoff = await sign_in(client, idp)
    tokens = (await exchange(client, handoff)).json()
    me = await client.get("/me", headers={"Authorization": f"Bearer {tokens['access_token']}"})
    account = me.json()["id"]
    await client.put(
        relink_path(account),
        json={"issuer": ISSUER, "subject": REISSUED},
        headers=bearer(superadmin),
    )
    await client.post(f"/users/{account}/deactivate", headers=bearer(superadmin))
    refused = await client.post("/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert refused.status_code == 401
    await client.post(f"/users/{account}/activate", headers=bearer(superadmin))
    again = await client.post("/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert again.status_code == 200


# --- docs truth (review finding 7) ---


@pytest.mark.asyncio
@pytest.mark.parametrize("path", [REWRITE, "/users/{id}/federated-identity"])
async def test_a_body_that_is_not_json_is_a_422_before_the_role_gate(client, path):
    """FastAPI decodes the body before it runs the dependencies, so a body that
    is not JSON answers 422 `json_invalid` even with no token; the answer is the
    same for any id and any caller, so it says nothing about accounts."""
    user = await make_user("plain")
    answers = []
    for target, headers in ((uuid.uuid4(), {}), (user.id, bearer(user))):
        url = path.replace("{id}", str(target))
        method = "POST" if path == REWRITE else "PUT"
        resp = await client.request(
            method, url, content=b"{", headers={"Content-Type": "application/json", **headers}
        )
        assert resp.status_code == 422
        answers.append(resp.json())
    assert answers[0] == answers[1]
    assert [e["type"] for e in answers[0]["detail"]] == ["json_invalid"]
    resp = await client.get(candidates_path(uuid.uuid4()))
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_a_lone_surrogate_in_a_subject_is_the_shared_422_before_the_role_gate(
    client, superadmin
):
    """The shared body check (OPS-VALID-6) refuses a body holding a lone surrogate when
    the route reads it, before the dependencies run, as FastAPI does for a body that is
    not JSON: every caller, with or without a token and whatever its role, gets the same
    422 `string_unicode` at the subject, for any id, so it says nothing about accounts.
    The identity-key predicate still refuses the Cs character for a value that reaches
    the schema another way."""
    victim = await make_oidc("victim")
    user = await make_user("plain")
    body = b'{"issuer": "https://idp.test", "subject": "a\\ud800b"}'
    answers = []
    for target, headers in (
        (victim.id, bearer(superadmin)),
        (victim.id, bearer(user)),
        (uuid.uuid4(), {}),
    ):
        resp = await client.put(
            relink_path(target),
            content=body,
            headers={"Content-Type": "application/json", **headers},
        )
        assert resp.status_code == 422, resp.text
        answers.append(resp.json())
    assert answers[0] == answers[1] == answers[2]
    assert [(e["loc"], e["type"]) for e in answers[0]["detail"]] == [
        (["body", "subject"], "string_unicode")
    ]
    with pytest.raises(ValidationError) as caught:
        FederatedIdentityLinkRequest.model_validate(
            {"issuer": "https://idp.test", "subject": "a\ud800b"}
        )
    assert [(e["loc"], e["type"]) for e in caught.value.errors()] == [
        (("subject",), "identity_key_invalid")
    ]
