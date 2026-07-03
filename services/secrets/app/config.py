from herd_common.config_loader import HerdJsonConfigSource
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource


class Settings(BaseSettings):
    database_url: str  # db login, password, and url go here via DATABASE_URL env var
    db_schema: str = "secrets"
    secret_key: str
    algorithm: str = "HS256"
    cors_origins: str = ""
    acl_service_url: str = "http://acl:8000"
    internal_api_token: str = ""

    # Key-encryption key (issue #39): base64-encoded 32 bytes. There is
    # deliberately no default; the service refuses to boot without it (ADR 0003,
    # decision point 3, following the issue #246 lesson on defaulted key material).
    secrets_kek: str = ""
    # Optional previous KEK, set only during a KEK rotation window. At boot,
    # wrapped DEKs that fail to unwrap with secrets_kek are unwrapped with this
    # and re-wrapped with the current KEK.
    secrets_kek_previous: str = ""

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
