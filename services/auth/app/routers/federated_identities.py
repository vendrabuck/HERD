"""Superadmin recovery routes for single sign-on identities (issue #1399).

The owner's decision of 2026-11-18 (option (a) on the issue; ADR 0016 D4 as
built): two superadmin-only routes, API only.

- `POST /admin/federated-identities/rewrite-issuer` moves every identity under
  one exact issuer string to another, for a planned `OIDC_ISSUER` change whose
  provider keeps each person's subject.
- `PUT /users/{user_id}/federated-identity` replaces one `oidc` account's
  identity with the exact (issuer, subject) the superadmin names, so that
  person's next sign-in, whose provider reissued their subject, signs in to the
  account they already have. This is the "unlink so the next login links
  afresh" of the decision, done in one step: an unlink alone would leave the
  next login a first login whose email the account still holds, which
  IAM-OIDC-10 refuses, and attaching that login by its email is the
  link-by-email D4 forbids. The superadmin names both the account and the
  identity, so no claim the provider sends can choose an account.

A third route, `GET /users/{user_id}/federated-identity/candidates`, lists the
relink candidates the login's refusal path captured for an account (owner
decision 2026-11-18), so the relink can name a candidate by id on a provider
that never shows `sub`.

All three answer in every `AUTH_METHOD`, so a superadmin can run them from a
local session after switching the mode back, from the OIDC break-glass login,
or with a superadmin API token. None changes an account id, so no other service
sees a change.

Audit: every call that passes the role check writes one log line with a fixed
`action`, the caller's id, the outcome, and counts or the target account id,
plus keyed fingerprints of the issuers or pairs involved
(`federated_identity_service.identity_fingerprint`); never an issuer or a
subject (the IAM-OIDC-12 rule for claim values), never a token. A database
error is logged by exception class name only, since its text carries the
statement's bound parameters, and answers a fixed 500 detail.
"""

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies.auth import require_role
from app.models.user import Role, User
from app.schemas.federated_identity import (
    CollisionPair,
    FederatedIdentityLinkRequest,
    FederatedIdentityResponse,
    IssuerRewriteConflictResponse,
    IssuerRewriteRequest,
    IssuerRewriteResponse,
    RelinkCandidateListResponse,
    RelinkCandidateResponse,
)
from app.services.federated_identity_service import (
    IssuerRewriteConflict,
    RelinkRefused,
    identity_fingerprint,
    list_relink_candidates,
    relink_identity,
    rewrite_issuer,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["admin"])

_superadmin_only = Depends(require_role(Role.SUPERADMIN))

ISSUER_UNCHANGED_DETAIL = "The new issuer equals the old issuer"
USER_NOT_FOUND_DETAIL = "User not found"
NOT_OIDC_DETAIL = "Only a single sign-on account can be linked to an identity"
IDENTITY_TAKEN_DETAIL = "This identity is already linked to another account"
STORE_FAILED_DETAIL = "Federated identity change failed"
CANDIDATE_NOT_FOUND_DETAIL = "Relink candidate not found for this account"

REWRITE_ACTION = "oidc_issuer_rewrite"
RELINK_ACTION = "oidc_identity_relink"
CANDIDATES_READ_ACTION = "oidc_relink_candidates_read"


def rewrite_conflict_detail(conflicts: int) -> str:
    noun = "identity" if conflicts == 1 else "identities"
    return (
        f"Rewrite refused: {conflicts} federated {noun} would collide with an identity "
        "already under the new issuer; nothing was changed"
    )


def _audit(level: int, action: str, outcome: str, caller_id: str, **fields) -> None:
    extra = {"action": action, "outcome": outcome, "caller_id": caller_id, **fields}
    logger.log(level, "%s: %s", action, outcome, extra=extra)


def _store_failed(action: str, caller_id: str, exc: SQLAlchemyError, **fields) -> HTTPException:
    _audit(logging.ERROR, action, "error", caller_id, exception_class=type(exc).__name__, **fields)
    return HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=STORE_FAILED_DETAIL
    )


def _collision_pairs(pairs) -> list[CollisionPair]:
    return [
        CollisionPair(old_issuer_account_id=old, new_issuer_account_id=new) for old, new in pairs
    ]


@router.post(
    "/admin/federated-identities/rewrite-issuer",
    response_model=IssuerRewriteResponse,
    responses={409: {"model": IssuerRewriteConflictResponse}},
)
async def rewrite_federated_issuer(
    body: IssuerRewriteRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = _superadmin_only,
):
    """Rewrite the issuer of every federated identity under exactly `old_issuer`
    to `new_issuer`, in one transaction. Superadmin only, any auth mode.

    - 400 when the two issuers are the same string.
    - A dry run answers the counts and the colliding account pairs and writes
      nothing.
    - 409 when any rewritten identity would share an (issuer, subject) with an
      identity already under `new_issuer`; nothing is written. The body is
      `IssuerRewriteConflictResponse`: the fixed `detail` string plus the count
      and the colliding account pairs (at most 50, `collisions_truncated` past
      that).
    - 200 otherwise, `rewritten` being the rows changed (0 when nothing matched).

    The audit line carries the two issuers' fingerprints, never the issuers.
    """
    # Read before the service ends its transaction: a rollback expires every
    # instance the session holds, the caller's row included.
    caller_id = str(current_user.id)
    fingerprints = {
        "old_issuer_fingerprint": identity_fingerprint(body.old_issuer),
        "new_issuer_fingerprint": identity_fingerprint(body.new_issuer),
    }
    if body.old_issuer == body.new_issuer:
        _audit(
            logging.INFO,
            REWRITE_ACTION,
            "refused_unchanged",
            caller_id,
            dry_run=body.dry_run,
            **fingerprints,
        )
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=ISSUER_UNCHANGED_DETAIL)
    try:
        result = await rewrite_issuer(
            db, old_issuer=body.old_issuer, new_issuer=body.new_issuer, dry_run=body.dry_run
        )
    except IssuerRewriteConflict as conflict:
        _audit(
            logging.WARNING,
            REWRITE_ACTION,
            "refused_conflict",
            caller_id,
            dry_run=False,
            conflicts=conflict.conflicts,
            **fingerprints,
        )
        refusal = IssuerRewriteConflictResponse(
            detail=rewrite_conflict_detail(conflict.conflicts),
            conflicts=conflict.conflicts,
            collisions=_collision_pairs(conflict.collisions),
            collisions_truncated=conflict.collisions_truncated,
        )
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT, content=refusal.model_dump(mode="json")
        )
    except SQLAlchemyError as exc:
        raise _store_failed(
            REWRITE_ACTION, caller_id, exc, dry_run=body.dry_run, **fingerprints
        ) from None
    _audit(
        logging.INFO if body.dry_run else logging.WARNING,
        REWRITE_ACTION,
        "dry_run" if body.dry_run else "rewritten",
        caller_id,
        dry_run=body.dry_run,
        matched=result.matched,
        conflicts=result.conflicts,
        rewritten=result.rewritten,
        **fingerprints,
    )
    return IssuerRewriteResponse(
        dry_run=body.dry_run,
        matched=result.matched,
        conflicts=result.conflicts,
        rewritten=result.rewritten,
        collisions=_collision_pairs(result.collisions),
        collisions_truncated=result.collisions_truncated,
    )


def _refusal(refused: RelinkRefused) -> HTTPException:
    if refused.reason == "user_missing":
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=USER_NOT_FOUND_DETAIL)
    if refused.reason == "candidate_missing":
        return HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=CANDIDATE_NOT_FOUND_DETAIL
        )
    detail = NOT_OIDC_DETAIL if refused.reason == "not_oidc" else IDENTITY_TAKEN_DETAIL
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)


@router.put("/users/{user_id}/federated-identity", response_model=FederatedIdentityResponse)
async def relink_federated_identity(
    user_id: uuid.UUID,
    body: FederatedIdentityLinkRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = _superadmin_only,
):
    """Make one exact (issuer, subject) the one federated identity of `oidc`
    account `user_id`, replacing the one it held. Superadmin only, any auth
    mode. The pair is typed (`issuer`, `subject`) or is that of one of the
    account's live relink candidates (`candidate_id`); exactly one form (422
    otherwise). A relink discards every candidate of the account.

    - 404 `User not found` for an unknown id.
    - 404 `Relink candidate not found for this account` for a candidate id that
      is unknown, expired, or another account's.
    - 409 when the account is not an `oidc` account (a local or LDAP account is
      never linked, ADR 0016 Out of scope).
    - 409 when another account holds that (issuer, subject).
    - 200 with the identity; when the account already held exactly that pair
      nothing is written.

    The audit line carries the fingerprints of the old and new pairs, never the
    pairs.
    """
    caller_id = str(current_user.id)
    target = str(user_id)
    via = "candidate" if body.candidate_id is not None else "pair"
    candidate = str(body.candidate_id) if body.candidate_id is not None else None
    try:
        result = await relink_identity(
            db,
            user_id,
            issuer=body.issuer,
            subject=body.subject,
            candidate_id=body.candidate_id,
        )
    except RelinkRefused as refused:
        _audit(
            logging.WARNING,
            RELINK_ACTION,
            f"refused_{refused.reason}",
            caller_id,
            user_id=target,
            via=via,
            candidate_id=candidate,
        )
        raise _refusal(refused) from None
    except SQLAlchemyError as exc:
        raise _store_failed(RELINK_ACTION, caller_id, exc, user_id=target, via=via) from None
    _audit(
        logging.WARNING if result.changed else logging.INFO,
        RELINK_ACTION,
        "relinked" if result.changed else "unchanged",
        caller_id,
        user_id=target,
        via=via,
        candidate_id=candidate,
        issuer_changed=result.issuer_changed,
        subject_changed=result.subject_changed,
        old_fingerprint=result.old_fingerprint,
        new_fingerprint=result.new_fingerprint,
        candidates_discarded=result.candidates_discarded,
    )
    identity = result.identity
    return FederatedIdentityResponse(
        user_id=identity.user_id,
        issuer=identity.issuer,
        subject=identity.subject,
        created_at=identity.created_at,
        last_login_at=identity.last_login_at,
    )


@router.get(
    "/users/{user_id}/federated-identity/candidates",
    response_model=RelinkCandidateListResponse,
)
async def list_federated_relink_candidates(
    user_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = _superadmin_only,
):
    """The live relink candidates of `oidc` account `user_id`: the identities
    of single sign-on logins refused because their email is this account's
    (issue #1399). Superadmin only, any auth mode. Newest first, each with its
    id, issuer, the fingerprint of its pair (never the subject), when it was
    first and last seen, when it expires, and how many attempts presented it;
    plus `captured_total`. A candidate is evidence that SOMEONE signed in at the
    provider with an account carrying this email, not that it is the person:
    confirm out of band before a relink names one (`docs/OPERATIONS.md`).

    - 404 `User not found` for an unknown id.
    - 409 `Only a single sign-on account can be linked to an identity` for a
      local or LDAP account.
    """
    caller_id = str(current_user.id)
    target = str(user_id)
    try:
        listing = await list_relink_candidates(db, user_id)
    except RelinkRefused as refused:
        _audit(
            logging.INFO,
            CANDIDATES_READ_ACTION,
            f"refused_{refused.reason}",
            caller_id,
            user_id=target,
        )
        raise _refusal(refused) from None
    except SQLAlchemyError as exc:
        raise _store_failed(CANDIDATES_READ_ACTION, caller_id, exc, user_id=target) from None
    _audit(
        logging.INFO,
        CANDIDATES_READ_ACTION,
        "listed",
        caller_id,
        user_id=target,
        live=len(listing.candidates),
        captured_total=listing.captured_total,
    )
    return RelinkCandidateListResponse(
        user_id=user_id,
        captured_total=listing.captured_total,
        candidates=[
            RelinkCandidateResponse(
                id=c.id,
                issuer=c.issuer,
                fingerprint=c.fingerprint,
                created_at=c.created_at,
                last_seen_at=c.last_seen_at,
                expires_at=c.expires_at,
                attempts=c.attempts,
            )
            for c in listing.candidates
        ],
    )
