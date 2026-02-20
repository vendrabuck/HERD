from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "postgresql+asyncpg://herd:herdpassword@localhost:5432/herd"
    db_schema: str = "reservations"
    secret_key: str
    algorithm: str = "HS256"
    cors_origins: str = ""
    inventory_service_url: str = "http://inventory:8000"
    nats_url: str = "nats://nats:4222"

    model_config = {"env_file": ".env", "case_sensitive": False}


settings = Settings()
