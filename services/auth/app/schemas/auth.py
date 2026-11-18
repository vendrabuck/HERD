import uuid
from datetime import datetime
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, EmailStr, Field, StringConstraints, field_validator
from pydantic_core import PydanticCustomError

from app.config import AuthMethod, settings
from app.models.user import Role
from app.schemas.pagination import Paginated
from app.services.passwords import validate_password

# Password bounds in characters (issue #1404, IAM-REG-2, IAM-LOGIN-3). A
# password checked against a bcrypt hash (registration, local login, the OIDC
# break-glass login) is at most PASSWORD_MAX_LENGTH, as it always was. In LDAP
# mode the password goes to the directory in a bind and never reaches bcrypt,
# so the login bound is only a ceiling on the request: 1024 characters, which
# are at most 4096 UTF-8 bytes (MAX_PASSWORD_BYTES in passwords.py).
PASSWORD_MAX_LENGTH = 72
LDAP_LOGIN_PASSWORD_MAX_LENGTH = 1024

# Profile field bounds (issue #1252). full_name is stripped of leading and
# trailing white space and must then be 1 to 200 characters (the column is
# String(200)). phone follows the loose rule the owner chose: only digits,
# spaces, "+", "-", "(", and ")", at least one digit, at most 32 characters
# (the column is String(32)); nothing dials it, so no E.164 check.
FULL_NAME_MAX_LENGTH = 200
PHONE_MAX_LENGTH = 32
PHONE_PATTERN = r"^[0-9 +()-]*[0-9][0-9 +()-]*$"

FullName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=FULL_NAME_MAX_LENGTH),
]
Phone = Annotated[str, StringConstraints(max_length=PHONE_MAX_LENGTH, pattern=PHONE_PATTERN)]

FULL_NAME_NULL_MESSAGE = "full_name may not be null"


def _blank_to_none(value: Any) -> Any:
    """Read a blank form input as null: strip a string, and an empty result is None."""
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return value


class RegisterRequest(BaseModel):
    email: EmailStr
    username: str = Field(min_length=3, max_length=32, pattern=r"^[a-zA-Z0-9_-]+$")
    password: str = Field(min_length=8, max_length=PASSWORD_MAX_LENGTH)
    # Optional display name (issue #1252): omitted or null stores the username.
    full_name: FullName | None = None

    # Refuse here exactly what hashing would refuse (a NUL), so registration
    # answers 422, not a 500 from create_user (issue #1426). LoginRequest has no
    # such check by decision: a NUL at login is the 401 of IAM-HASH-3.
    _password_is_hashable = field_validator("password")(validate_password)


def login_password_max_length(auth_method: AuthMethod) -> int:
    """The longest login password, in characters, the mode's judge takes (IAM-LOGIN-3).

    Issue #1404: local mode and the OIDC break-glass login check a bcrypt hash,
    so they keep registration's bound; LDAP mode sends every login, a local
    account's included (IAM-LDAP-1), to the directory and never hashes it.
    """
    if auth_method == "ldap":
        return LDAP_LOGIN_PASSWORD_MAX_LENGTH
    return PASSWORD_MAX_LENGTH


class LoginRequest(BaseModel):
    # Accepts an email in local mode or a directory login name (e.g.
    # sAMAccountName) in LDAP mode; both flow through authenticate_user.
    email: str = Field(min_length=1, max_length=255)
    # max_length is the ceiling of every mode, LDAP's; the validator below
    # applies the live mode's own bound. The constraint also keeps pydantic's
    # lone-surrogate refusal (`string_unicode`, IAM-LOGIN-5), which pydantic
    # runs only on a constrained string and ahead of any length check.
    password: str = Field(
        max_length=LDAP_LOGIN_PASSWORD_MAX_LENGTH,
        description=(
            f"At most {PASSWORD_MAX_LENGTH} characters in local mode and for the "
            f"single sign-on break-glass login, {LDAP_LOGIN_PASSWORD_MAX_LENGTH} in LDAP mode."
        ),
    )

    @field_validator("password")
    @classmethod
    def _password_within_the_judges_bound(cls, value: str) -> str:
        """The same `string_too_long` error a `max_length` raises, at the bound of
        the mode that will judge the password, read when the request arrives."""
        limit = login_password_max_length(settings.auth_method)
        if len(value) > limit:
            raise PydanticCustomError(
                "string_too_long",
                "String should have at most {max_length} characters",
                {"max_length": limit},
            )
        return value


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class OidcExchangeResponse(TokenResponse):
    """`POST /oidc/exchange` (ADR 0016 D3): the `POST /login` body plus `next`,
    the sanitized same-origin path the SPA navigates to."""

    next: str


class OidcExchangeRequest(BaseModel):
    """`POST /oidc/exchange` takes exactly `{}`: no field is defined and any
    key is refused (ADR 0016 D3)."""

    model_config = ConfigDict(extra="forbid")


class SsoOptions(BaseModel):
    provider_name: str


class LoginOptionsResponse(BaseModel):
    """`GET /login-options`: which sign-in methods the login page offers, from
    auth's live settings (ADR 0016 D10)."""

    auth_method: AuthMethod
    registration_open: bool
    password_login: bool
    sso: SsoOptions | None


class RefreshRequest(BaseModel):
    refresh_token: str


class LogoutRequest(BaseModel):
    refresh_token: str


class UserResponse(BaseModel):
    id: uuid.UUID
    email: str
    username: str
    full_name: str
    contact_email: str | None = None
    phone: str | None = None
    is_active: bool
    role: Role
    created_at: datetime
    updated_at: datetime
    modified_by: uuid.UUID | None = None

    model_config = {"from_attributes": True}


class ProfileUpdateRequest(BaseModel):
    """Body of PATCH /me/profile and PATCH /users/{user_id}/profile (issue #1252).

    PATCH semantics: an omitted key is unchanged. null on contact_email or
    phone clears it, and an empty or all-white-space string is read as null
    (a blank form input). null or an empty value on full_name is a 422 naming
    the field. Only the three profile fields are accepted: any other key
    (role, email, username, is_active, ...) is refused by extra="forbid".
    """

    model_config = ConfigDict(extra="forbid")

    full_name: FullName | None = None
    contact_email: EmailStr | None = None
    phone: Phone | None = None

    @field_validator("full_name", mode="before")
    @classmethod
    def _full_name_not_null(cls, value: Any) -> Any:
        if value is None:
            raise ValueError(FULL_NAME_NULL_MESSAGE)
        return value

    @field_validator("contact_email", "phone", mode="before")
    @classmethod
    def _blank_is_null(cls, value: Any) -> Any:
        return _blank_to_none(value)

    def changes(self) -> dict[str, str | None]:
        """The fields the caller sent, with their validated values."""
        return {name: getattr(self, name) for name in self.model_fields_set}


class SetRoleRequest(BaseModel):
    role: Role


class PaginatedUserResponse(Paginated[UserResponse]):
    pass
