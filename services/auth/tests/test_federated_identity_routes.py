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

import logging
import uuid
from datetime import datetime

import httpx
import pytest
from app.config import settings
from app.models.federated_identity import FederatedIdentity
from app.models.user import Role, User
from app.routers import federated_identities as routes
from app.services import oidc_service
from app.services.api_token_service import create_api_token
from app.services.auth_service import get_password_hash
from app.utils.jwt import create_access_token
from cryptography.hazmat.primitives.asymmetric import rsa
from herd_common.logging import JSONFormatter
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
    assert resp.json() == {"dry_run": False, "matched": 1, "conflicts": 0, "rewritten": 1}
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
    assert resp.json() == {"dry_run": True, "matched": 1, "conflicts": 0, "rewritten": 0}


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
        {"dry_run": True, "matched": 2, "conflicts": 0, "rewritten": 0},
    )
    assert (await identities())[a.id][0] == ISSUER
    resp = await client.post(REWRITE, json={**body, "dry_run": False}, headers=bearer(superadmin))
    assert (resp.status_code, resp.json()) == (
        200,
        {"dry_run": False, "matched": 2, "conflicts": 0, "rewritten": 2},
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
    await make_oidc("a")
    await make_oidc("b")
    await make_oidc("held-a", issuer=NEW_ISSUER, subject="sub-a")
    before = await identities()
    body = {"old_issuer": ISSUER, "new_issuer": NEW_ISSUER, "dry_run": False}
    resp = await client.post(REWRITE, json=body, headers=bearer(superadmin))
    assert resp.status_code == 409
    assert resp.json() == {
        "detail": "Rewrite refused: 1 federated identity would collide with an identity "
        "already under the new issuer; nothing was changed"
    }
    await make_oidc("held-b", issuer=NEW_ISSUER, subject="sub-b")
    resp = await client.post(REWRITE, json=body, headers=bearer(superadmin))
    assert resp.json() == {
        "detail": "Rewrite refused: 2 federated identities would collide with an identity "
        "already under the new issuer; nothing was changed"
    }
    dry = await client.post(REWRITE, json={**body, "dry_run": True}, headers=bearer(superadmin))
    assert dry.json() == {"dry_run": True, "matched": 2, "conflicts": 2, "rewritten": 0}
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
        ({"issuer": ISSUER}, ["body", "subject"], "missing"),
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
    assert dry.json() == {"dry_run": True, "matched": 1, "conflicts": 0, "rewritten": 0}
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
    # The documented way out with these two routes alone: retire the stray
    # account's identity to a subject no provider issues, then rewrite.
    retired = await client.put(
        relink_path(stray),
        json={"issuer": NEW_ISSUER, "subject": f"retired-{stray}"},
        headers=bearer(superadmin),
    )
    assert retired.status_code == 200
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
