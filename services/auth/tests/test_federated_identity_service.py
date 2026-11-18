"""Unit tests for the superadmin federated-identity recovery (issue #1399).

`app/services/federated_identity_service.py` on the shared in-memory SQLite
harness. Pinned here: the issuer rewrite's exact match (no trailing-slash or
case normalization), its dry run, its all-or-nothing refusal with a count, its
loss to a constraint between the count and the UPDATE; the relink's refusals
and its replace and no-op paths; that neither ever changes an account id or
touches a row it was not asked to; that every function ends its transaction on
every outcome; and that the request bodies accept exactly the identity keys a
login accepts (IAM-OIDC-32). The concurrent cases run against Postgres in
`test_federated_identity_live_pg.py`.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from app.models.federated_identity import SUBJECT_MAX_LENGTH, FederatedIdentity
from app.models.user import Role, User
from app.schemas.federated_identity import FederatedIdentityLinkRequest, IssuerRewriteRequest
from app.services import auth_service, federated_identity_service
from app.services.auth_service import (
    ISSUER_MAX_LENGTH,
    FederatedLogin,
    _identity_key_refusal,
    identity_key_part_ok,
)
from app.services.federated_identity_service import (
    IssuerRewriteConflict,
    RelinkRefused,
    relink_identity,
    rewrite_issuer,
)
from pydantic import ValidationError
from sqlalchemy import select

from tests._harness import TestSessionLocal

OLD = "https://idp.example.test/realms/herd"
NEW = "https://idp.example.test/realms/herd/"
OTHER = "https://other.example.test"
LAST_LOGIN = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)


async def _account(name: str, *, source: str = "oidc", role: Role = Role.USER) -> uuid.UUID:
    async with TestSessionLocal() as db:
        user = User(
            email=f"{name}@example.test",
            username=name,
            full_name=name,
            hashed_password=None if source != "local" else "x",
            auth_source=source,
            role=role,
        )
        db.add(user)
        await db.commit()
        return user.id


async def _identity(
    user_id: uuid.UUID, issuer: str, subject: str, *, last_login: datetime | None = LAST_LOGIN
) -> uuid.UUID:
    async with TestSessionLocal() as db:
        row = FederatedIdentity(
            user_id=user_id, issuer=issuer, subject=subject, last_login_at=last_login
        )
        db.add(row)
        await db.commit()
        return row.id


async def _rows() -> dict[uuid.UUID, tuple]:
    """Every identity row by account: (id, issuer, subject, created_at, last_login_at)."""
    async with TestSessionLocal() as db:
        rows = (await db.execute(select(FederatedIdentity))).scalars().all()
        return {r.user_id: (r.id, r.issuer, r.subject, r.created_at, r.last_login_at) for r in rows}


async def _account_ids() -> set[uuid.UUID]:
    async with TestSessionLocal() as db:
        return set((await db.execute(select(User.id))).scalars().all())


async def _oidc(name: str, issuer: str = OLD, subject: str | None = None) -> uuid.UUID:
    user_id = await _account(name)
    await _identity(user_id, issuer, subject or f"sub-{name}")
    return user_id


# --- rewrite_issuer ---


@pytest.mark.asyncio
async def test_rewrite_moves_exactly_the_old_issuers_rows_and_keeps_everything_else():
    a = await _oidc("a")
    b = await _oidc("b")
    c = await _oidc("c", issuer=OTHER)
    trailing = await _oidc("d", issuer=OLD + "/x")
    upper = await _oidc("e", issuer=OLD.upper())
    accounts = await _account_ids()
    before = await _rows()

    async with TestSessionLocal() as db:
        result = await rewrite_issuer(db, old_issuer=OLD, new_issuer=NEW, dry_run=False)
        assert not db.in_transaction()

    assert (result.matched, result.conflicts, result.rewritten) == (2, 0, 2)
    after = await _rows()
    for moved in (a, b):
        row_id, _, subject, created, last_login = before[moved]
        assert after[moved] == (row_id, NEW, subject, created, last_login)
    for untouched in (c, trailing, upper):
        assert after[untouched] == before[untouched]
    assert await _account_ids() == accounts


@pytest.mark.asyncio
async def test_rewrite_compares_the_exact_string_with_no_trailing_slash_normalization():
    # OLD and NEW differ only by the trailing slash; a rewrite from NEW finds
    # nothing under OLD, and one from OLD + " " (a trailing space) neither.
    await _oidc("a")
    before = await _rows()
    for old in (NEW, OLD + " ", OLD.upper()):
        async with TestSessionLocal() as db:
            result = await rewrite_issuer(db, old_issuer=old, new_issuer=OTHER, dry_run=False)
        assert (result.matched, result.rewritten) == (0, 0)
    assert await _rows() == before


@pytest.mark.asyncio
async def test_dry_run_counts_and_writes_nothing():
    await _oidc("a")
    await _oidc("b")
    held = await _oidc("held", issuer=NEW, subject="sub-b")
    before = await _rows()
    async with TestSessionLocal() as db:
        result = await rewrite_issuer(db, old_issuer=OLD, new_issuer=NEW, dry_run=True)
        assert not db.in_transaction()
    assert (result.matched, result.conflicts, result.rewritten) == (2, 1, 0)
    assert await _rows() == before
    assert held in before


@pytest.mark.asyncio
async def test_a_conflict_refuses_the_whole_rewrite_with_its_count():
    await _oidc("a")
    await _oidc("b")
    await _oidc("c")
    await _oidc("held-b", issuer=NEW, subject="sub-b")
    await _oidc("held-c", issuer=NEW, subject="sub-c")
    before = await _rows()
    async with TestSessionLocal() as db:
        with pytest.raises(IssuerRewriteConflict) as raised:
            await rewrite_issuer(db, old_issuer=OLD, new_issuer=NEW, dry_run=False)
        assert not db.in_transaction()
    assert raised.value.conflicts == 2
    assert await _rows() == before


@pytest.mark.asyncio
async def test_a_subject_under_a_third_issuer_is_not_a_conflict():
    a = await _oidc("a")
    await _oidc("elsewhere", issuer=OTHER, subject="sub-a")
    async with TestSessionLocal() as db:
        result = await rewrite_issuer(db, old_issuer=OLD, new_issuer=NEW, dry_run=False)
    assert (result.matched, result.conflicts, result.rewritten) == (1, 0, 1)
    assert (await _rows())[a][1] == NEW


@pytest.mark.asyncio
async def test_rewrite_that_loses_to_an_insert_after_its_count_is_refused_and_writes_nothing(
    monkeypatch,
):
    """A first login inserting (NEW, sub-a) between the count and the UPDATE: the
    unique constraint fails the UPDATE, nothing is written, and the conflict is
    reported with a recount. Simulated by a count that misses the row."""
    a = await _oidc("a")
    await _oidc("b")
    await _oidc("raced", issuer=NEW, subject="sub-a")
    before = await _rows()
    real = federated_identity_service._count_conflicts
    calls = []

    async def first_count_misses(db, old_issuer, new_issuer):
        calls.append(1)
        if len(calls) == 1:
            return 0
        return await real(db, old_issuer, new_issuer)

    monkeypatch.setattr(federated_identity_service, "_count_conflicts", first_count_misses)
    async with TestSessionLocal() as db:
        with pytest.raises(IssuerRewriteConflict) as raised:
            await rewrite_issuer(db, old_issuer=OLD, new_issuer=NEW, dry_run=False)
        assert not db.in_transaction()
    assert raised.value.conflicts == 1
    assert len(calls) == 2
    assert await _rows() == before
    assert before[a][1] == OLD


@pytest.mark.asyncio
async def test_rewrite_of_nothing_answers_zero():
    async with TestSessionLocal() as db:
        result = await rewrite_issuer(db, old_issuer=OLD, new_issuer=NEW, dry_run=False)
    assert (result.matched, result.conflicts, result.rewritten) == (0, 0, 0)


# --- relink_identity ---


@pytest.mark.asyncio
async def test_relink_replaces_the_identity_and_keeps_the_account_id():
    a = await _oidc("a")
    other = await _oidc("other")
    accounts = await _account_ids()
    before = await _rows()
    async with TestSessionLocal() as db:
        result = await relink_identity(db, a, issuer=NEW, subject="sub-a-reissued")
        assert not db.in_transaction()
    assert (result.changed, result.issuer_changed, result.subject_changed) == (True, True, True)
    after = await _rows()
    row_id, issuer, subject, created, last_login = after[a]
    assert (issuer, subject, last_login) == (NEW, "sub-a-reissued", None)
    assert row_id != before[a][0]
    assert result.identity.user_id == a
    assert (result.identity.issuer, result.identity.subject) == (NEW, "sub-a-reissued")
    assert result.identity.created_at == created
    assert result.identity.last_login_at is None
    assert after[other] == before[other]
    assert await _account_ids() == accounts
    assert len(after) == len(before)


@pytest.mark.asyncio
async def test_relink_reports_which_half_changed():
    a = await _oidc("a")
    async with TestSessionLocal() as db:
        result = await relink_identity(db, a, issuer=OLD, subject="sub-a-2")
    assert (result.issuer_changed, result.subject_changed) == (False, True)
    async with TestSessionLocal() as db:
        result = await relink_identity(db, a, issuer=NEW, subject="sub-a-2")
    assert (result.issuer_changed, result.subject_changed) == (True, False)


@pytest.mark.asyncio
async def test_relink_to_the_pair_already_held_writes_nothing():
    a = await _oidc("a")
    before = await _rows()
    async with TestSessionLocal() as db:
        result = await relink_identity(db, a, issuer=OLD, subject="sub-a")
        assert not db.in_transaction()
    assert (result.changed, result.issuer_changed, result.subject_changed) == (False, False, False)
    assert (result.identity.issuer, result.identity.subject) == (OLD, "sub-a")
    assert result.identity.last_login_at is not None
    assert await _rows() == before


@pytest.mark.asyncio
async def test_relink_gives_an_oidc_account_without_an_identity_one():
    lone = await _account("lone")
    async with TestSessionLocal() as db:
        result = await relink_identity(db, lone, issuer=OLD, subject="sub-lone")
    assert result.changed is True
    assert (await _rows())[lone][1:3] == (OLD, "sub-lone")


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["local", "ldap", "api_token", ""])
async def test_relink_refuses_every_account_that_is_not_oidc(source):
    target = await _account(f"t-{source or 'blank'}", source=source)
    before = await _rows()
    async with TestSessionLocal() as db:
        with pytest.raises(RelinkRefused) as raised:
            await relink_identity(db, target, issuer=OLD, subject="sub-x")
        assert not db.in_transaction()
    assert raised.value.reason == "not_oidc"
    assert await _rows() == before


@pytest.mark.asyncio
async def test_relink_refuses_the_local_superadmin():
    sa = await _account("sa", source="local", role=Role.SUPERADMIN)
    async with TestSessionLocal() as db:
        with pytest.raises(RelinkRefused) as raised:
            await relink_identity(db, sa, issuer=OLD, subject="sub-sa")
    assert raised.value.reason == "not_oidc"
    assert sa not in await _rows()


@pytest.mark.asyncio
async def test_relink_refuses_an_unknown_account():
    async with TestSessionLocal() as db:
        with pytest.raises(RelinkRefused) as raised:
            await relink_identity(db, uuid.uuid4(), issuer=OLD, subject="sub-x")
        assert not db.in_transaction()
    assert raised.value.reason == "user_missing"


@pytest.mark.asyncio
async def test_relink_refuses_a_pair_another_account_holds():
    a = await _oidc("a")
    await _oidc("b")
    before = await _rows()
    async with TestSessionLocal() as db:
        with pytest.raises(RelinkRefused) as raised:
            await relink_identity(db, a, issuer=OLD, subject="sub-b")
        assert not db.in_transaction()
    assert raised.value.reason == "identity_taken"
    assert await _rows() == before


@pytest.mark.asyncio
async def test_relink_that_loses_to_an_insert_after_its_check_is_refused_as_taken(monkeypatch):
    """Another writer takes the pair between the holder check and the insert:
    the unique constraint fails the insert, the delete is rolled back with it,
    and the account keeps its old identity."""
    a = await _oidc("a")
    await _oidc("b")
    before = await _rows()

    async def blind(db, issuer, subject):
        return None

    monkeypatch.setattr(federated_identity_service, "_holder_of", blind)
    async with TestSessionLocal() as db:
        with pytest.raises(RelinkRefused) as raised:
            await relink_identity(db, a, issuer=OLD, subject="sub-b")
        assert not db.in_transaction()
    assert raised.value.reason == "identity_taken"
    assert await _rows() == before


# --- the identity key predicate: one judge for logins and for the routes ---


IDENTITY_KEY_CASES = [
    "x",
    " x",
    "x ",
    " ",
    "",
    "\t",
    "a\x00b",
    "a\nb",
    "a\x7fb",
    "a\ud800b",
    "a​b",
    "aéb",
    "https://idp.example.test/",
    "y" * 255,
    "y" * 256,
]


def _schema_accepts(field: str, value) -> bool:
    try:
        if field == "subject":
            FederatedIdentityLinkRequest(issuer="x", subject=value)
        elif field == "issuer":
            FederatedIdentityLinkRequest(issuer=value, subject="x")
        elif field == "old_issuer":
            IssuerRewriteRequest(old_issuer=value, new_issuer="x", dry_run=True)
        else:
            IssuerRewriteRequest(old_issuer="x", new_issuer=value, dry_run=True)
    except ValidationError:
        return False
    return True


def _login_accepts(issuer, subject) -> bool:
    login = FederatedLogin(
        issuer=issuer,
        subject=subject,
        email=None,
        email_verified=None,
        username=None,
        full_name=None,
    )
    return _identity_key_refusal(login) is None


@pytest.mark.parametrize("value", IDENTITY_KEY_CASES + [None, 5, ["x"]])
def test_the_routes_accept_exactly_the_identity_keys_a_login_accepts(value):
    assert _schema_accepts("subject", value) is _login_accepts("x", value)
    for field in ("issuer", "old_issuer", "new_issuer"):
        assert _schema_accepts(field, value) is _login_accepts(value, "x")


def test_identity_key_bounds_are_the_column_widths():
    assert ISSUER_MAX_LENGTH == SUBJECT_MAX_LENGTH == 255
    assert identity_key_part_ok("y" * 255, 255) is True
    assert identity_key_part_ok("y" * 256, 255) is False
    assert auth_service._identity_key_refusal is _identity_key_refusal
