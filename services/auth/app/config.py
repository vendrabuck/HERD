from typing import Literal

from herd_common.base_settings import HerdBaseSettings


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
