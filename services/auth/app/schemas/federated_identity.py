"""Request and response bodies of the superadmin federated-identity routes (issue #1399).

Both request bodies refuse unknown keys (`extra="forbid"`), like auth's other
admin bodies. Every issuer and subject passes the ONE predicate a login's
identity key passes, `auth_service.identity_key_part_ok` (IAM-OIDC-32): a
string that is not blank, at most 255 characters, with no Cc or Cs character.
Nothing else is trimmed or normalized; the value is stored and compared as
sent, because an ID token's `iss` and `sub` are compared byte for byte. The
refusal message is fixed and never quotes the value.
"""

import uuid
from datetime import datetime
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, StrictBool, model_validator
from pydantic_core import PydanticCustomError

from app.models.federated_identity import SUBJECT_MAX_LENGTH
from app.services.auth_service import ISSUER_MAX_LENGTH, identity_key_part_ok

IDENTITY_KEY_MESSAGE = (
    "must be a non-blank string of at most {limit} characters with no control character"
)
RELINK_TARGET_MESSAGE = "send either issuer and subject, or candidate_id, and not both"


def _identity_key(limit: int):
    def check(value: str) -> str:
        if not identity_key_part_ok(value, limit):
            raise PydanticCustomError(
                "identity_key_invalid", IDENTITY_KEY_MESSAGE, {"limit": limit}
            )
        return value

    return AfterValidator(check)


# A non-string is refused by the `str` type itself (`string_type`; pydantic
# does not coerce a number to a string). A lone surrogate, which JSON can carry
# as an escape such as "\ud800", reaches the check below (pydantic's JSON
# parser keeps it in the string) and is refused there as `identity_key_invalid`,
# since a lone surrogate is a Cs character.
Issuer = Annotated[str, _identity_key(ISSUER_MAX_LENGTH)]
Subject = Annotated[str, _identity_key(SUBJECT_MAX_LENGTH)]


class IssuerRewriteRequest(BaseModel):
    """`POST /admin/federated-identities/rewrite-issuer`. `dry_run` is required
    and must be a JSON boolean, so a write is always asked for in so many
    words."""

    model_config = ConfigDict(extra="forbid")

    old_issuer: Issuer
    new_issuer: Issuer
    dry_run: StrictBool


class CollisionPair(BaseModel):
    """One subject that blocks a rewrite: the account whose identity is under
    the old issuer, and the account already holding that subject under the new
    issuer. Account ids only, no claim."""

    old_issuer_account_id: uuid.UUID
    new_issuer_account_id: uuid.UUID


class IssuerRewriteResponse(BaseModel):
    dry_run: bool
    matched: int
    conflicts: int
    rewritten: int
    collisions: list[CollisionPair]
    collisions_truncated: bool


class IssuerRewriteConflictResponse(BaseModel):
    """The 409 body: `detail` is the usual fixed string; `conflicts` the total
    and `collisions` at most 50 pairs (`collisions_truncated` when there were
    more)."""

    detail: str
    conflicts: int
    collisions: list[CollisionPair]
    collisions_truncated: bool


class FederatedIdentityLinkRequest(BaseModel):
    """`PUT /users/{user_id}/federated-identity`: the exact pair the account's
    next single sign-on login must present, typed (`issuer` and `subject`) or
    taken from one of the account's live relink candidates (`candidate_id`).
    Exactly one of the two forms; a null counts as absent."""

    model_config = ConfigDict(extra="forbid")

    issuer: Issuer | None = None
    subject: Subject | None = None
    candidate_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _exactly_one_target(self):
        typed = self.issuer is not None or self.subject is not None
        complete = self.issuer is not None and self.subject is not None
        if typed == (self.candidate_id is not None) or (typed and not complete):
            raise PydanticCustomError("relink_target", RELINK_TARGET_MESSAGE)
        return self


class FederatedIdentityResponse(BaseModel):
    user_id: uuid.UUID
    issuer: str
    subject: str
    created_at: datetime
    last_login_at: datetime | None


class RelinkCandidateResponse(BaseModel):
    """A live relink candidate. Never the subject: `fingerprint` is the keyed
    fingerprint of its (issuer, subject), the same value the relink's audit
    line carries as `new_fingerprint` when this candidate is used."""

    id: uuid.UUID
    issuer: str
    fingerprint: str
    created_at: datetime
    last_seen_at: datetime
    expires_at: datetime
    attempts: int


class RelinkCandidateListResponse(BaseModel):
    """`GET /users/{user_id}/federated-identity/candidates`: the account's live
    candidates, newest first, and `captured_total`, the number of distinct
    pairs ever captured for it, which keeps counting when the caps evict."""

    user_id: uuid.UUID
    captured_total: int
    candidates: list[RelinkCandidateResponse]
