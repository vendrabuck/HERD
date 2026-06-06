from herd_common.config_loader import HerdJsonConfigSource
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
