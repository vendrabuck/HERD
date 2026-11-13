"""The OpenID Connect settings (ADR 0016 phase 1, issue #37): every default, the
algorithm Literal refused at load, `AUTH_METHOD` still refusing `oidc` (the
feature stays dark), the compose pairing of all twelve variables, what
`oidc_configuration_problems` reports, and exactly when each startup warning
fires.

The repository-wide compose pairing (every Settings field wired, every default
equal) is `tests/unit/test_compose_settings_wiring.py`; the check here reads the
auth service's own block for the twelve OIDC lines so a removed or renamed line
fails in the auth suite too.
"""

from __future__ import annotations

import json
import logging
import re
import string
import typing
from pathlib import Path

import pytest
from app import config
from app.config import (
    OidcIdTokenAlgorithm,
    Settings,
    log_oidc_startup_warnings,
    oidc_configuration_problems,
)
from herd_common.jwt_codec import ID_TOKEN_ALGORITHMS
from herd_common.logging import JSONFormatter
from pydantic import ValidationError

REPO = Path(__file__).resolve().parents[3]

DEFAULTS = {
    "oidc_issuer": "",
    "oidc_client_id": "",
    "oidc_client_secret": "",
    "oidc_redirect_uri": "",
    "oidc_scopes": "openid email profile",
    "oidc_id_token_algorithm": "RS256",
    "oidc_username_claim": "preferred_username",
    "oidc_email_claim": "email",
    "oidc_provider_name": "single sign-on",
    "oidc_require_https": True,
    "oidc_ca_cert": "",
    "oidc_break_glass_local_superadmin": False,
}

CLIENT_SECRET = "SENTINEL-client-secret-value"

COMPLETE = {
    "oidc_issuer": "https://idp.test/realms/herd",
    "oidc_client_id": "herd",
    "oidc_client_secret": CLIENT_SECRET,
    "oidc_redirect_uri": "https://herd.test/api/auth/oidc/callback",
}


@pytest.fixture
def clean_env(monkeypatch):
    """No OIDC_* or AUTH_METHOD variable leaks in from the shell running the tests."""
    for name in [*DEFAULTS, "auth_method"]:
        monkeypatch.delenv(name.upper(), raising=False)
    return monkeypatch


def oidc_settings(**changes) -> Settings:
    """A Settings model in OIDC mode. `auth_method` does not admit "oidc" at
    load, so it is set after construction, the way the service tests set it."""
    loaded = Settings(**{**COMPLETE, **changes})
    loaded.auth_method = "oidc"
    return loaded


# --- defaults and load-time validation ---


def test_every_oidc_default(clean_env):
    loaded = Settings()
    assert {name: getattr(loaded, name) for name in DEFAULTS} == DEFAULTS


def test_the_settings_model_has_exactly_the_twelve_oidc_fields():
    assert sorted(n for n in Settings.model_fields if n.startswith("oidc_")) == sorted(DEFAULTS)


def test_the_algorithm_literal_is_the_shared_asymmetric_list():
    assert typing.get_args(OidcIdTokenAlgorithm) == ID_TOKEN_ALGORITHMS


@pytest.mark.parametrize("algorithm", ID_TOKEN_ALGORITHMS)
def test_each_listed_algorithm_loads_from_the_environment(algorithm, clean_env):
    clean_env.setenv("OIDC_ID_TOKEN_ALGORITHM", algorithm)
    assert Settings().oidc_id_token_algorithm == algorithm


@pytest.mark.parametrize(
    "algorithm",
    ["HS256", "HS384", "HS512", "none", "None", "NONE", "", "rs256", "EdDSA", "ES256K", "RS1"],
)
def test_any_other_algorithm_is_refused_at_load_without_echoing_it(algorithm, clean_env):
    clean_env.setenv("OIDC_ID_TOKEN_ALGORITHM", algorithm)
    with pytest.raises(ValidationError) as exc_info:
        Settings()
    [error] = exc_info.value.errors()
    assert error["loc"] == ("oidc_id_token_algorithm",)
    assert error["type"] == "literal_error"
    # HerdBaseSettings hides the refused input from the error text.
    assert "input_value" not in str(exc_info.value)


def test_auth_method_still_refuses_oidc_so_the_feature_stays_dark(clean_env):
    clean_env.setenv("AUTH_METHOD", "oidc")
    with pytest.raises(ValidationError) as exc_info:
        Settings()
    assert [e["loc"] for e in exc_info.value.errors()] == [("auth_method",)]


@pytest.mark.parametrize(
    ("variable", "text", "expected"),
    [("OIDC_REQUIRE_HTTPS", "false", False), ("OIDC_BREAK_GLASS_LOCAL_SUPERADMIN", "true", True)],
)
def test_the_two_switches_load_from_the_environment(variable, text, expected, clean_env):
    clean_env.setenv(variable, text)
    assert getattr(Settings(), variable.lower()) is expected


# --- compose pairing ---


def _auth_environment_block() -> str:
    text = (REPO / "docker-compose.yml").read_text()
    start = text.index("\n  auth:\n")
    following = re.search(r"\n  [a-z][a-z0-9-]*:\n", text[start + 1 :])
    return text[start : start + 1 + following.start()] if following else text[start:]


def _compose_default(value: object) -> str:
    return str(value).lower() if isinstance(value, bool) else str(value)


@pytest.mark.parametrize("field", sorted(DEFAULTS))
def test_each_oidc_setting_is_wired_in_the_auth_compose_block_with_its_default(field):
    variable = field.upper()
    line = f"      {variable}: ${{{variable}:-{_compose_default(DEFAULTS[field])}}}\n"
    assert line in _auth_environment_block()


def test_each_oidc_setting_has_a_commented_env_example_line():
    text = (REPO / ".env.example").read_text()
    for field in DEFAULTS:
        assert re.search(rf"^#{field.upper()}=", text, re.MULTILINE), field


def test_each_oidc_setting_has_an_env_vars_row():
    text = (REPO / "docs" / "ENV_VARS.md").read_text()
    for field in DEFAULTS:
        assert f"| `{field.upper()}` |" in text, field


# --- oidc_configuration_problems ---


def test_a_complete_configuration_has_no_problem():
    assert oidc_configuration_problems(oidc_settings()) == []


def test_the_defaults_name_the_four_unset_variables(clean_env):
    assert oidc_configuration_problems(Settings()) == [
        "OIDC_ISSUER",
        "OIDC_CLIENT_ID",
        "OIDC_CLIENT_SECRET",
        "OIDC_REDIRECT_URI",
    ]


@pytest.mark.parametrize(
    ("changes", "variable"),
    [
        ({"oidc_issuer": ""}, "OIDC_ISSUER"),
        ({"oidc_issuer": "   "}, "OIDC_ISSUER"),
        ({"oidc_issuer": "http://idp.test/realms/herd"}, "OIDC_ISSUER"),
        ({"oidc_issuer": "https://idp.test/realms/herd?x=1"}, "OIDC_ISSUER"),
        ({"oidc_issuer": "https://idp.test/realms/herd#f"}, "OIDC_ISSUER"),
        ({"oidc_issuer": "idp.test/realms/herd"}, "OIDC_ISSUER"),
        ({"oidc_issuer": " https://idp.test/realms/herd"}, "OIDC_ISSUER"),
        ({"oidc_issuer": "ftp://idp.test/realms/herd"}, "OIDC_ISSUER"),
        ({"oidc_client_id": ""}, "OIDC_CLIENT_ID"),
        ({"oidc_client_id": " \t"}, "OIDC_CLIENT_ID"),
        ({"oidc_client_secret": ""}, "OIDC_CLIENT_SECRET"),
        ({"oidc_client_secret": "  "}, "OIDC_CLIENT_SECRET"),
        ({"oidc_redirect_uri": ""}, "OIDC_REDIRECT_URI"),
        ({"oidc_redirect_uri": "http://herd.test/api/auth/oidc/callback"}, "OIDC_REDIRECT_URI"),
        (
            {"oidc_redirect_uri": "https://herd.test/api/auth/oidc/callback?x=1"},
            "OIDC_REDIRECT_URI",
        ),
        ({"oidc_redirect_uri": "https://herd.test/api/auth/oidc/callback#f"}, "OIDC_REDIRECT_URI"),
        ({"oidc_redirect_uri": "https://herd.test/api/auth/oidc/callback/"}, "OIDC_REDIRECT_URI"),
        ({"oidc_redirect_uri": "https://herd.test/api/auth/callback"}, "OIDC_REDIRECT_URI"),
        ({"oidc_redirect_uri": "/api/auth/oidc/callback"}, "OIDC_REDIRECT_URI"),
        ({"oidc_scopes": "email profile"}, "OIDC_SCOPES"),
        ({"oidc_scopes": "openidx email"}, "OIDC_SCOPES"),
        ({"oidc_scopes": ""}, "OIDC_SCOPES"),
        ({"oidc_username_claim": ""}, "OIDC_USERNAME_CLAIM"),
        ({"oidc_email_claim": " "}, "OIDC_EMAIL_CLAIM"),
        ({"oidc_ca_cert": "/nonexistent/ca.pem"}, "OIDC_CA_CERT"),
        # Userinfo would be sent as Basic credentials on discovery.
        ({"oidc_issuer": "https://u:p@idp.test/realms/herd"}, "OIDC_ISSUER"),
        ({"oidc_issuer": "https://u@idp.test/realms/herd"}, "OIDC_ISSUER"),
        ({"oidc_issuer": "https://idp.test/realms/herd?"}, "OIDC_ISSUER"),
        ({"oidc_issuer": "https://idp.test/realms/herd#"}, "OIDC_ISSUER"),
        ({"oidc_issuer": "https://idp.test/r\u00e9alms/herd"}, "OIDC_ISSUER"),
        (
            {"oidc_redirect_uri": "https://u:p@herd.test/api/auth/oidc/callback"},
            "OIDC_REDIRECT_URI",
        ),
        ({"oidc_redirect_uri": "https://herd.test/api/auth/oidc/callback?"}, "OIDC_REDIRECT_URI"),
        ({"oidc_redirect_uri": "https://herd.test/api/auth/oidc/callback#"}, "OIDC_REDIRECT_URI"),
        ({"oidc_redirect_uri": "https://herd.test/api/../oidc/callback"}, "OIDC_REDIRECT_URI"),
        ({"oidc_redirect_uri": "https://herd.test/api/%2e%2e/oidc/callback"}, "OIDC_REDIRECT_URI"),
        ({"oidc_redirect_uri": "https://herd.test/api/%2E%2e/oidc/callback"}, "OIDC_REDIRECT_URI"),
        ({"oidc_redirect_uri": "https://herd.test/api/auth/oidc/callback\t"}, "OIDC_REDIRECT_URI"),
        ({"oidc_redirect_uri": "https://herd.test/a pi/auth/oidc/callback"}, "OIDC_REDIRECT_URI"),
    ],
)
def test_each_missing_or_invalid_setting_is_named(changes, variable):
    assert oidc_configuration_problems(oidc_settings(**changes)) == [variable]


def test_a_ca_bundle_that_is_a_directory_is_named(tmp_path):
    assert oidc_configuration_problems(oidc_settings(oidc_ca_cert=str(tmp_path))) == [
        "OIDC_CA_CERT"
    ]


def test_an_existing_ca_bundle_file_is_accepted(tmp_path):
    bundle = tmp_path / "ca.pem"
    bundle.write_text("a file; its contents are checked when a request is made")
    assert oidc_configuration_problems(oidc_settings(oidc_ca_cert=str(bundle))) == []


def test_plain_http_issuer_and_redirect_are_accepted_when_https_is_not_required():
    loaded = oidc_settings(
        oidc_issuer="http://oidc-test:5556/dex",
        oidc_redirect_uri="http://192.0.2.10/api/auth/oidc/callback",
        oidc_require_https=False,
    )
    assert oidc_configuration_problems(loaded) == []


def test_scopes_need_openid_as_a_word_in_any_position():
    assert oidc_configuration_problems(oidc_settings(oidc_scopes="profile  openid")) == []


# --- startup warnings ---


def _warnings(caplog) -> list[dict]:
    formatter = JSONFormatter("auth")
    return [
        json.loads(formatter.format(r))
        for r in caplog.records
        if r.name == config.logger.name and r.levelno == logging.WARNING
    ]


@pytest.mark.parametrize("mode", ["local", "ldap"])
def test_no_oidc_warning_fires_outside_oidc_mode(mode, caplog, clean_env):
    caplog.set_level(logging.DEBUG)
    loaded = Settings(oidc_require_https=False, oidc_break_glass_local_superadmin=True)
    loaded.auth_method = mode
    assert log_oidc_startup_warnings(loaded) == []
    assert _warnings(caplog) == []


def test_no_warning_fires_for_a_complete_https_configuration_without_break_glass(caplog):
    caplog.set_level(logging.DEBUG)
    assert log_oidc_startup_warnings(oidc_settings()) == []
    assert _warnings(caplog) == []


def test_incomplete_settings_fire_one_warning_naming_the_variables_not_their_values(caplog):
    caplog.set_level(logging.DEBUG)
    loaded = oidc_settings(oidc_client_id="", oidc_redirect_uri="https://x.test/wrong")
    assert log_oidc_startup_warnings(loaded) == ["oidc_settings_incomplete"]
    [logged] = _warnings(caplog)
    assert logged["action"] == "oidc_settings_incomplete"
    assert logged["settings"] == ["OIDC_CLIENT_ID", "OIDC_REDIRECT_URI"]
    assert logged["message"] == (
        "OIDC settings missing or invalid: OIDC_CLIENT_ID, OIDC_REDIRECT_URI; "
        "single sign-on is unavailable until they are set"
    )
    assert "https://x.test/wrong" not in json.dumps(logged)


def test_an_unset_secret_is_named_and_a_set_one_never_logged(caplog):
    caplog.set_level(logging.DEBUG)
    log_oidc_startup_warnings(oidc_settings(oidc_client_secret="", oidc_require_https=False))
    log_oidc_startup_warnings(oidc_settings(oidc_issuer="", oidc_break_glass_local_superadmin=True))
    text = json.dumps(_warnings(caplog))
    assert "OIDC_CLIENT_SECRET" in text
    assert CLIENT_SECRET not in text


def test_https_not_required_fires_its_warning(caplog):
    caplog.set_level(logging.DEBUG)
    assert log_oidc_startup_warnings(oidc_settings(oidc_require_https=False)) == [
        "oidc_https_not_required"
    ]
    [logged] = _warnings(caplog)
    assert logged["action"] == "oidc_https_not_required"
    assert logged["message"] == (
        "OIDC_REQUIRE_HTTPS is false: the issuer, its endpoints, and the redirect URI "
        "may use plain HTTP; use this only for a lab identity provider"
    )


def test_break_glass_on_fires_its_warning(caplog):
    caplog.set_level(logging.DEBUG)
    assert log_oidc_startup_warnings(oidc_settings(oidc_break_glass_local_superadmin=True)) == [
        "oidc_break_glass_enabled"
    ]
    [logged] = _warnings(caplog)
    assert logged["action"] == "oidc_break_glass_enabled"
    assert logged["message"] == (
        "OIDC_BREAK_GLASS_LOCAL_SUPERADMIN is true: POST /login admits local "
        "superadmin accounts with a password in single sign-on mode"
    )


def test_all_three_warnings_fire_together_in_order(caplog):
    caplog.set_level(logging.DEBUG)
    loaded = oidc_settings(
        oidc_issuer="", oidc_require_https=False, oidc_break_glass_local_superadmin=True
    )
    assert log_oidc_startup_warnings(loaded) == [
        "oidc_settings_incomplete",
        "oidc_https_not_required",
        "oidc_break_glass_enabled",
    ]
    assert [w["action"] for w in _warnings(caplog)] == [
        "oidc_settings_incomplete",
        "oidc_https_not_required",
        "oidc_break_glass_enabled",
    ]


def test_a_single_dot_segment_and_dots_inside_a_segment_are_not_refused():
    loaded = oidc_settings(oidc_redirect_uri="https://herd.test/a..b/./api/auth/oidc/callback")
    assert oidc_configuration_problems(loaded) == []


# --- the redirect path's character set (ADR 0016 D11, amended in phase 3) ---

ALLOWED_PATH_CHARACTERS = set(string.ascii_letters + string.digits + "-._~/%")
# Every ASCII character outside the set (controls, space, comma, ";", "=", and
# the rest), plus non-ASCII ones: a Latin letter, a Cyrillic look-alike, and a
# Unicode line separator.
REFUSED_PATH_CHARACTERS = [chr(c) for c in range(0x80) if chr(c) not in ALLOWED_PATH_CHARACTERS] + [
    "\u00e9",
    "\u0430",
    "\u2028",
]


def test_the_allowed_redirect_path_characters_are_exactly_the_set():
    assert config._REDIRECT_PATH_CHARACTERS == ALLOWED_PATH_CHARACTERS
    loaded = oidc_settings(oidc_redirect_uri="https://herd.test/AZaz09-._~%41/oidc/callback")
    assert config.oidc_cookie_path(loaded) == "/AZaz09-._~%41/oidc"
    assert oidc_configuration_problems(loaded) == []


@pytest.mark.parametrize(
    "character", REFUSED_PATH_CHARACTERS, ids=[f"U+{ord(c):04X}" for c in REFUSED_PATH_CHARACTERS]
)
def test_each_refused_redirect_path_character_yields_no_cookie_path(character):
    """One case per refused character, each pinned on its own: it is outside
    `_REDIRECT_PATH_CHARACTERS`, and a redirect URI whose path holds it gives no
    cookie path and is reported invalid. `;` is the attribute separator Starlette
    would write unquoted into the cookies' Path."""
    assert character not in config._REDIRECT_PATH_CHARACTERS
    loaded = oidc_settings(oidc_redirect_uri=f"https://herd.test/a{character}b/oidc/callback")
    assert config.oidc_cookie_path(loaded) is None
    assert "OIDC_REDIRECT_URI" in oidc_configuration_problems(loaded)


def test_a_semicolon_alone_is_refused_in_the_redirect_path():
    """No `=` beside it, so only the character-set rule can refuse it."""
    loaded = oidc_settings(oidc_redirect_uri="https://herd.test/x;Secure/oidc/callback")
    assert config.oidc_cookie_path(loaded) is None
    assert oidc_configuration_problems(loaded) == ["OIDC_REDIRECT_URI"]
