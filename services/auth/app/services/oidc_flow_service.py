"""The OIDC login flow store (ADR 0016 phase 2, D2 and D11).

Each sign-in attempt is one `oidc_login_flows` row whose status moves forward
only, every edge one guarded UPDATE whose WHERE names the expected current
status (the `rotate_refresh_token` precedent):

- (none) to pending: `start_flow`, an insert after pruning.
- pending to claimed: `claim_flow`; state hash, binding hash,
  `status='pending'`, `expires_at > now`.
- claimed to authenticated: `complete_flow`; `status='claimed'`,
  `expires_at > now`.
- claimed to failed: `fail_flow`; `status='claimed'`.
- authenticated to exchanged: `exchange_handoff`; handoff hash,
  `status='authenticated'`, `handoff_expires_at > now`, the user active.

The loser of any race matches zero rows, writes nothing, and gets None or
False. Raw secrets are passed in by the caller; only their SHA-256 hashes
(`hash_token`) are stored, except the nonce, which the callback compares
verbatim to the ID token's claim, and the PKCE verifier, which the callback
must send to the token endpoint and which the claim sets to NULL.

Nothing here logs a secret, the nonce, or the verifier (D9): the flow id is the
correlation key. No route calls this module yet; phase 3 adds them.
"""

from __future__ import annotations

import hmac
import logging
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from sqlalchemy import delete, exists, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.oidc_login_flow import (
    FLOW_AUTHENTICATED,
    FLOW_CLAIMED,
    FLOW_EXCHANGED,
    FLOW_FAILED,
    FLOW_PENDING,
    OidcLoginFlow,
)
from app.models.user import User
from app.utils.jwt import hash_token

logger = logging.getLogger(__name__)

# Fixed in code, not settings (ADR 0016, Settings).
FLOW_TTL = timedelta(seconds=600)
HANDOFF_TTL = timedelta(seconds=60)
# A row is prunable once its expires_at is MORE than this far in the past.
PRUNE_AFTER = timedelta(hours=1)
PRUNE_BATCH = 500
NEXT_PATH_MAX_LENGTH = 512
DEFAULT_NEXT_PATH = "/topology"
# RFC 7636 section 4.1.
CODE_VERIFIER_MIN_LENGTH = 43
CODE_VERIFIER_MAX_LENGTH = 128


def _utcnow() -> datetime:
    """The flow store's clock; tests replace it to pin the expiry boundaries."""
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class ClaimedFlow:
    """What the callback needs from a row it has just claimed."""

    id: uuid.UUID
    nonce: str
    code_verifier: str
    next_path: str


def sanitize_next_path(raw: str | None) -> str:
    """Return `raw` when it is a safe same-origin path, else `/topology` (D11).

    Accepted only when it is at most 512 characters, starts with exactly one
    `/` (not `//`), contains no backslash and no control character, and
    `urlsplit` finds no scheme and no netloc. Anything else is replaced
    silently, never refused.
    """
    if not raw or len(raw) > NEXT_PATH_MAX_LENGTH:
        return DEFAULT_NEXT_PATH
    if not raw.startswith("/") or raw.startswith("//"):
        return DEFAULT_NEXT_PATH
    if "\\" in raw:
        return DEFAULT_NEXT_PATH
    if any(unicodedata.category(ch) == "Cc" for ch in raw):
        return DEFAULT_NEXT_PATH
    parts = urlsplit(raw)
    if parts.scheme or parts.netloc:
        return DEFAULT_NEXT_PATH
    return raw


async def _prune_expired(db: AsyncSession, now: datetime) -> None:
    """Delete, in its own transaction, at most PRUNE_BATCH rows whose expires_at
    is more than PRUNE_AFTER in the past, whatever their status.

    No live row qualifies: a pending row is claimable only before expires_at,
    `complete_flow` refuses after it, so a handoff expires at most HANDOFF_TTL
    after it, and PRUNE_AFTER is far longer than HANDOFF_TTL.
    """
    cutoff = now - PRUNE_AFTER
    batch = (
        select(OidcLoginFlow.id)
        .where(OidcLoginFlow.expires_at < cutoff)
        .limit(PRUNE_BATCH)
        .scalar_subquery()
    )
    await db.execute(
        delete(OidcLoginFlow)
        .where(OidcLoginFlow.id.in_(batch))
        .execution_options(synchronize_session=False)
    )
    await db.commit()


async def start_flow(
    db: AsyncSession,
    *,
    state: str,
    nonce: str,
    code_verifier: str,
    binding_secret: str,
    next_path: str | None,
) -> uuid.UUID:
    """Prune, then insert a `pending` row for a new sign-in; return its id.

    `state` and `binding_secret` are stored as hashes, `nonce` and
    `code_verifier` as given, `next_path` after `sanitize_next_path`. Raises
    ValueError (naming no value) for an empty secret or a verifier outside the
    RFC 7636 length range, so a caller bug cannot store a guessable row.
    """
    if not state or not nonce or not binding_secret:
        raise ValueError("state, nonce, and binding_secret must be non-empty")
    if not CODE_VERIFIER_MIN_LENGTH <= len(code_verifier) <= CODE_VERIFIER_MAX_LENGTH:
        raise ValueError("code_verifier must be 43 to 128 characters")

    now = _utcnow()
    await _prune_expired(db, now)

    flow = OidcLoginFlow(
        state_hash=hash_token(state),
        binding_hash=hash_token(binding_secret),
        nonce=nonce,
        code_verifier=code_verifier,
        next_path=sanitize_next_path(next_path),
        status=FLOW_PENDING,
        expires_at=now + FLOW_TTL,
    )
    db.add(flow)
    await db.commit()
    return flow.id


async def claim_flow(db: AsyncSession, *, state: str, binding_secret: str) -> ClaimedFlow | None:
    """Move the row for `state` from pending to claimed, or return None.

    The read below only finds the row and compares the binding hash in
    constant time (D11); it decides nothing on its own. The UPDATE is the
    judge: it re-asserts both hashes, `status='pending'`, and `expires_at >
    now`, and sets `code_verifier` to NULL, so a claimed row no longer holds a
    usable verifier. A wrong binding secret writes nothing: the row stays
    pending, exactly as a callback that never arrived leaves it.
    """
    state_hash = hash_token(state)
    binding_hash = hash_token(binding_secret)
    row = (
        await db.execute(
            select(
                OidcLoginFlow.id,
                OidcLoginFlow.binding_hash,
                OidcLoginFlow.nonce,
                OidcLoginFlow.code_verifier,
                OidcLoginFlow.next_path,
            ).where(OidcLoginFlow.state_hash == state_hash)
        )
    ).one_or_none()
    if row is None:
        await db.rollback()
        logger.warning("OIDC flow claim refused", extra={"reason": "unknown_state"})
        return None
    if not hmac.compare_digest(row.binding_hash, binding_hash):
        await db.rollback()
        logger.warning(
            "OIDC flow claim refused",
            extra={"reason": "binding_mismatch", "flow_id": str(row.id)},
        )
        return None

    result = await db.execute(
        update(OidcLoginFlow)
        .where(
            OidcLoginFlow.id == row.id,
            OidcLoginFlow.state_hash == state_hash,
            OidcLoginFlow.binding_hash == binding_hash,
            OidcLoginFlow.status == FLOW_PENDING,
            OidcLoginFlow.expires_at > _utcnow(),
        )
        .values(status=FLOW_CLAIMED, code_verifier=None)
        .execution_options(synchronize_session=False)
    )
    # The status predicate alone decides. A row that matched was pending when
    # the read saw it (nothing returns a row to pending), so the verifier read
    # there is the one start_flow stored.
    if result.rowcount != 1:
        await db.rollback()
        logger.warning(
            "OIDC flow claim refused",
            extra={"reason": "not_claimable", "flow_id": str(row.id)},
        )
        return None
    await db.commit()
    return ClaimedFlow(
        id=row.id,
        nonce=row.nonce,
        code_verifier=row.code_verifier,
        next_path=row.next_path,
    )


async def complete_flow(
    db: AsyncSession, flow_id: uuid.UUID, *, user_id: uuid.UUID, handoff_secret: str
) -> bool:
    """Move a claimed row to authenticated with its user and handoff; True when
    the guarded UPDATE matched.

    Re-asserts `expires_at > now` as well as `status='claimed'`, so no handoff
    is born after the flow's own lifetime and the prune rule can never reach a
    live handoff. The handoff expires HANDOFF_TTL from now.
    """
    if not handoff_secret:
        raise ValueError("handoff_secret must be non-empty")
    now = _utcnow()
    try:
        result = await db.execute(
            update(OidcLoginFlow)
            .where(
                OidcLoginFlow.id == flow_id,
                OidcLoginFlow.status == FLOW_CLAIMED,
                OidcLoginFlow.expires_at > now,
            )
            .values(
                status=FLOW_AUTHENTICATED,
                user_id=user_id,
                handoff_hash=hash_token(handoff_secret),
                handoff_expires_at=now + HANDOFF_TTL,
            )
            .execution_options(synchronize_session=False)
        )
    except IntegrityError as exc:
        # A handoff hash equal to another row's (the unique constraint) or an
        # unknown user (the foreign key): nothing is written either way.
        await db.rollback()
        logger.warning(
            "OIDC flow completion refused",
            extra={
                "reason": "constraint",
                "flow_id": str(flow_id),
                "error_class": type(exc).__name__,
            },
        )
        return False
    if result.rowcount != 1:
        await db.rollback()
        return False
    await db.commit()
    return True


async def fail_flow(db: AsyncSession, flow_id: uuid.UUID) -> bool:
    """Move a claimed row to failed; True when the guarded UPDATE matched.

    Only a claimed row can fail (ADR 0016, D2): a pending row a callback never
    claimed simply expires, and a finished row keeps its outcome. No expiry
    guard, so a failure is always recordable.
    """
    result = await db.execute(
        update(OidcLoginFlow)
        .where(OidcLoginFlow.id == flow_id, OidcLoginFlow.status == FLOW_CLAIMED)
        .values(status=FLOW_FAILED, code_verifier=None)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        await db.rollback()
        return False
    await db.commit()
    return True


async def exchange_handoff(
    db: AsyncSession, *, handoff_secret: str
) -> tuple[uuid.UUID, str] | None:
    """Consume a handoff once: (user id, next path), or None.

    One UPDATE from authenticated to exchanged whose WHERE re-asserts the
    handoff hash, the status, `handoff_expires_at > now`, and that the row's
    user is active (a correlated subquery, the `rotate_refresh_token` form), so
    a user deactivated after the callback gets nothing.
    """
    handoff_hash = hash_token(handoff_secret)
    user_is_active = exists(
        select(User.id).where(User.id == OidcLoginFlow.user_id, User.is_active.is_(True))
    ).correlate(OidcLoginFlow)
    result = await db.execute(
        update(OidcLoginFlow)
        .where(
            OidcLoginFlow.handoff_hash == handoff_hash,
            OidcLoginFlow.status == FLOW_AUTHENTICATED,
            OidcLoginFlow.handoff_expires_at > _utcnow(),
            user_is_active,
        )
        .values(status=FLOW_EXCHANGED)
        .returning(OidcLoginFlow.id, OidcLoginFlow.user_id, OidcLoginFlow.next_path)
        .execution_options(synchronize_session=False)
    )
    rows = result.all()
    if len(rows) != 1:
        await db.rollback()
        logger.warning("OIDC handoff exchange refused", extra={"reason": "not_exchangeable"})
        return None
    await db.commit()
    flow_id, user_id, next_path = rows[0]
    logger.info("OIDC handoff exchanged", extra={"flow_id": str(flow_id)})
    return user_id, next_path


__all__ = [
    "ClaimedFlow",
    "DEFAULT_NEXT_PATH",
    "FLOW_AUTHENTICATED",
    "FLOW_CLAIMED",
    "FLOW_EXCHANGED",
    "FLOW_FAILED",
    "FLOW_PENDING",
    "claim_flow",
    "complete_flow",
    "exchange_handoff",
    "fail_flow",
    "sanitize_next_path",
    "start_flow",
]
