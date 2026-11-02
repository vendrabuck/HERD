"""Schema tests for the user profile request bodies (issue #1252).

Pure Pydantic, no DB or HTTP. Pins each bound at the boundary (cap accepted,
cap plus one refused), PATCH's null and empty-string rules, the extra="forbid"
refusal of every non-profile key, and pydantic's exact error type and message
for each refusal, because the frontend shows those messages per field.
"""

import pytest
from app.schemas.auth import (
    FULL_NAME_NULL_MESSAGE,
    PHONE_PATTERN,
    ProfileUpdateRequest,
    RegisterRequest,
)
from pydantic import ValidationError

_PATTERN_MESSAGE = f"String should match pattern '{PHONE_PATTERN}'"


def _only_error(**body) -> dict:
    with pytest.raises(ValidationError) as exc:
        ProfileUpdateRequest(**body)
    errors = exc.value.errors(include_url=False, include_input=False)
    assert len(errors) == 1, errors
    return errors[0]


# --- full_name ---


def test_full_name_is_stripped():
    assert ProfileUpdateRequest(full_name="  Ada Lovelace  ").full_name == "Ada Lovelace"


def test_full_name_at_200_characters_accepted():
    assert ProfileUpdateRequest(full_name="n" * 200).full_name == "n" * 200


def test_full_name_200_characters_after_strip_accepted():
    assert ProfileUpdateRequest(full_name="  " + "n" * 200 + "  ").full_name == "n" * 200


def test_full_name_at_201_characters_refused_with_exact_message():
    error = _only_error(full_name="n" * 201)
    assert error["loc"] == ("full_name",)
    assert error["type"] == "string_too_long"
    assert error["msg"] == "String should have at most 200 characters"


@pytest.mark.parametrize("value", ["", "   ", "\t\n"])
def test_full_name_empty_after_strip_refused_with_exact_message(value):
    error = _only_error(full_name=value)
    assert error["loc"] == ("full_name",)
    assert error["type"] == "string_too_short"
    assert error["msg"] == "String should have at least 1 character"


def test_full_name_null_refused_naming_the_field():
    error = _only_error(full_name=None)
    assert error["loc"] == ("full_name",)
    assert error["type"] == "value_error"
    assert error["msg"] == f"Value error, {FULL_NAME_NULL_MESSAGE}"
    assert FULL_NAME_NULL_MESSAGE == "full_name may not be null"


# --- contact_email ---


def test_contact_email_valid_address_accepted():
    assert ProfileUpdateRequest(contact_email="ada@example.com").contact_email == "ada@example.com"


def test_contact_email_invalid_address_refused_with_exact_message():
    error = _only_error(contact_email="not-an-address")
    assert error["loc"] == ("contact_email",)
    assert error["type"] == "value_error"
    assert error["msg"] == (
        "value is not a valid email address: An email address must have an @-sign."
    )


@pytest.mark.parametrize("blank", ["", "   "])
def test_contact_email_blank_reads_as_null(blank):
    body = ProfileUpdateRequest(contact_email=blank)
    assert body.contact_email is None
    assert body.changes() == {"contact_email": None}


def test_contact_email_null_clears():
    assert ProfileUpdateRequest(contact_email=None).changes() == {"contact_email": None}


# --- phone ---


def test_phone_with_every_allowed_character_accepted():
    assert ProfileUpdateRequest(phone="+1 (555) 010-0199").phone == "+1 (555) 010-0199"


@pytest.mark.parametrize("value", ["5", "+5", "(5)", "5-5", "5 5"])
def test_phone_each_allowed_character_beside_a_digit_accepted(value):
    assert ProfileUpdateRequest(phone=value).phone == value


def test_phone_at_32_characters_accepted():
    assert ProfileUpdateRequest(phone="1" * 32).phone == "1" * 32


def test_phone_at_33_characters_refused_with_exact_message():
    error = _only_error(phone="1" * 33)
    assert error["loc"] == ("phone",)
    assert error["type"] == "string_too_long"
    assert error["msg"] == "String should have at most 32 characters"


@pytest.mark.parametrize("value", ["555-01a0", "555.0100", "ext 5", "555#1", "５５５"])
def test_phone_with_a_disallowed_character_refused_with_exact_message(value):
    error = _only_error(phone=value)
    assert error["loc"] == ("phone",)
    assert error["type"] == "string_pattern_mismatch"
    assert error["msg"] == _PATTERN_MESSAGE


@pytest.mark.parametrize("value", ["+", "()", "+-()", "- -"])
def test_phone_without_a_digit_refused_with_exact_message(value):
    error = _only_error(phone=value)
    assert error["loc"] == ("phone",)
    assert error["type"] == "string_pattern_mismatch"
    assert error["msg"] == _PATTERN_MESSAGE


@pytest.mark.parametrize("blank", ["", "   "])
def test_phone_blank_reads_as_null(blank):
    assert ProfileUpdateRequest(phone=blank).changes() == {"phone": None}


def test_phone_is_stripped():
    assert ProfileUpdateRequest(phone="  555 0100  ").phone == "555 0100"


# --- PATCH semantics and extra="forbid" ---


def test_omitted_keys_are_not_in_changes():
    assert ProfileUpdateRequest().changes() == {}
    assert ProfileUpdateRequest(phone="555").changes() == {"phone": "555"}


@pytest.mark.parametrize(
    "key,value",
    [
        ("role", "admin"),
        ("email", "other@example.com"),
        ("username", "other"),
        ("is_active", False),
        ("deactivated_by_sync", True),
        ("auth_source", "local"),
        ("hashed_password", "x"),
        ("department", "eng"),
    ],
)
def test_non_profile_key_refused_as_extra(key, value):
    error = _only_error(**{key: value})
    assert error["loc"] == (key,)
    assert error["type"] == "extra_forbidden"
    assert error["msg"] == "Extra inputs are not permitted"


# --- RegisterRequest.full_name ---

_REGISTER = {"email": "r@example.com", "username": "reg-user", "password": "password123"}


def test_register_full_name_is_optional():
    assert RegisterRequest(**_REGISTER).full_name is None


def test_register_full_name_is_stripped():
    assert RegisterRequest(**_REGISTER, full_name="  Reg User ").full_name == "Reg User"


def test_register_full_name_at_201_characters_refused():
    with pytest.raises(ValidationError) as exc:
        RegisterRequest(**_REGISTER, full_name="n" * 201)
    errors = exc.value.errors(include_url=False, include_input=False)
    assert [(e["loc"], e["msg"]) for e in errors] == [
        (("full_name",), "String should have at most 200 characters")
    ]


def test_register_full_name_blank_refused():
    with pytest.raises(ValidationError) as exc:
        RegisterRequest(**_REGISTER, full_name="   ")
    errors = exc.value.errors(include_url=False, include_input=False)
    assert [(e["loc"], e["msg"]) for e in errors] == [
        (("full_name",), "String should have at least 1 character")
    ]
