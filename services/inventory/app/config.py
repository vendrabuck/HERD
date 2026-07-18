from herd_common.config_loader import herd_settings_sources
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource


class Settings(BaseSettings):
    database_url: str  # db login, password, and url go here via DATABASE_URL env var
    db_schema: str = "inventory"
    secret_key: str
    algorithm: str = "HS256"
    cors_origins: str = ""
    internal_api_token: str = ""
    auth_service_url: str = "http://auth:8000"
    execution_service_url: str = "http://execution:8000"
    reservations_service_url: str = "http://reservations:8000"
    acl_service_url: str = "http://acl:8000"
    secrets_service_url: str = "http://secrets:8000"
    apply_scheduler_interval_seconds: int = 30
    apply_scheduler_enabled: bool = True
    minio_endpoint: str = ""
    minio_access_key: str = ""
    minio_secret_key: str = ""
    minio_bucket: str = "herd-drivers"
    minio_use_ssl: bool = False
    driver_max_size_bytes: int = 10_485_760
    driver_storage_path: str = "/data/drivers"

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
