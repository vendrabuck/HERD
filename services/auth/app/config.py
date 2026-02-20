from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "postgresql+asyncpg://herd:herdpassword@localhost:5432/herd"
    db_schema: str = "auth"
    secret_key: str
    algorithm: str = "HS256"
    cors_origins: str = ""
    access_token_expire_minutes: int = 30
    refresh_token_expire_days: int = 7

    # Superadmin seed: set all three to create the single superadmin on first startup.
    # Leave any blank to skip seeding (safe default for non-production).
    superadmin_email: str = ""
    superadmin_username: str = ""
    superadmin_password: str = ""

    model_config = {"env_file": ".env", "case_sensitive": False}


settings = Settings()
