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

from pydantic import AfterValidator, BaseModel, ConfigDict, StrictBool
from pydantic_core import PydanticCustomError

from app.models.federated_identity import SUBJECT_MAX_LENGTH
from app.services.auth_service import ISSUER_MAX_LENGTH, identity_key_part_ok

IDENTITY_KEY_MESSAGE = (
    "must be a non-blank string of at most {limit} characters with no control character"
)


def _identity_key(limit: int):
    def check(value: str) -> str:
        if not identity_key_part_ok(value, limit):
            raise PydanticCustomError(
                "identity_key_invalid", IDENTITY_KEY_MESSAGE, {"limit": limit}
            )
        return value

    return AfterValidator(check)


# `str` with strict=False still refuses a non-string (pydantic does not coerce
# a number to a string), and refuses a lone surrogate as `string_unicode`.
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


class IssuerRewriteResponse(BaseModel):
    dry_run: bool
    matched: int
    conflicts: int
    rewritten: int


class FederatedIdentityLinkRequest(BaseModel):
    """`PUT /users/{user_id}/federated-identity`: the exact pair the account's
    next single sign-on login must present."""

    model_config = ConfigDict(extra="forbid")

    issuer: Issuer
    subject: Subject


class FederatedIdentityResponse(BaseModel):
    user_id: uuid.UUID
    issuer: str
    subject: str
    created_at: datetime
    last_login_at: datetime | None
