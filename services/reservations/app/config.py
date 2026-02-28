from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str  # db login, password, and url go here via DATABASE_URL env var
    db_schema: str = "reservations"
    secret_key: str
    algorithm: str = "HS256"
    cors_origins: str = ""
    inventory_service_url: str = "http://inventory:8000"
    nats_url: str = "nats://nats:4222"
    internal_api_token: str = ""
    expiration_interval_seconds: int = 60

    model_config = {"env_file": ".env", "case_sensitive": False}


settings = Settings()
