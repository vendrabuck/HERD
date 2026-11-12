"""Unit tests for OIDC just-in-time provisioning (ADR 0016 phase 2, D4 and D7).

`resolve_federated_user` and `create_oidc_user` in auth_service, on in-memory
SQLite. The collision matrix crosses every existing account source (local, ldap,
and another oidc identity) with every way the new login can collide (email,
email in another case, username, username in another case, both); every cell
must refuse, link nothing, create nothing, and leave the existing account as it
was. The concurrent-first-login race is driven deterministically here by
interposing a competing login at each collision check; the real concurrent race
is proven on Postgres in test_oidc_flow_service_live_pg.py.
"""

from __future__ import annotations

import dataclasses
import logging
import uuid

import pytest
from app.database import Base
from app.models.federated_identity import FederatedIdentity
from app.models.group import UserGroup
from app.models.user import Role, User
from app.services import auth_service
from app.services.auth_service import (
    FederatedLogin,
    create_oidc_user,
    create_tokens_for_user,
    create_user,
    resolve_federated_user,
    rotate_refresh_token,
)
from app.services.group_service import get_user_groups
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
TestSessionLocal = async_sessionmaker(engine, expire_on_commit=False)

ISSUER = "https://idp.example.test/realm"
LOGGER = "app.services.auth_service"


@pytest.fixture(autouse=True)
async def setup_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


def _login(**overrides) -> FederatedLogin:
    base = dict(
        issuer=ISSUER,
        subject="sub-alice-0001",
        email="alice@example.com",
        email_verified=True,
        username="alice",
        full_name="Alice Liddell",
    )
    base.update(overrides)
    return FederatedLogin(**base)


async def _counts() -> tuple[int, int]:
    async with TestSessionLocal() as db:
        users = (await db.execute(select(func.count()).select_from(User))).scalar_one()
        idents = (
            await db.execute(select(func.count()).select_from(FederatedIdentity))
        ).scalar_one()
    return users, idents


async def _identities() -> list[FederatedIdentity]:
    async with TestSessionLocal() as db:
        return list((await db.execute(select(FederatedIdentity))).scalars().all())


def _snapshot(user: User) -> dict:
    return {
        "id": user.id,
        "email": user.email,
        "username": user.username,
        "full_name": user.full_name,
        "auth_source": user.auth_source,
        "role": user.role,
        "is_active": user.is_active,
        "hashed_password": user.hashed_password,
    }


async def _load(user_id: uuid.UUID) -> User:
    async with TestSessionLocal() as db:
        return (await db.execute(select(User).where(User.id == user_id))).scalar_one()


def _refusals(caplog) -> list[logging.LogRecord]:
    return [
        r
        for r in caplog.records
        if r.name == LOGGER
        and getattr(r, "action", None) == "login_failure"
        and getattr(r, "source", None) == "oidc"
    ]


# --- first login creates an oidc account ------------------------------------------


async def test_first_login_creates_an_oidc_user_with_no_password_and_role_user():
    async with TestSessionLocal() as db:
        user = await resolve_federated_user(db, _login())
    assert user is not None
    row = await _load(user.id)
    assert row.auth_source == "oidc"
    assert row.hashed_password is None
    assert row.role == Role.USER
    assert row.is_active is True
    assert (row.email, row.username, row.full_name) == (
        "alice@example.com",
        "alice",
        "Alice Liddell",
    )
    [identity] = await _identities()
    assert (identity.user_id, identity.issuer, identity.subject) == (
        user.id,
        ISSUER,
        "sub-alice-0001",
    )
    assert await _counts() == (1, 1)


@pytest.mark.parametrize(
    "full_name,stored",
    [
        (None, "alice"),
        ("", "alice"),
        ("   ", "alice"),
        ("  Alice  ", "Alice"),
        ("n" * 200, "n" * 200),
        ("n" * 201, "n" * 200),
    ],
    ids=["absent", "empty", "blank", "stripped", "200", "201-truncated"],
)
async def test_first_login_full_name_defaults_and_truncation(full_name, stored):
    async with TestSessionLocal() as db:
        user = await resolve_federated_user(db, _login(full_name=full_name))
    assert (await _load(user.id)).full_name == stored


async def test_first_login_auto_assigns_not_grouped():
    async with TestSessionLocal() as db:
        db.add(UserGroup(name="Not Grouped", description="Default"))
        await db.commit()
        user = await resolve_federated_user(db, _login())
        groups = await get_user_groups(db, user.id)
    assert {g.name for g in groups} == {"Not Grouped"}


@pytest.mark.parametrize("verified", [True, None], ids=["verified-true", "claim-absent"])
async def test_email_verified_true_or_absent_is_accepted(verified):
    """ADR 0016 D4: refuse only when email_verified is present and false."""
    async with TestSessionLocal() as db:
        assert await resolve_federated_user(db, _login(email_verified=verified)) is not None


@pytest.mark.parametrize(
    "overrides",
    [
        {"username": "u" * 100},
        {"email": "e" * 243 + "@example.com"},
        {"subject": "s" * 255},
    ],
    ids=["username-100", "email-255", "subject-255"],
)
async def test_values_at_the_column_width_are_accepted(overrides):
    assert len(next(iter(overrides.values()))) in (100, 255)
    async with TestSessionLocal() as db:
        assert await resolve_federated_user(db, _login(**overrides)) is not None


# --- first-login refusals that need no database -----------------------------------

REFUSALS = [
    ({"email": None}, "email_missing"),
    ({"email": ""}, "email_missing"),
    ({"email": "   "}, "email_missing"),
    ({"email_verified": False}, "email_unverified"),
    ({"email": "e" * 244 + "@example.com"}, "email_too_long"),
    ({"username": None}, "username_missing"),
    ({"username": ""}, "username_missing"),
    ({"username": "   "}, "username_missing"),
    ({"username": "u" * 101}, "username_too_long"),
    ({"subject": ""}, "subject_invalid"),
    ({"subject": "s" * 256}, "subject_invalid"),
]


@pytest.mark.parametrize(
    "overrides,reason", REFUSALS, ids=[f"{r}-{i}" for i, (_, r) in enumerate(REFUSALS)]
)
async def test_first_login_refusal_creates_nothing_and_logs_one_fixed_reason(
    caplog, overrides, reason
):
    login = _login(**overrides)
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        async with TestSessionLocal() as db:
            assert await resolve_federated_user(db, login) is None
    assert await _counts() == (0, 0)
    [record] = _refusals(caplog)
    assert record.reason == reason


# --- the collision matrix -----------------------------------------------------------


async def _existing(source: str) -> User:
    """An existing account of `source` holding email bob@example.com and
    username bob."""
    async with TestSessionLocal() as db:
        if source == "local":
            return await create_user(db, "bob@example.com", "bob", "password123")
        if source == "ldap":
            return await auth_service.create_ldap_user(db, "bob@example.com", "bob")
        return await create_oidc_user(
            db,
            _login(subject="sub-bob-other", email="bob@example.com", username="bob"),
        )


COLLISIONS = {
    "email": ({"email": "bob@example.com", "username": "newname"}, "email_taken"),
    "email-case": ({"email": "BoB@Example.COM", "username": "newname"}, "email_taken"),
    "username": ({"email": "new@example.com", "username": "bob"}, "username_taken"),
    "username-case": ({"email": "new@example.com", "username": "BOB"}, "username_taken"),
    "both": ({"email": "bob@example.com", "username": "bob"}, "email_taken"),
    "both-case": ({"email": "BOB@EXAMPLE.COM", "username": "Bob"}, "email_taken"),
}


@pytest.mark.parametrize("source", ["local", "ldap", "oidc"])
@pytest.mark.parametrize("kind", sorted(COLLISIONS))
async def test_collision_matrix_refuses_and_never_links(caplog, source, kind):
    existing = await _existing(source)
    before = _snapshot(await _load(existing.id))
    users_before, idents_before = await _counts()
    overrides, reason = COLLISIONS[kind]
    login = _login(subject="sub-new-login", **overrides)

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        async with TestSessionLocal() as db:
            assert await resolve_federated_user(db, login) is None

    assert await _counts() == (users_before, idents_before)
    assert _snapshot(await _load(existing.id)) == before
    assert not [i for i in await _identities() if i.subject == "sub-new-login"]
    [record] = _refusals(caplog)
    assert record.reason == reason


async def test_collision_refusal_holds_for_an_inactive_existing_account():
    existing = await _existing("local")
    async with TestSessionLocal() as db:
        await auth_service.set_user_active(db, existing.id, False)
        assert await resolve_federated_user(db, _login(email="bob@example.com")) is None
    assert await _counts() == (1, 0)


# --- returning logins -----------------------------------------------------------------


async def test_returning_login_returns_the_same_user_and_updates_nothing_but_last_login():
    async with TestSessionLocal() as db:
        first = await resolve_federated_user(db, _login())
    before = _snapshot(await _load(first.id))
    [identity] = await _identities()
    assert identity.last_login_at is None

    changed = _login(
        email="renamed@example.com",
        email_verified=False,
        username="renamed",
        full_name="Someone Else",
    )
    async with TestSessionLocal() as db:
        again = await resolve_federated_user(db, changed)
    assert again is not None and again.id == first.id
    assert _snapshot(await _load(first.id)) == before
    [identity] = await _identities()
    assert identity.last_login_at is not None
    assert await _counts() == (1, 1)


async def test_returning_login_with_another_issuer_is_a_different_identity():
    async with TestSessionLocal() as db:
        await resolve_federated_user(db, _login())
        other = await resolve_federated_user(
            db, _login(issuer="https://other.example.test", email="x@example.com", username="x")
        )
    assert other is not None
    assert await _counts() == (2, 2)


async def test_returning_login_refused_for_an_inactive_user_and_last_login_untouched(caplog):
    async with TestSessionLocal() as db:
        user = await resolve_federated_user(db, _login())
        await auth_service.set_user_active(db, user.id, False)
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        async with TestSessionLocal() as db:
            assert await resolve_federated_user(db, _login()) is None
    [identity] = await _identities()
    assert identity.last_login_at is None
    [record] = _refusals(caplog)
    assert record.reason == "inactive"
    assert record.user_id == str(user.id)


async def test_identity_linked_to_a_non_oidc_account_is_refused(caplog):
    async with TestSessionLocal() as db:
        local = await create_user(db, "carol@example.com", "carol", "password123")
        db.add(FederatedIdentity(user_id=local.id, issuer=ISSUER, subject="sub-carol"))
        await db.commit()
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        async with TestSessionLocal() as db:
            assert await resolve_federated_user(db, _login(subject="sub-carol")) is None
    [record] = _refusals(caplog)
    assert record.reason == "wrong_auth_source"


async def test_identity_whose_user_is_gone_is_refused(caplog):
    # SQLite does not enforce the foreign key here, so the orphan can exist.
    async with TestSessionLocal() as db:
        db.add(FederatedIdentity(user_id=uuid.uuid4(), issuer=ISSUER, subject="sub-orphan"))
        await db.commit()
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        async with TestSessionLocal() as db:
            assert await resolve_federated_user(db, _login(subject="sub-orphan")) is None
    [record] = _refusals(caplog)
    assert record.reason == "user_missing"
    assert await _counts() == (0, 1)


# --- a concurrent first login of the same identity -----------------------------------


async def _interpose(monkeypatch, name: str, competitor) -> dict:
    """Run `competitor` once, in its own session, at the start of auth_service's
    `name` check, then let the check run as written."""
    real = getattr(auth_service, name)
    state = {"fired": False}

    async def wrapped(db, value):
        if not state["fired"]:
            state["fired"] = True
            async with TestSessionLocal() as other:
                state["result"] = await competitor(other)
        return await real(db, value)

    monkeypatch.setattr(auth_service, name, wrapped)
    return state


@pytest.mark.parametrize("check", ["_email_taken", "_username_taken"])
async def test_concurrent_first_login_of_the_same_identity_yields_one_user(monkeypatch, check):
    """The competitor provisions the same (issuer, subject) inside this login's
    window: at the email check (which then sees the email taken) or at the
    username check (the insert then hits the unique constraints). Either way
    the loser finds the identity on its second look and returns the winner's
    account: one user, one identity, both logins admitted."""
    state = await _interpose(monkeypatch, check, lambda s: resolve_federated_user(s, _login()))
    async with TestSessionLocal() as db:
        loser = await resolve_federated_user(db, _login())
    assert state["fired"]
    winner = state["result"]
    assert winner is not None and loser is not None
    assert loser.id == winner.id
    assert await _counts() == (1, 1)


async def test_another_account_taking_the_email_inside_the_window_is_refused(monkeypatch, caplog):
    """A different account (a local registration) commits the same email after
    the email check: the insert hits the unique constraint, the identity still
    does not exist, and the login is refused without linking."""
    state = await _interpose(
        monkeypatch,
        "_username_taken",
        lambda s: create_user(s, "alice@example.com", "alice-local", "password123"),
    )
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        async with TestSessionLocal() as db:
            assert await resolve_federated_user(db, _login()) is None
    assert state["fired"]
    assert await _counts() == (1, 0)
    [record] = _refusals(caplog)
    assert record.reason == "provision_conflict"


async def test_create_oidc_user_rolls_back_both_rows_on_a_conflict():
    from sqlalchemy.exc import IntegrityError

    async with TestSessionLocal() as db:
        await create_oidc_user(db, _login())
        with pytest.raises(IntegrityError):
            await create_oidc_user(db, _login(email="other@example.com", username="other"))
    assert await _counts() == (1, 1)


# --- the session is an ordinary HERD session --------------------------------------------


async def test_oidc_user_session_uses_ordinary_refresh_rotation():
    async with TestSessionLocal() as db:
        user = await resolve_federated_user(db, _login())
        access, refresh = await create_tokens_for_user(db, user)
        assert access and refresh
        rotated = await rotate_refresh_token(db, refresh)
        assert rotated is not None
        assert await rotate_refresh_token(db, refresh) is None
        await auth_service.set_user_active(db, user.id, False)
        assert await rotate_refresh_token(db, rotated[1]) is None


# --- log hygiene ----------------------------------------------------------------------


async def test_refusal_logs_carry_no_claim_values(caplog):
    marker = dataclasses.replace(
        _login(),
        issuer="https://MARKER-ISSUER.example.test",
        subject="MARKER-SUBJECT",
        email="MARKER-EMAIL@example.com",
        username="MARKER-USERNAME",
        full_name="MARKER-NAME",
    )
    await _existing("local")
    cases = [
        dataclasses.replace(marker, email_verified=False),
        dataclasses.replace(marker, username="u" * 101 + "MARKER-USERNAME"),
        dataclasses.replace(marker, email="bob@example.com"),
        dataclasses.replace(marker, username="bob"),
    ]
    with caplog.at_level(logging.DEBUG, logger="app"):
        async with TestSessionLocal() as db:
            for login in cases:
                assert await resolve_federated_user(db, login) is None
            user = await resolve_federated_user(db, marker)
            await auth_service.set_user_active(db, user.id, False)
            assert await resolve_federated_user(db, marker) is None
    records = [r for r in caplog.records if r.name.startswith("app.")]
    assert len(_refusals(caplog)) == 5
    for record in records:
        text = record.getMessage() + " " + " ".join(str(v) for v in record.__dict__.values())
        for value in ("MARKER-ISSUER", "MARKER-SUBJECT", "MARKER-EMAIL", "MARKER-NAME"):
            assert value not in text, f"{value} reached a log record"
        if record in _refusals(caplog):
            assert "MARKER-USERNAME" not in text


# --- review round: unstorable and look-alike claim content ----------------------------

# Characters Postgres cannot store (NUL and other Cc; a lone surrogate, Cs) and
# an invisible format character (Cf). SQLite stores all of them, so these unit
# tests prove the refusal happens; test_oidc_flow_service_live_pg.py proves
# Postgres would have raised without it.
NUL, UNIT_SEP, DEL, SURROGATE, ZWSP = "\x00", "\x1f", "\x7f", "\ud800", "​"

IDENTITY_KEY_CASES = [
    ("issuer", None, "issuer_invalid"),
    ("issuer", "", "issuer_invalid"),
    ("issuer", "   ", "issuer_invalid"),
    ("issuer", "https://idp.example/" + "i" * 236, "issuer_invalid"),
    ("issuer", ISSUER + NUL, "issuer_invalid"),
    ("issuer", ISSUER + UNIT_SEP, "issuer_invalid"),
    ("issuer", ISSUER + SURROGATE, "issuer_invalid"),
    ("subject", None, "subject_invalid"),
    ("subject", "", "subject_invalid"),
    ("subject", " \t ", "subject_invalid"),
    ("subject", "s" * 256, "subject_invalid"),
    ("subject", "sub" + NUL + "x", "subject_invalid"),
    ("subject", "sub" + DEL, "subject_invalid"),
    ("subject", "sub" + SURROGATE, "subject_invalid"),
]


@pytest.mark.parametrize(
    "field,value,reason",
    IDENTITY_KEY_CASES,
    ids=[f"{f}-{i}" for i, (f, _, _) in enumerate(IDENTITY_KEY_CASES)],
)
async def test_issuer_and_subject_refused_before_the_identity_lookup(
    monkeypatch, caplog, field, value, reason
):
    async def no_lookup(*args, **kwargs):
        raise AssertionError("the identity lookup ran on an unchecked issuer or subject")

    monkeypatch.setattr(auth_service, "_find_federated_identity", no_lookup)
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        async with TestSessionLocal() as db:
            assert await resolve_federated_user(db, _login(**{field: value})) is None
    assert await _counts() == (0, 0)
    [record] = _refusals(caplog)
    assert record.reason == reason


def test_issuer_at_255_characters_is_accepted_by_the_key_check():
    login = _login(issuer="https://idp.example/" + "i" * 235)
    assert len(login.issuer) == 255
    assert auth_service._identity_key_refusal(login) is None


NAME_CASES = [
    ({"email": " alice@example.com"}, "email_invalid"),
    ({"email": "alice@example.com "}, "email_invalid"),
    ({"email": "alice@example.com\n"}, "email_invalid"),
    ({"email": "ali" + ZWSP + "ce@example.com"}, "email_invalid"),
    ({"email": "alice@example.com" + NUL}, "email_invalid"),
    ({"email": "al" + NUL + "ice@example.com"}, "email_invalid"),
    ({"email": "alice" + SURROGATE + "@example.com"}, "email_invalid"),
    ({"email": "alice.example.com"}, "email_invalid"),
    ({"email": "alice@@example.com"}, "email_invalid"),
    ({"email": "a@b@example.com"}, "email_invalid"),
    ({"email": "@example.com"}, "email_invalid"),
    ({"email": "alice@"}, "email_invalid"),
    ({"email_verified": "false"}, "email_unverified"),
    ({"email_verified": 0}, "email_unverified"),
    ({"email": 42}, "email_missing"),
    ({"username": " alice"}, "username_invalid"),
    ({"username": "alice "}, "username_invalid"),
    ({"username": "ali" + ZWSP + "ce"}, "username_invalid"),
    ({"username": "ali" + NUL + "ce"}, "username_invalid"),
    ({"username": "alice" + DEL}, "username_invalid"),
    ({"username": "alice" + SURROGATE}, "username_invalid"),
    ({"username": 42}, "username_missing"),
]


@pytest.mark.parametrize(
    "overrides,reason", NAME_CASES, ids=[f"{r}-{i}" for i, (_, r) in enumerate(NAME_CASES)]
)
async def test_email_and_username_content_refusals(caplog, overrides, reason):
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        async with TestSessionLocal() as db:
            assert await resolve_federated_user(db, _login(**overrides)) is None
    assert await _counts() == (0, 0)
    [record] = _refusals(caplog)
    assert record.reason == reason


LOOKALIKES = {
    "leading-space": (" admin@t.example", "email_invalid"),
    "trailing-space": ("admin@t.example ", "email_invalid"),
    "zero-width-space": ("ad" + ZWSP + "min@t.example", "email_invalid"),
    "case": ("ADMIN@T.EXAMPLE", "email_taken"),
}


@pytest.mark.parametrize("kind", sorted(LOOKALIKES))
async def test_lookalike_of_an_existing_email_is_refused(kind):
    async with TestSessionLocal() as db:
        await create_user(db, "admin@t.example", "admin", "password123")
    email, _ = LOOKALIKES[kind]
    async with TestSessionLocal() as db:
        assert await resolve_federated_user(db, _login(email=email, username="other")) is None
    assert await _counts() == (1, 0)


@pytest.mark.parametrize(
    "email", ["admın@t.example", "ａdmin@t.example"], ids=["dotless-i", "fullwidth-a"]
)
async def test_visible_unicode_lookalikes_are_distinct_emails_today(email):
    """No Unicode normalization or confusable check exists yet (no owner
    decision): a visible look-alike is a different email and is provisioned.
    Pinned so a change here is deliberate; see IAM-OIDC-10."""
    async with TestSessionLocal() as db:
        await create_user(db, "admin@t.example", "admin", "password123")
        assert await resolve_federated_user(db, _login(email=email, username="other")) is not None


@pytest.mark.parametrize(
    "full_name,stored",
    [
        ("Ali" + NUL + "ce", "Ali ce"),
        ("Alice\nLiddell", "Alice Liddell"),
        (NUL + UNIT_SEP, "alice"),
        (SURROGATE + "Alice", "Alice"),
        ("Ali" + ZWSP + "ce", "Ali" + ZWSP + "ce"),
        (42, "alice"),
    ],
    ids=["nul", "newline", "only-controls", "surrogate", "zero-width-kept", "non-string"],
)
async def test_full_name_control_characters_are_replaced(full_name, stored):
    async with TestSessionLocal() as db:
        user = await resolve_federated_user(db, _login(full_name=full_name))
    assert (await _load(user.id)).full_name == stored


BAD_CHARACTERS = [NUL, UNIT_SEP, DEL, SURROGATE, ZWSP, "\n", " "]


@pytest.mark.parametrize("field", ["issuer", "subject", "email", "username", "full_name"])
@pytest.mark.parametrize("bad", BAD_CHARACTERS, ids=[repr(c) for c in BAD_CHARACTERS])
@pytest.mark.parametrize("where", ["start", "middle", "end"])
async def test_resolve_never_raises_on_claim_content(field, bad, where):
    base = getattr(_login(), field)
    value = {"start": bad + base, "middle": base[:2] + bad + base[2:], "end": base + bad}[where]
    async with TestSessionLocal() as db:
        result = await resolve_federated_user(db, _login(**{field: value}))
    assert result is None or isinstance(result, User)


# --- the first-login advisory locks (Postgres only; wiring checked here) -----------------


async def test_first_login_takes_both_name_locks_in_key_order_before_the_checks(monkeypatch):
    calls: list[tuple[str, object]] = []

    async def record_lock(session, key):
        calls.append(("lock", key))

    real_email_taken = auth_service._email_taken

    async def record_email_taken(db, email):
        calls.append(("email_taken", email))
        return await real_email_taken(db, email)

    monkeypatch.setattr(auth_service, "xact_lock", record_lock)
    monkeypatch.setattr(auth_service, "_email_taken", record_email_taken)
    async with TestSessionLocal() as db:
        assert await resolve_federated_user(db, _login(email="Victim@X.example")) is not None
    locks = [k for kind, k in calls if kind == "lock"]
    assert len(locks) == 2 and locks == sorted(locks)
    assert [kind for kind, _ in calls].index("email_taken") == 2
    from herd_common.advisory_lock import advisory_key_from_string

    assert advisory_key_from_string("oidc-first-login-email:victim@x.example") in locks
    assert advisory_key_from_string("oidc-first-login-username:alice") in locks


async def test_name_lock_keys_ignore_case(monkeypatch):
    keys: list[list[int]] = []

    async def record_lock(session, key):
        keys[-1].append(key)

    monkeypatch.setattr(auth_service, "xact_lock", record_lock)
    for i, (email, username) in enumerate(
        [("Victim@X.example", "Bob"), ("victim@x.example", "BOB")]
    ):
        keys.append([])
        async with TestSessionLocal() as db:
            await resolve_federated_user(
                db, _login(subject=f"sub-{i}", email=email, username=username)
            )
    assert keys[0] == keys[1] and len(keys[0]) == 2


async def test_refusals_before_the_insert_take_no_name_lock(monkeypatch):
    taken = []

    async def record_lock(session, key):
        taken.append(key)

    monkeypatch.setattr(auth_service, "xact_lock", record_lock)
    async with TestSessionLocal() as db:
        assert await resolve_federated_user(db, _login(email_verified=False)) is None
        assert await resolve_federated_user(db, _login(subject="")) is None
    assert taken == []


async def test_resolve_ends_its_transaction_on_every_refusal_and_commits_a_success():
    """IAM-OIDC-14: every refusal rolls back; a success commits its writes (seen
    from another session) and returns a loaded user."""
    await _existing("local")
    async with TestSessionLocal() as db:
        for login in (
            _login(subject=""),
            _login(email_verified=False),
            _login(email="bob@example.com"),
        ):
            assert await resolve_federated_user(db, login) is None
            assert not db.in_transaction()
        for _ in range(2):  # the first login, then the returning one
            user = await resolve_federated_user(db, _login())
            assert user is not None and user.username == "alice"
            async with TestSessionLocal() as other:
                found = await other.execute(select(User.id).where(User.id == user.id))
                assert found.scalar_one_or_none() == user.id
        await auth_service.set_user_active(db, user.id, False)
        assert await resolve_federated_user(db, _login()) is None
        assert not db.in_transaction()
