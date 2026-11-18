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

Both answer in every `AUTH_METHOD`, so a superadmin can run them from a local
session after switching the mode back, from the OIDC break-glass login, or with
a superadmin API token. Neither changes an account id, so no other service sees
a change.

Audit: every call that passes the role check writes one log line with a fixed
`action`, the caller's id, the outcome, and counts or the target account id;
never an issuer or a subject (the IAM-OIDC-12 rule for claim values), never a
token. A database error is logged by exception class name only, since its text
carries the statement's bound parameters, and answers a fixed 500 detail.
"""

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies.auth import require_role
from app.models.user import Role, User
from app.schemas.federated_identity import (
    FederatedIdentityLinkRequest,
    FederatedIdentityResponse,
    IssuerRewriteRequest,
    IssuerRewriteResponse,
)
from app.services.federated_identity_service import (
    IssuerRewriteConflict,
    RelinkRefused,
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

REWRITE_ACTION = "oidc_issuer_rewrite"
RELINK_ACTION = "oidc_identity_relink"


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


@router.post("/admin/federated-identities/rewrite-issuer", response_model=IssuerRewriteResponse)
async def rewrite_federated_issuer(
    body: IssuerRewriteRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = _superadmin_only,
):
    """Rewrite the issuer of every federated identity under exactly `old_issuer`
    to `new_issuer`, in one transaction. Superadmin only, any auth mode.

    - 400 when the two issuers are the same string.
    - A dry run answers the counts and writes nothing.
    - 409 when any rewritten identity would share an (issuer, subject) with an
      identity already under `new_issuer`; nothing is written, and the detail
      carries the count (a dry run reports it as `conflicts` instead).
    - 200 otherwise, `rewritten` being the rows changed (0 when nothing matched).
    """
    # Read before the service ends its transaction: a rollback expires every
    # instance the session holds, the caller's row included.
    caller_id = str(current_user.id)
    if body.old_issuer == body.new_issuer:
        _audit(logging.INFO, REWRITE_ACTION, "refused_unchanged", caller_id, dry_run=body.dry_run)
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
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=rewrite_conflict_detail(conflict.conflicts),
        ) from None
    except SQLAlchemyError as exc:
        raise _store_failed(REWRITE_ACTION, caller_id, exc, dry_run=body.dry_run) from None
    _audit(
        logging.INFO if body.dry_run else logging.WARNING,
        REWRITE_ACTION,
        "dry_run" if body.dry_run else "rewritten",
        caller_id,
        dry_run=body.dry_run,
        matched=result.matched,
        conflicts=result.conflicts,
        rewritten=result.rewritten,
    )
    return IssuerRewriteResponse(
        dry_run=body.dry_run,
        matched=result.matched,
        conflicts=result.conflicts,
        rewritten=result.rewritten,
    )


@router.put("/users/{user_id}/federated-identity", response_model=FederatedIdentityResponse)
async def relink_federated_identity(
    user_id: uuid.UUID,
    body: FederatedIdentityLinkRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = _superadmin_only,
):
    """Make (issuer, subject) the one federated identity of `oidc` account
    `user_id`, replacing the one it held. Superadmin only, any auth mode.

    - 404 `User not found` for an unknown id.
    - 409 when the account is not an `oidc` account (a local or LDAP account is
      never linked, ADR 0016 Out of scope).
    - 409 when another account holds that (issuer, subject).
    - 200 with the identity; when the account already held exactly that pair
      nothing is written.
    """
    caller_id = str(current_user.id)
    target = str(user_id)
    try:
        result = await relink_identity(db, user_id, issuer=body.issuer, subject=body.subject)
    except RelinkRefused as refused:
        _audit(
            logging.WARNING,
            RELINK_ACTION,
            f"refused_{refused.reason}",
            caller_id,
            user_id=target,
        )
        if refused.reason == "user_missing":
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=USER_NOT_FOUND_DETAIL
            ) from None
        detail = NOT_OIDC_DETAIL if refused.reason == "not_oidc" else IDENTITY_TAKEN_DETAIL
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail) from None
    except SQLAlchemyError as exc:
        raise _store_failed(RELINK_ACTION, caller_id, exc, user_id=target) from None
    _audit(
        logging.WARNING if result.changed else logging.INFO,
        RELINK_ACTION,
        "relinked" if result.changed else "unchanged",
        caller_id,
        user_id=target,
        issuer_changed=result.issuer_changed,
        subject_changed=result.subject_changed,
    )
    identity = result.identity
    return FederatedIdentityResponse(
        user_id=identity.user_id,
        issuer=identity.issuer,
        subject=identity.subject,
        created_at=identity.created_at,
        last_login_at=identity.last_login_at,
    )
