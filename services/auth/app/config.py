from typing import Literal

from herd_common.config_loader import HerdJsonConfigSource
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource


class Settings(BaseSettings):
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

    log_level: str = "INFO"

    model_config = {"env_file": ".env", "case_sensitive": False}

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            HerdJsonConfigSource(settings_cls),
            file_secret_settings,
        )


settings = Settings()
