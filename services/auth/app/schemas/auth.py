import uuid
from datetime import datetime
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, EmailStr, Field, StringConstraints, field_validator

from app.models.user import Role
from app.schemas.pagination import Paginated

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
    password: str = Field(min_length=8, max_length=72)
    # Optional display name (issue #1252): omitted or null stores the username.
    full_name: FullName | None = None


class LoginRequest(BaseModel):
    # Accepts an email in local mode or a directory login name (e.g.
    # sAMAccountName) in LDAP mode; both flow through authenticate_user.
    email: str = Field(min_length=1, max_length=255)
    password: str = Field(max_length=72)


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


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
