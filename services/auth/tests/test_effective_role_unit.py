"""Unit tests for app.dependencies.auth.effective_role.

This is the pure function that decides, for every authorization decision
inside the auth service, which role a request is held to: the lower of the
JWT's role claim and the account's current database role. Enumerated over
the full space rather than a handful of examples, because the invariant this
function restores (a demoted or downscoped token can never regain the
account's higher database role inside this service) is exactly the kind of
thing a partial test suite would miss a corner of.
"""

import pytest
from app.dependencies.auth import effective_role
from app.models.user import Role

ROLES = [Role.USER, Role.ADMIN, Role.SUPERADMIN]


@pytest.mark.parametrize("db_role", ROLES)
@pytest.mark.parametrize("claim_role", ROLES)
def test_effective_role_is_the_lower_of_claim_and_db_role(claim_role, db_role):
    """All nine (claim, db) pairs: the exact returned role, not just "not higher"."""
    rank = {Role.USER: 0, Role.ADMIN: 1, Role.SUPERADMIN: 2}
    expected = claim_role if rank[claim_role] <= rank[db_role] else db_role
    assert effective_role(claim_role.value, db_role) == expected


@pytest.mark.parametrize("db_role", ROLES)
def test_missing_claim_counts_as_user(db_role):
    assert effective_role(None, db_role) == Role.USER


@pytest.mark.parametrize("db_role", ROLES)
def test_empty_claim_counts_as_user(db_role):
    assert effective_role("", db_role) == Role.USER


@pytest.mark.parametrize("db_role", ROLES)
def test_unknown_claim_counts_as_user(db_role):
    assert effective_role("not-a-real-role", db_role) == Role.USER


def test_matching_claim_and_db_role_is_a_no_op():
    """No behavior change for a request whose claim equals the DB role."""
    for role in ROLES:
        assert effective_role(role.value, role) == role


def test_claim_never_exceeds_db_role():
    """A user-claim token on a superadmin row stays at user; the reported defect."""
    assert effective_role(Role.USER.value, Role.SUPERADMIN) == Role.USER


def test_db_role_never_exceeds_claim():
    """A superadmin-claim token on a since-demoted user row is held to user."""
    assert effective_role(Role.SUPERADMIN.value, Role.USER) == Role.USER
