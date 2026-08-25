from herd_common.base_settings import HerdBaseSettings


class Settings(HerdBaseSettings):
    database_url: str
    db_schema: str = "user_profile"
    secret_key: str
    algorithm: str = "HS256"
    cors_origins: str = ""
    internal_api_token: str = ""

    log_level: str = "INFO"


settings = Settings()
