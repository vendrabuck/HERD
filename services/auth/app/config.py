import logging
from typing import Literal

from herd_common.base_settings import HerdBaseSettings

logger = logging.getLogger(__name__)

# Production floor for the LDAP sync interval, shared by the interval loop's
# clamp (app/tasks/ldap_sync_loop.py's effective_interval_seconds, which
# aliases this constant) and the stale-run clamp below, so the two can never
# drift apart. Defined HERE rather than imported from the loop because
# app.tasks.ldap_sync_loop imports app.services.ldap_sync_service, which
# imports this module: importing the other way would be a cycle.
MIN_LDAP_SYNC_INTERVAL_SECONDS = 60


class Settings(HerdBaseSettings):
    database_url: str  # db login, password, and url go here via DATABASE_URL env var
    db_schema: str = "auth"
    secret_key: str
    algorithm: str = "HS256"
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

    log_level: str = "INFO"


settings = Settings()


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

    Mirrors ldap_sync_loop.effective_interval_seconds deliberately, including
    its reasoning for clamping instead of validating: a bad tuning value must
    never prevent auth from booting, so it is corrected (with a warning) at
    the point of use rather than rejected by a pydantic ge= constraint.
    """
    effective_interval = max(settings.ldap_sync_interval_seconds, MIN_LDAP_SYNC_INTERVAL_SECONDS)
    floor = max(MIN_LDAP_SYNC_INTERVAL_SECONDS, 2 * effective_interval)
    raw = settings.ldap_sync_run_stale_seconds
    if raw < floor:
        logger.warning(
            "ldap_sync_run_stale_seconds=%s is below the %ds floor "
            "(max of 60s and twice the %ds effective sync interval); clamping",
            raw,
            floor,
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
