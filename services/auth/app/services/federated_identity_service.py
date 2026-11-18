"""Superadmin recovery of federated identities (issue #1399, ADR 0016 D4 as built).

Reached only through the superadmin routes (`app/routers/federated_identities.py`),
in any `AUTH_METHOD`, except `capture_relink_candidate`, which the login's
refusal path calls:

- `rewrite_issuer`: every identity whose issuer is EXACTLY `old_issuer` gets
  `new_issuer`, in one UPDATE, keeping its subject, its account, its id, and
  its timestamps. It is all or nothing: when any of those subjects already has
  an identity under `new_issuer`, nothing is written and the count and the
  colliding account ids are reported. For a planned `OIDC_ISSUER` change whose
  provider keeps every subject.
- `relink_identity`: one `oidc` account's identity is replaced by an exact
  (issuer, subject), either the pair the superadmin types or the pair of one of
  that account's relink candidates, named by id. The account keeps its id, so
  no other service sees a change. A relink discards the account's candidates.
- `capture_relink_candidate`: when a first login is refused because its email
  belongs to exactly one `oidc` account, the refused pair is kept as a relink
  candidate against that account (owner decision 2026-11-18). A candidate is
  only evidence that someone signed in at the provider with an account carrying
  that email: nothing here links from it, and only a superadmin relink naming
  it does.
- `list_relink_candidates`: an account's live candidates, for the superadmin.

What these do NOT do, by design (ADR 0016 D4): no function links a login to an
account. The rewrite and relink never read an email or a claim; the capture
reads the refused login's email only to find the account to file the evidence
under. None touches a local or LDAP account.

The invariant the writers keep, whatever the interleaving with logins and with
each other: each account has at most one identity
(`uq_federated_identities_user_id`) and no two accounts share an (issuer,
subject) (`uq_federated_identities_issuer_subject`). The database constraints
are the judge; the read-time checks only give the refusal its answer. On
Postgres `relink_identity` locks the account's row first, so two relinks of one
account apply one after the other (the last one wins); captures are serialized
by one transaction-scoped advisory lock, so the caps hold between them.

Transactions: every function ends by committing or rolling back the session it
is handed, on every outcome. No function here logs; the routes and the login
write the log lines, and no log line names an issuer or a subject: they carry
`identity_fingerprint` values instead.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from herd_common.advisory_lock import advisory_key_from_string, xact_lock
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.config import settings
from app.models.federated_identity import FederatedIdentity
from app.models.federated_relink_candidate import (
    CANDIDATE_TTL,
    CANDIDATES_GLOBAL_CAP,
    CANDIDATES_PER_ACCOUNT,
    FederatedRelinkCandidate,
    FederatedRelinkCaptureTotal,
)
from app.models.user import Role, User

# At most this many colliding account pairs are listed by a rewrite's dry run
# or its 409; `collisions_truncated` says when there were more.
COLLISIONS_LIST_CAP = 50

# The keyed fingerprint (owner decision 2026-11-18): HMAC-SHA256 under a key
# derived from AUTH_SECRET_KEY (`settings.secret_key`, the JWT signing key) with
# this fixed context string, so the fingerprint key is never the signing key
# itself and no new setting exists. 16 hex characters (64 bits).
FINGERPRINT_CONTEXT = b"herd-auth/oidc-identity-fingerprint/v1"
FINGERPRINT_HEX_LENGTH = 16

_CAPTURE_LOCK_KEY = advisory_key_from_string("oidc-relink-candidate-capture")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def identity_fingerprint(*parts: str) -> str:
    """The keyed fingerprint of an issuer (one part) or an (issuer, subject)
    pair (two parts), for log lines and the candidate list.

    `HMAC-SHA256(K, json.dumps([parts...]))` truncated to 16 hex characters,
    where `K = HMAC-SHA256(AUTH_SECRET_KEY, FINGERPRINT_CONTEXT)`. The JSON list
    makes the encoding unambiguous and keeps an issuer's fingerprint distinct
    from any pair's. Rotating AUTH_SECRET_KEY changes every fingerprint.
    """
    key = hmac.new(settings.secret_key.encode("utf-8"), FINGERPRINT_CONTEXT, hashlib.sha256)
    message = json.dumps(list(parts)).encode("utf-8")
    digest = hmac.new(key.digest(), message, hashlib.sha256).hexdigest()
    return digest[:FINGERPRINT_HEX_LENGTH]


@dataclass(frozen=True)
class IssuerRewriteResult:
    """What a rewrite found and did. `matched` counts the identities under the
    old issuer when it was read; `conflicts` counts those whose subject already
    has an identity under the new issuer, and `collisions` lists up to
    `COLLISIONS_LIST_CAP` of them as (account under the old issuer, account
    under the new issuer); `rewritten` is the number of rows the UPDATE changed
    (0 for a dry run)."""

    matched: int
    conflicts: int
    rewritten: int
    collisions: tuple[tuple[uuid.UUID, uuid.UUID], ...] = ()
    collisions_truncated: bool = False


class IssuerRewriteConflict(Exception):
    """The rewrite would give two accounts one (issuer, subject); nothing was
    written. `conflicts` is the number of colliding subjects counted after the
    rollback (at least 1), `collisions` the listed pairs."""

    def __init__(
        self,
        conflicts: int,
        collisions: tuple[tuple[uuid.UUID, uuid.UUID], ...] = (),
        collisions_truncated: bool = False,
    ) -> None:
        super().__init__("issuer rewrite refused: subjects collide under the new issuer")
        self.conflicts = conflicts
        self.collisions = collisions
        self.collisions_truncated = collisions_truncated


class RelinkRefused(Exception):
    """A relink that wrote nothing. `reason` is one of `user_missing`,
    `not_oidc`, `identity_taken`, `candidate_missing`."""

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
    (False when it already held exactly that pair). The fingerprints are of the
    pair before (None when the account had no identity) and after."""

    identity: IdentityView
    changed: bool
    issuer_changed: bool
    subject_changed: bool
    old_fingerprint: str | None = None
    new_fingerprint: str | None = None
    candidates_discarded: int = 0


@dataclass(frozen=True)
class CandidateView:
    """A live relink candidate as the superadmin sees it: never the subject,
    only the pair's fingerprint, enough to tell candidates apart and to compare
    with a pair read at the provider or a fingerprint in an audit line."""

    id: uuid.UUID
    issuer: str
    fingerprint: str
    created_at: datetime
    last_seen_at: datetime
    expires_at: datetime
    attempts: int


@dataclass(frozen=True)
class CandidateList:
    """An account's live candidates, newest first, and how many distinct pairs
    were ever captured for it (live, expired, or evicted)."""

    candidates: tuple[CandidateView, ...]
    captured_total: int


@dataclass(frozen=True)
class CaptureOutcome:
    """What a capture did: the candidate's id, whether the pair was new for the
    account (False when a repeat bumped `attempts`), and how many rows the caps
    and the expiry removed."""

    candidate_id: uuid.UUID
    user_id: uuid.UUID
    new: bool
    evicted: int


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


async def _list_collisions(
    db: AsyncSession, old_issuer: str, new_issuer: str
) -> tuple[tuple[uuid.UUID, uuid.UUID], ...]:
    """Up to COLLISIONS_LIST_CAP + 1 colliding pairs of account ids, (holder
    under the old issuer, holder under the new issuer), in a stable order."""
    taken = aliased(FederatedIdentity)
    result = await db.execute(
        select(FederatedIdentity.user_id, taken.user_id)
        .join(
            taken,
            (taken.subject == FederatedIdentity.subject) & (taken.issuer == new_issuer),
        )
        .where(FederatedIdentity.issuer == old_issuer)
        .order_by(FederatedIdentity.user_id, taken.user_id)
        .limit(COLLISIONS_LIST_CAP + 1)
    )
    return tuple((row[0], row[1]) for row in result.all())


def _capped(
    pairs: tuple[tuple[uuid.UUID, uuid.UUID], ...],
) -> tuple[tuple[tuple[uuid.UUID, uuid.UUID], ...], bool]:
    return pairs[:COLLISIONS_LIST_CAP], len(pairs) > COLLISIONS_LIST_CAP


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

    A dry run counts, lists the colliding account pairs, and writes nothing. A
    real run with any conflict writes nothing and raises
    `IssuerRewriteConflict` with the count and the pairs; otherwise it runs ONE
    UPDATE and commits. When a first login inserts a colliding identity between
    the count and the UPDATE, the unique constraint fails the UPDATE, the
    transaction is rolled back, and the conflict is raised with a fresh count
    and list. Ends its transaction on every outcome.
    """
    try:
        matched = await _count_matched(db, old_issuer)
        conflicts = await _count_conflicts(db, old_issuer, new_issuer)
        collisions, truncated = _capped(
            await _list_collisions(db, old_issuer, new_issuer) if conflicts else ()
        )
    except BaseException:
        await db.rollback()
        raise
    if dry_run:
        await db.rollback()
        return IssuerRewriteResult(
            matched=matched,
            conflicts=conflicts,
            rewritten=0,
            collisions=collisions,
            collisions_truncated=truncated,
        )
    if conflicts:
        await db.rollback()
        raise IssuerRewriteConflict(conflicts, collisions, truncated)
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
            collisions, truncated = _capped(await _list_collisions(db, old_issuer, new_issuer))
        finally:
            await db.rollback()
        raise IssuerRewriteConflict(max(recount, 1), collisions, truncated) from None
    except BaseException:
        await db.rollback()
        raise
    return IssuerRewriteResult(matched=matched, conflicts=0, rewritten=int(result.rowcount))


async def relink_identity(
    db: AsyncSession,
    user_id: uuid.UUID,
    *,
    issuer: str | None = None,
    subject: str | None = None,
    candidate_id: uuid.UUID | None = None,
) -> RelinkResult:
    """Make one exact (issuer, subject) the one federated identity of `oidc`
    account `user_id`, replacing whatever it held. The pair is either typed
    (`issuer` and `subject`) or the pair of the account's live candidate
    `candidate_id`; exactly one of the two forms (the request schema enforces
    it; a caller passing both or neither gets a ValueError).

    Refuses, writing nothing: an unknown account (`user_missing`); an account
    whose `auth_source` is not `oidc` (`not_oidc`); a candidate id that is not a
    live candidate of THIS account (`candidate_missing`, the same answer for an
    unknown id, an expired one, and another account's); a pair another account
    holds (`identity_taken`, also when a concurrent writer takes it first and
    the insert fails on the constraint). When the account already holds
    exactly that pair, nothing is written and `changed` is False. Otherwise the
    old row is deleted, a new one inserted (a new id, a new `created_at`, no
    `last_login_at`), and every candidate of the account deleted (the one used
    included), in one transaction. The account row is locked `FOR UPDATE`
    first (a no-op on SQLite), so concurrent relinks of one account apply in
    turn. Ends its transaction on every outcome.
    """
    typed = issuer is not None or subject is not None
    if typed == (candidate_id is not None) or (typed and (issuer is None or subject is None)):
        raise ValueError("relink takes either issuer and subject, or candidate_id")
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
        if candidate_id is not None:
            candidate = (
                await db.execute(
                    select(FederatedRelinkCandidate.issuer, FederatedRelinkCandidate.subject).where(
                        FederatedRelinkCandidate.id == candidate_id,
                        FederatedRelinkCandidate.user_id == user_id,
                        FederatedRelinkCandidate.expires_at > _utcnow(),
                    )
                )
            ).one_or_none()
            if candidate is None:
                raise RelinkRefused("candidate_missing")
            issuer, subject = candidate.issuer, candidate.subject
        current = (
            await db.execute(select(FederatedIdentity).where(FederatedIdentity.user_id == user_id))
        ).scalar_one_or_none()
        old_fingerprint = (
            identity_fingerprint(current.issuer, current.subject) if current is not None else None
        )
        new_fingerprint = identity_fingerprint(issuer, subject)
        if current is not None and (current.issuer, current.subject) == (issuer, subject):
            unchanged = IdentityView.of(current)
            await db.rollback()
            return RelinkResult(
                identity=unchanged,
                changed=False,
                issuer_changed=False,
                subject_changed=False,
                old_fingerprint=old_fingerprint,
                new_fingerprint=new_fingerprint,
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
        discarded = await db.execute(
            delete(FederatedRelinkCandidate)
            .where(FederatedRelinkCandidate.user_id == user_id)
            .execution_options(synchronize_session=False)
        )
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
        old_fingerprint=old_fingerprint,
        new_fingerprint=new_fingerprint,
        candidates_discarded=int(discarded.rowcount),
    )


async def _oidc_holder_of_email(db: AsyncSession, email: str) -> uuid.UUID | None:
    """The one account whose email equals `email` case-insensitively, when it
    is an `oidc` account that is not a superadmin; else None (no holder, more
    than one, a local or LDAP holder, or a superadmin)."""
    rows = (
        await db.execute(
            select(User.id, User.auth_source, User.role)
            .where(func.lower(User.email) == func.lower(email))
            .limit(2)
        )
    ).all()
    if len(rows) != 1:
        return None
    holder = rows[0]
    if holder.auth_source != "oidc" or holder.role == Role.SUPERADMIN:
        return None
    return holder.id


async def capture_relink_candidate(
    db: AsyncSession, *, email: str, issuer: str, subject: str
) -> CaptureOutcome | None:
    """Keep a refused login's (issuer, subject) as a relink candidate against
    the `oidc` account that holds its email; None when no such account exists.

    Called by `resolve_federated_user` after it rolled back an `email_taken`
    refusal, in a transaction of its own; the caller catches every exception, so
    a failure here never changes the refusal. Under one transaction-scoped
    advisory lock (a no-op on SQLite), in order: expired candidates are
    deleted; a pair the account already has is a repeat (`attempts` and
    `last_seen_at` move, the expiry does not); a new pair is inserted with a
    24-hour life and the account's `captured_total` goes up by one; then the
    account's oldest candidates beyond `CANDIDATES_PER_ACCOUNT`, and the oldest
    in the table beyond `CANDIDATES_GLOBAL_CAP`, are deleted. Commits.
    """
    try:
        await xact_lock(db, _CAPTURE_LOCK_KEY)
        holder = await _oidc_holder_of_email(db, email)
        if holder is None:
            await db.rollback()
            return None
        now = _utcnow()
        expired = await db.execute(
            delete(FederatedRelinkCandidate)
            .where(FederatedRelinkCandidate.expires_at <= now)
            .execution_options(synchronize_session=False)
        )
        evicted = int(expired.rowcount)
        existing = (
            await db.execute(
                select(FederatedRelinkCandidate.id).where(
                    FederatedRelinkCandidate.user_id == holder,
                    FederatedRelinkCandidate.issuer == issuer,
                    FederatedRelinkCandidate.subject == subject,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            await db.execute(
                update(FederatedRelinkCandidate)
                .where(FederatedRelinkCandidate.id == existing)
                .values(
                    attempts=FederatedRelinkCandidate.attempts + 1,
                    last_seen_at=now,
                )
                .execution_options(synchronize_session=False)
            )
            await db.commit()
            return CaptureOutcome(candidate_id=existing, user_id=holder, new=False, evicted=evicted)
        candidate_id = uuid.uuid4()
        db.add(
            FederatedRelinkCandidate(
                id=candidate_id,
                user_id=holder,
                issuer=issuer,
                subject=subject,
                created_at=now,
                last_seen_at=now,
                expires_at=now + CANDIDATE_TTL,
                attempts=1,
            )
        )
        await db.flush()
        bumped = await db.execute(
            update(FederatedRelinkCaptureTotal)
            .where(FederatedRelinkCaptureTotal.user_id == holder)
            .values(
                captured_total=FederatedRelinkCaptureTotal.captured_total + 1,
                last_captured_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        if bumped.rowcount == 0:
            db.add(
                FederatedRelinkCaptureTotal(user_id=holder, captured_total=1, last_captured_at=now)
            )
            await db.flush()
        evicted += await _evict_beyond(
            db,
            select(FederatedRelinkCandidate.id).where(FederatedRelinkCandidate.user_id == holder),
            CANDIDATES_PER_ACCOUNT,
        )
        evicted += await _evict_beyond(
            db, select(FederatedRelinkCandidate.id), CANDIDATES_GLOBAL_CAP
        )
        await db.commit()
    except BaseException:
        await db.rollback()
        raise
    return CaptureOutcome(candidate_id=candidate_id, user_id=holder, new=True, evicted=evicted)


async def _evict_beyond(db: AsyncSession, ids_query, keep: int) -> int:
    """Delete every row of `ids_query` but the newest `keep` (by created_at,
    then id); the number deleted."""
    doomed = (
        (
            await db.execute(
                ids_query.order_by(
                    FederatedRelinkCandidate.created_at.desc(), FederatedRelinkCandidate.id.desc()
                ).offset(keep)
            )
        )
        .scalars()
        .all()
    )
    if not doomed:
        return 0
    await db.execute(
        delete(FederatedRelinkCandidate)
        .where(FederatedRelinkCandidate.id.in_(doomed))
        .execution_options(synchronize_session=False)
    )
    return len(doomed)


async def list_relink_candidates(db: AsyncSession, user_id: uuid.UUID) -> CandidateList:
    """The live candidates of `oidc` account `user_id`, newest first, and its
    `captured_total`. Raises RelinkRefused `user_missing` or `not_oidc`, as the
    relink does. Reads only; ends its transaction."""
    try:
        account = (
            await db.execute(select(User.id, User.auth_source).where(User.id == user_id))
        ).one_or_none()
        if account is None:
            raise RelinkRefused("user_missing")
        if account.auth_source != "oidc":
            raise RelinkRefused("not_oidc")
        rows = (
            await db.execute(
                select(FederatedRelinkCandidate)
                .where(
                    FederatedRelinkCandidate.user_id == user_id,
                    FederatedRelinkCandidate.expires_at > _utcnow(),
                )
                .order_by(
                    FederatedRelinkCandidate.created_at.desc(), FederatedRelinkCandidate.id.desc()
                )
            )
        ).scalars()
        views = tuple(
            CandidateView(
                id=r.id,
                issuer=r.issuer,
                fingerprint=identity_fingerprint(r.issuer, r.subject),
                created_at=r.created_at,
                last_seen_at=r.last_seen_at,
                expires_at=r.expires_at,
                attempts=r.attempts,
            )
            for r in rows
        )
        total = (
            await db.execute(
                select(FederatedRelinkCaptureTotal.captured_total).where(
                    FederatedRelinkCaptureTotal.user_id == user_id
                )
            )
        ).scalar_one_or_none()
    finally:
        await db.rollback()
    return CandidateList(candidates=views, captured_total=int(total or 0))
