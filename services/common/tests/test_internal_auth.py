"""Tests for the shared constant-time X-Internal-Token verifier (issue #311).

Pins the fail-closed contract: a correct token passes, a wrong token refuses,
and an unset or empty value on either side (configured or provided) refuses
rather than accidentally matching. Also asserts the implementation actually
calls hmac.compare_digest rather than a plain `==`/`!=`, so a later edit that
reintroduces a non-constant-time comparison fails this test.
"""

from unittest.mock import patch

from herd_common.internal_auth import internal_token_matches


def test_correct_token_matches():
    assert internal_token_matches("secret-token", "secret-token") is True


def test_wrong_token_does_not_match():
    assert internal_token_matches("wrong-token", "secret-token") is False


def test_empty_configured_token_refuses_even_with_empty_header():
    # A naive `provided == configured` would let an empty header through when
    # the server-side token is unset; this must refuse instead.
    assert internal_token_matches("", "") is False


def test_none_configured_token_refuses():
    assert internal_token_matches("secret-token", None) is False


def test_none_configured_token_refuses_any_provided_value():
    assert internal_token_matches("anything", None) is False


def test_empty_provided_header_refuses():
    assert internal_token_matches("", "secret-token") is False


def test_none_provided_header_refuses():
    assert internal_token_matches(None, "secret-token") is False


def test_prefix_match_does_not_short_circuit_match():
    # A near-miss (long shared prefix) must still refuse; regression guard
    # for any future accidental swap back to a prefix-based check.
    assert internal_token_matches("secret-toke", "secret-token") is False
    assert internal_token_matches("secret-tokenX", "secret-token") is False


def test_uses_constant_time_comparison():
    """Pin the implementation to hmac.compare_digest, not `==`/`!=`.

    A later edit that reverts to a plain string comparison must fail this
    test even though the boolean result would look identical on this input.
    """
    with patch("herd_common.internal_auth.hmac.compare_digest") as mock_compare:
        mock_compare.return_value = True
        result = internal_token_matches("a", "b")
    mock_compare.assert_called_once_with("a", "b")
    assert result is True
