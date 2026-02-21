from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "postgresql+asyncpg://herd:herdpassword@localhost:5432/herd"
    db_schema: str = "inventory"
    secret_key: str
    algorithm: str = "HS256"
    cors_origins: str = ""
    internal_api_token: str = ""

    model_config = {"env_file": ".env", "case_sensitive": False}


settings = Settings()
