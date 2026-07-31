from herd_common.base_settings import HerdBaseSettings


class Settings(HerdBaseSettings):
    database_url: str  # db login, password, and url go here via DATABASE_URL env var
    db_schema: str = "acl"
    secret_key: str
    algorithm: str = "HS256"
    cors_origins: str = ""
    auth_service_url: str = "http://auth:8000"

    log_level: str = "INFO"


settings = Settings()
