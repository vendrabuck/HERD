from herd_common.config_loader import herd_settings_sources
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource


class Settings(BaseSettings):
    database_url: str
    db_schema: str = "notifications"
    secret_key: str
    algorithm: str = "HS256"
    cors_origins: str = ""

    nats_url: str = "nats://nats:4222"
    user_profile_service_url: str = "http://user-profile:8000"
    auth_service_url: str = "http://auth:8000"
    reservations_service_url: str = "http://reservations:8000"
    internal_api_token: str = ""

    preferences_cache_ttl_seconds: int = 30
    # ROADMAP #13 iter 2: TTL for the cached list of admin user-ids used
    # as fan-out recipients for device health transitions. Admin list is
    # small and slow-changing so a longer TTL is fine.
    health_notify_admin_cache_ttl_seconds: int = 60

    # --- Outbound dispatch channels (ROADMAP #40) ---
    # Instance-level transport config for the email, chat, and webhook
    # dispatchers. Per-user opt-in lives in extras.notifications; these are
    # the shared credentials/endpoints. A channel is "configured" only when
    # its required transport settings are present; an unconfigured channel is
    # skipped (logged once) rather than erroring, so a user can opt in before
    # the operator wires up transport without breaking dispatch.

    # Email (SMTP). email_from + smtp_host are the minimum to be configured.
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_use_tls: bool = True
    smtp_timeout_seconds: float = 10.0
    email_from: str = ""

    # Chat (Slack-style incoming webhook). A single instance-level URL; the
    # message is posted as JSON. Configured when the URL is set.
    chat_webhook_url: str = ""
    chat_timeout_seconds: float = 10.0

    # Outbound webhook. POSTs the notification as JSON, HMAC-signed with
    # webhook_signing_secret (X-HERD-Signature: sha256=<hex>). Configured
    # when both URL and secret are set.
    outbound_webhook_url: str = ""
    webhook_signing_secret: str = ""
    webhook_timeout_seconds: float = 10.0

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
        return herd_settings_sources(
            settings_cls,
            init_settings,
            env_settings,
            dotenv_settings,
            file_secret_settings,
        )


settings = Settings()
