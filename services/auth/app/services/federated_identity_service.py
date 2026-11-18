"""Superadmin recovery of federated identities (issue #1399, ADR 0016 D4 as built).

Two operations, both reached only through superadmin routes
(`app/routers/federated_identities.py`), both in any `AUTH_METHOD`:

- `rewrite_issuer`: every identity whose issuer is EXACTLY `old_issuer` gets
  `new_issuer`, in one UPDATE, keeping its subject, its account, its id, and
  its timestamps. It is all or nothing: when any of those subjects already has
  an identity under `new_issuer`, nothing is written and the count is reported.
  For a planned `OIDC_ISSUER` change whose provider keeps every subject.
- `relink_identity`: one `oidc` account's identity is replaced by the exact
  (issuer, subject) the superadmin names, for a person whose provider reissued
  their subject. The account keeps its id, so no other service sees a change.

What these do NOT do, by design (ADR 0016 D4): neither reads an email, a
username, or any claim, so neither links a login to an account by anything but
an exact (issuer, subject) the superadmin wrote. Neither touches a local or
LDAP account (linking those stays out of scope).

The invariant both keep, whatever the interleaving with logins and with each
other: each account has at most one identity (`uq_federated_identities_user_id`)
and no two accounts share an (issuer, subject)
(`uq_federated_identities_issuer_subject`). The database constraints are the
judge; the read-time checks here only give the refusal its answer. On Postgres
`relink_identity` locks the account's row first, so two relinks of one account
apply one after the other (the last one wins) instead of one failing on the
user_id constraint.

Transactions: every function ends by committing or rolling back the session it
is handed, on every outcome. No function here logs; the routes write the audit
line, and no log line names an issuer or a subject.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.models.federated_identity import FederatedIdentity
from app.models.user import User


@dataclass(frozen=True)
class IssuerRewriteResult:
    """What a rewrite found and did. `matched` counts the identities under the
    old issuer when it was read; `conflicts` counts those whose subject already
    has an identity under the new issuer; `rewritten` is the number of rows the
    UPDATE changed (0 for a dry run)."""

    matched: int
    conflicts: int
    rewritten: int


class IssuerRewriteConflict(Exception):
    """The rewrite would give two accounts one (issuer, subject); nothing was
    written. `conflicts` is the number of colliding subjects counted after the
    rollback (at least 1)."""

    def __init__(self, conflicts: int) -> None:
        super().__init__("issuer rewrite refused: subjects collide under the new issuer")
        self.conflicts = conflicts


class RelinkRefused(Exception):
    """A relink that wrote nothing. `reason` is one of `user_missing`,
    `not_oidc`, `identity_taken`."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"relink refused: {reason}")
        self.reason = reason


@dataclass(frozen=True)
class IdentityView:
    """A federated identity's columns, read before the transaction ended, so a
    caller never touches an expired ORM instance."""

    user_id: uuid.UUID
    issuer: str
    subject: str
    created_at: datetime
    last_login_at: datetime | None

    @classmethod
    def of(cls, row: FederatedIdentity) -> IdentityView:
        return cls(
            user_id=row.user_id,
            issuer=row.issuer,
            subject=row.subject,
            created_at=row.created_at,
            last_login_at=row.last_login_at,
        )


@dataclass(frozen=True)
class RelinkResult:
    """The account's identity after a relink, and whether anything changed
    (False when it already held exactly that pair)."""

    identity: IdentityView
    changed: bool
    issuer_changed: bool
    subject_changed: bool


async def _count_matched(db: AsyncSession, issuer: str) -> int:
    result = await db.execute(
        select(func.count())
        .select_from(FederatedIdentity)
        .where(FederatedIdentity.issuer == issuer)
    )
    return int(result.scalar_one())


async def _count_conflicts(db: AsyncSession, old_issuer: str, new_issuer: str) -> int:
    """Identities under `old_issuer` whose subject already has an identity
    under `new_issuer`: exactly the rows the UPDATE would fail on."""
    taken = aliased(FederatedIdentity)
    result = await db.execute(
        select(func.count())
        .select_from(FederatedIdentity)
        .where(
            FederatedIdentity.issuer == old_issuer,
            select(taken.id)
            .where(taken.issuer == new_issuer, taken.subject == FederatedIdentity.subject)
            .exists(),
        )
    )
    return int(result.scalar_one())


async def _holder_of(db: AsyncSession, issuer: str, subject: str) -> uuid.UUID | None:
    """The account holding (issuer, subject), or None. Read-time only: the
    unique constraint is the judge when a writer takes the pair after this."""
    result = await db.execute(
        select(FederatedIdentity.user_id).where(
            FederatedIdentity.issuer == issuer, FederatedIdentity.subject == subject
        )
    )
    return result.scalar_one_or_none()


async def rewrite_issuer(
    db: AsyncSession, *, old_issuer: str, new_issuer: str, dry_run: bool
) -> IssuerRewriteResult:
    """Move every identity under exactly `old_issuer` to `new_issuer`.

    Issuers are compared as the exact strings stored (and as an ID token
    carries them): no trailing slash, case, or whitespace normalization. The
    caller refuses `old_issuer == new_issuer` and validates both strings.

    A dry run counts and writes nothing. A real run with any conflict writes
    nothing and raises `IssuerRewriteConflict`; otherwise it runs ONE UPDATE and
    commits. When a first login inserts a colliding identity between the count
    and the UPDATE, the unique constraint fails the UPDATE, the transaction is
    rolled back, and the conflict is raised with a fresh count. Ends its
    transaction on every outcome.
    """
    try:
        matched = await _count_matched(db, old_issuer)
        conflicts = await _count_conflicts(db, old_issuer, new_issuer)
    except BaseException:
        await db.rollback()
        raise
    if dry_run:
        await db.rollback()
        return IssuerRewriteResult(matched=matched, conflicts=conflicts, rewritten=0)
    if conflicts:
        await db.rollback()
        raise IssuerRewriteConflict(conflicts)
    try:
        result = await db.execute(
            update(FederatedIdentity)
            .where(FederatedIdentity.issuer == old_issuer)
            .values(issuer=new_issuer)
            .execution_options(synchronize_session=False)
        )
        await db.commit()
    except IntegrityError:
        await db.rollback()
        try:
            recount = await _count_conflicts(db, old_issuer, new_issuer)
        finally:
            await db.rollback()
        raise IssuerRewriteConflict(max(recount, 1)) from None
    except BaseException:
        await db.rollback()
        raise
    return IssuerRewriteResult(matched=matched, conflicts=0, rewritten=int(result.rowcount))


async def relink_identity(
    db: AsyncSession, user_id: uuid.UUID, *, issuer: str, subject: str
) -> RelinkResult:
    """Make (issuer, subject) the one federated identity of `oidc` account
    `user_id`, replacing whatever it held.

    Refuses, writing nothing: an unknown account (`user_missing`); an account
    whose `auth_source` is not `oidc` (`not_oidc`); a pair another account
    holds (`identity_taken`, also when a concurrent writer takes it first and
    the insert fails on the constraint). When the account already holds
    exactly that pair, nothing is written and `changed` is False. Otherwise the
    old row is deleted and a new one inserted in one transaction: a new id, a
    new `created_at`, and no `last_login_at`, since that identity has not
    signed in yet. The account row is locked `FOR UPDATE` first (a no-op on
    SQLite), so concurrent relinks of one account apply in turn. Ends its
    transaction on every outcome; the identity comes back as an
    `IdentityView`, read before the transaction ended.
    """
    try:
        account = (
            await db.execute(
                select(User.id, User.auth_source).where(User.id == user_id).with_for_update()
            )
        ).one_or_none()
        if account is None:
            raise RelinkRefused("user_missing")
        if account.auth_source != "oidc":
            raise RelinkRefused("not_oidc")
        current = (
            await db.execute(select(FederatedIdentity).where(FederatedIdentity.user_id == user_id))
        ).scalar_one_or_none()
        if current is not None and (current.issuer, current.subject) == (issuer, subject):
            unchanged = IdentityView.of(current)
            await db.rollback()
            return RelinkResult(
                identity=unchanged, changed=False, issuer_changed=False, subject_changed=False
            )
        if await _holder_of(db, issuer, subject) is not None:
            raise RelinkRefused("identity_taken")
        issuer_changed = current is None or current.issuer != issuer
        subject_changed = current is None or current.subject != subject
        await db.execute(
            delete(FederatedIdentity)
            .where(FederatedIdentity.user_id == user_id)
            .execution_options(synchronize_session=False)
        )
        replacement = FederatedIdentity(user_id=user_id, issuer=issuer, subject=subject)
        db.add(replacement)
        await db.flush()
        await db.commit()
    except RelinkRefused:
        await db.rollback()
        raise
    except IntegrityError:
        await db.rollback()
        raise RelinkRefused("identity_taken") from None
    except BaseException:
        await db.rollback()
        raise
    try:
        await db.refresh(replacement)
        linked = IdentityView.of(replacement)
    finally:
        await db.rollback()
    return RelinkResult(
        identity=linked,
        changed=True,
        issuer_changed=issuer_changed,
        subject_changed=subject_changed,
    )
