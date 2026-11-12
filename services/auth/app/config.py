import logging
import os
from typing import Literal
from urllib.parse import unquote, urlsplit

from herd_common.auth_settings import AuthSecretKey, JwtAlgorithm
from herd_common.base_settings import HerdBaseSettings

logger = logging.getLogger(__name__)

# Production floor for the LDAP sync interval. Both clamps that need it live
# in this module (effective_ldap_sync_interval_seconds and
# effective_ldap_sync_run_stale_seconds, which floors at twice the effective
# interval), so there is ONE implementation of the interval clamp and the two
# cannot drift; app/tasks/ldap_sync_loop.py's effective_interval_seconds is a
# thin delegate. They live HERE rather than in the loop because
# app.tasks.ldap_sync_loop imports app.services.ldap_sync_service, which
# imports this module: importing the other way would be a cycle.
MIN_LDAP_SYNC_INTERVAL_SECONDS = 60

# ADR 0016 D8: the ID-token algorithms OIDC_ID_TOKEN_ALGORITHM admits, all
# asymmetric. Equal to herd_common.jwt_codec.ID_TOKEN_ALGORITHMS, pinned by
# tests/test_oidc_settings.py (a Literal must be spelled out to be one).
OidcIdTokenAlgorithm = Literal[
    "RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512"
]

# The path OIDC_REDIRECT_URI must end in (ADR 0016 D11).
OIDC_CALLBACK_PATH_SUFFIX = "/oidc/callback"


class Settings(HerdBaseSettings):
    database_url: str  # db login, password, and url go here via DATABASE_URL env var
    db_schema: str = "auth"
    secret_key: AuthSecretKey
    algorithm: JwtAlgorithm = "HS256"
    cors_origins: str = ""
    access_token_expire_minutes: int = 30
    refresh_token_expire_days: int = 7
    # Shared with other services for cross-service service-to-service calls.
    # Empty disables every internal-token endpoint on this service.
    internal_api_token: str = ""

    # Superadmin seed: set all three to create the single superadmin on first startup.
    # Leave any blank to skip seeding (safe default for non-production).
    superadmin_email: str = ""
    superadmin_username: str = ""
    superadmin_password: str = ""

    # Authentication backend: "local" uses bcrypt-hashed passwords stored in the
    # users table; "ldap" binds against the configured directory server instead.
    # The toggle is global: only one backend is active at a time.
    auth_method: Literal["local", "ldap"] = "local"

    # LDAP / Active Directory settings. Only consulted when auth_method == "ldap".
    ldap_server_url: str = ""
    ldap_bind_dn: str = ""
    ldap_bind_password: str = ""
    ldap_user_base_dn: str = ""
    ldap_user_filter: str = "(sAMAccountName={username})"
    ldap_email_attribute: str = "mail"
    ldap_username_attribute: str = "sAMAccountName"
    # Group sync (ADR 0011). The group entry's membership attribute, values
    # interpreted as member DNs (Active Directory and groupOfNames "member";
    # posixGroup memberUid is out of scope), and the attribute cached as the
    # mapping's display name. Remaining sync keys land with the phases that
    # consult them.
    ldap_group_member_attribute: str = "member"
    ldap_group_name_attribute: str = "cn"
    # Interval loop (ADR 0011 phase 5). Dark by default: enabling mapping
    # CRUD and on-demand sync-now above never opts a deployment into the
    # background loop. Both this flag and auth_method == "ldap" gate whether
    # main.py's lifespan starts the loop task at all.
    ldap_group_sync_enabled: bool = False
    ldap_sync_interval_seconds: int = 3600
    # Retention for the ldap_sync_runs audit table, pruned by the same
    # interval loop (never by sync-now).
    ldap_sync_runs_retention_days: int = 90
    # Stale-run reaper (issue #528): a row stuck in status "running" older
    # than this many seconds is flipped to "failed", the crash-corpse case
    # where a hard process death (OOM kill, container crash, power loss)
    # never reached execute_run's finally block. Reaped at the START of
    # every sync run (interval tick or sync-now), inside the sync lock.
    # Read through effective_ldap_sync_run_stale_seconds() below, never
    # raw: the default is deliberately TWICE the default interval, and the
    # accessor enforces that 2x relationship as a floor, so a run that is
    # merely slow (or an interval tuned longer than this) is never mistaken
    # for a corpse.
    ldap_sync_run_stale_seconds: int = 7200
    # Deactivation sweep (ADR 0011 phase 4). Independent opt-in: enabling
    # group mirroring above never opts a deployment into deactivation.
    ldap_sync_deactivation_enabled: bool = False
    # A complete filter expression (not a value) identifying disabled
    # accounts, e.g. the AD userAccountControl lockout-bit filter. Empty
    # means absence-only detection.
    ldap_disabled_filter: str = ""
    # Circuit breaker: the sweep aborts (deactivating no one) only when the
    # proven-absent-or-disabled count STRICTLY exceeds BOTH terms.
    ldap_sync_deactivation_max_percent: int = 20
    ldap_sync_deactivation_min_count: int = 3
    ldap_use_tls: bool = True
    # Verify the directory server's TLS certificate. Defaults to True (the bind
    # transmits the service-account and every user's password, so an unvalidated
    # cert means an active network attacker can MITM and harvest credentials).
    # Set False only for a lab directory behind a self-signed cert that you
    # cannot pin via ldap_ca_cert; doing so logs a startup warning.
    ldap_tls_validate: bool = True
    # Optional path (inside the container) to a CA bundle to verify the directory
    # server against, e.g. a pinned internal CA. Used when ldap_tls_validate is
    # True; when empty the system trust store is used.
    ldap_ca_cert: str = ""

    # OpenID Connect single sign-on (ADR 0016, issue #37). Consulted only when
    # auth_method == "oidc", which auth_method above does not admit yet: the
    # feature stays dark until ADR 0016 phase 6 widens it. Incomplete settings
    # never stop auth from booting (the LDAP precedent); see
    # oidc_configuration_problems and log_oidc_startup_warnings below.
    oidc_issuer: str = ""
    oidc_client_id: str = ""
    # Sent to the token endpoint with client_secret_basic; never logged.
    oidc_client_secret: str = ""
    oidc_redirect_uri: str = ""
    oidc_scopes: str = "openid email profile"
    # The one accepted ID-token algorithm; anything else is refused at load.
    oidc_id_token_algorithm: OidcIdTokenAlgorithm = "RS256"
    oidc_username_claim: str = "preferred_username"
    oidc_email_claim: str = "email"
    oidc_provider_name: str = "single sign-on"
    # False only for a lab IdP served over plain HTTP; logs a startup warning.
    oidc_require_https: bool = True
    # Optional CA bundle path (inside the container) for a private IdP CA; when
    # empty the system trust store is used.
    oidc_ca_cert: str = ""
    # ADR 0016 D5. The field only; POST /login reads it from ADR 0016 phase 3.
    oidc_break_glass_local_superadmin: bool = False

    log_level: str = "INFO"


settings = Settings()


def effective_ldap_sync_interval_seconds(raw: int, *, warn: bool = True) -> int:
    """Clamp a configured LDAP sync interval to a sane floor.

    A misconfigured ldap_sync_interval_seconds (0, a typo like 3, or any
    value well under a minute) must not turn the interval loop into a
    directory-hammering busy loop. Deliberately NOT a pydantic ge= validator
    on the setting itself: a bad tuning knob must never prevent auth from
    booting, since login availability outranks strictness here. Clamping
    (and logging that it happened) keeps the service up with a safe interval
    instead.

    The single implementation of that clamp. Two callers reach it:
    app.tasks.ldap_sync_loop.effective_interval_seconds (which delegates
    here, so the production loop, main.py's lifespan, and the status
    endpoint keep their existing entry point) and
    effective_ldap_sync_run_stale_seconds below, whose floor is twice the
    EFFECTIVE interval and so must clamp the interval exactly the same way.
    That second caller passes warn=False: it runs on EVERY sync run (the
    reaper reads its threshold there), so warning from it would repeat a
    misconfiguration the loop's own call already logged at startup, once per
    run, forever.
    """
    if raw < MIN_LDAP_SYNC_INTERVAL_SECONDS:
        if not warn:
            return MIN_LDAP_SYNC_INTERVAL_SECONDS
        logger.warning(
            "ldap_sync_interval_seconds=%s is below the %ds floor; clamping",
            raw,
            MIN_LDAP_SYNC_INTERVAL_SECONDS,
            extra={
                "action": "ldap_sync_interval_clamped",
                "configured_seconds": raw,
                "floor_seconds": MIN_LDAP_SYNC_INTERVAL_SECONDS,
            },
        )
        return MIN_LDAP_SYNC_INTERVAL_SECONDS
    return raw


def effective_ldap_sync_run_stale_seconds() -> int:
    """Clamp ldap_sync_run_stale_seconds to a safe floor before the stale-run
    reaper uses it (issue #528).

    Two terms, whichever is larger:

    - MIN_LDAP_SYNC_INTERVAL_SECONDS (60), the same absolute floor the
      interval clamp uses, so a 0 or a typo like 3 can never turn the reaper
      into something that fails every run it sees.
    - twice the EFFECTIVE interval, since the interval loop starts a run
      every interval and a healthy run may legitimately outlast one tick.
      A threshold at or below the interval would let the reaper fail a run
      that is merely slow but still alive: the reaper's CAS keeps the row's
      real outcome only if the run finalizes FIRST, and this floor is what
      makes that the overwhelmingly likely order.

    The effective interval comes from effective_ldap_sync_interval_seconds
    above, the same function the production loop clamps with, so this floor
    is computed against the interval the loop will ACTUALLY use rather than
    against a re-derived copy of it.

    Shares that function's reasoning for clamping instead of validating: a
    bad tuning value must never prevent auth from booting, so it is
    corrected (with a warning) at the point of use rather than rejected by a
    pydantic ge= constraint.
    """
    effective_interval = effective_ldap_sync_interval_seconds(
        settings.ldap_sync_interval_seconds, warn=False
    )
    floor = max(MIN_LDAP_SYNC_INTERVAL_SECONDS, 2 * effective_interval)
    raw = settings.ldap_sync_run_stale_seconds
    if raw < floor:
        logger.warning(
            "ldap_sync_run_stale_seconds=%s is below the %ds floor "
            "(max of %ds and twice the %ds effective sync interval); clamping",
            raw,
            floor,
            MIN_LDAP_SYNC_INTERVAL_SECONDS,
            effective_interval,
            extra={
                "action": "ldap_sync_run_stale_seconds_clamped",
                "configured_seconds": raw,
                "floor_seconds": floor,
                "effective_interval_seconds": effective_interval,
            },
        )
        return floor
    return raw


def _absolute_url_ok(value: str, require_https: bool) -> bool:
    """True when value is an absolute URL whose scheme is https (or http when
    require_https is false), with a host, written in printable ASCII (no space
    or control character), with no userinfo (`user:pass@`, which httpx would
    send as Basic credentials), no query and no fragment (not even a bare `?`
    or `#`), and no `..` path segment (percent-encoded ones included)."""
    if not value or not (value.isascii() and all(0x20 < ord(ch) < 0x7F for ch in value)):
        return False
    try:
        parts = urlsplit(value)
    except ValueError:
        return False
    schemes = ("https",) if require_https else ("https", "http")
    return (
        parts.scheme in schemes
        and bool(parts.netloc)
        and "@" not in parts.netloc
        and "?" not in value
        and "#" not in value
        and ".." not in unquote(parts.path).split("/")
    )


def oidc_configuration_problems(config: Settings) -> list[str]:
    """The OIDC settings that are missing or invalid, by variable name.

    Empty when single sign-on can be attempted. Names only, never values: the
    list reaches a log line and OIDC_CLIENT_SECRET is one of the names. Checked:
    OIDC_ISSUER, OIDC_CLIENT_ID, OIDC_CLIENT_SECRET, and OIDC_REDIRECT_URI set;
    the issuer and the redirect URI absolute, https unless OIDC_REQUIRE_HTTPS is
    false, printable ASCII, with no userinfo, no query or fragment (not even a
    bare `?` or `#`), and no `..` path segment; the redirect URI's path ending in
    /oidc/callback (ADR 0016 D11); OIDC_SCOPES naming openid; the two claim
    names set; OIDC_CA_CERT, when set, naming an existing file. A value of white
    space only counts as missing.
    """
    problems: list[str] = []
    require_https = config.oidc_require_https
    if not _absolute_url_ok(config.oidc_issuer, require_https):
        problems.append("OIDC_ISSUER")
    if not config.oidc_client_id.strip():
        problems.append("OIDC_CLIENT_ID")
    if not config.oidc_client_secret.strip():
        problems.append("OIDC_CLIENT_SECRET")
    redirect = config.oidc_redirect_uri
    if not (
        _absolute_url_ok(redirect, require_https)
        and urlsplit(redirect).path.endswith(OIDC_CALLBACK_PATH_SUFFIX)
    ):
        problems.append("OIDC_REDIRECT_URI")
    if "openid" not in config.oidc_scopes.split():
        problems.append("OIDC_SCOPES")
    if not config.oidc_username_claim.strip():
        problems.append("OIDC_USERNAME_CLAIM")
    if not config.oidc_email_claim.strip():
        problems.append("OIDC_EMAIL_CLAIM")
    if config.oidc_ca_cert and not os.path.isfile(config.oidc_ca_cert):
        problems.append("OIDC_CA_CERT")
    return problems


def log_oidc_startup_warnings(config: Settings) -> list[str]:
    """Log the OIDC startup warnings and return their actions, in order.

    Fires only when config.auth_method == "oidc" (ADR 0016: every OIDC setting
    is consulted only in that mode), and then, each at most once:

    - `oidc_settings_incomplete` when oidc_configuration_problems is not empty,
      naming the variables (never their values); auth still boots, and single
      sign-on answers unavailable;
    - `oidc_https_not_required` when OIDC_REQUIRE_HTTPS is false;
    - `oidc_break_glass_enabled` when OIDC_BREAK_GLASS_LOCAL_SUPERADMIN is true.

    ADR 0016 phase 3 calls this from the lifespan; until auth_method admits
    "oidc" it never fires.
    """
    if config.auth_method != "oidc":
        return []
    fired: list[str] = []
    problems = oidc_configuration_problems(config)
    if problems:
        logger.warning(
            "OIDC settings missing or invalid: %s; single sign-on is unavailable "
            "until they are set",
            ", ".join(problems),
            extra={"action": "oidc_settings_incomplete", "settings": problems},
        )
        fired.append("oidc_settings_incomplete")
    if not config.oidc_require_https:
        logger.warning(
            "OIDC_REQUIRE_HTTPS is false: the issuer, its endpoints, and the "
            "redirect URI may use plain HTTP; use this only for a lab identity provider",
            extra={"action": "oidc_https_not_required"},
        )
        fired.append("oidc_https_not_required")
    if config.oidc_break_glass_local_superadmin:
        logger.warning(
            "OIDC_BREAK_GLASS_LOCAL_SUPERADMIN is true: POST /login admits local "
            "superadmin accounts with a password in single sign-on mode",
            extra={"action": "oidc_break_glass_enabled"},
        )
        fired.append("oidc_break_glass_enabled")
    return fired
