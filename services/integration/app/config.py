from herd_common.base_settings import HerdBaseSettings


class Settings(HerdBaseSettings):
    database_url: str  # db login, password, and url go here via DATABASE_URL env var
    db_schema: str = "integration"
    secret_key: str
    algorithm: str = "HS256"
    cors_origins: str = ""
    reservations_service_url: str = "http://reservations:8000"
    internal_api_token: str = ""

    # Outbound webhook delivery (issue #33, phase 4).
    nats_url: str = "nats://nats:4222"
    webhook_delivery_timeout_seconds: float = 10.0
    webhook_delivery_attempts: int = 4
    # Test-only in-network 2xx sink for the live webhook delivery test. Off by
    # default and enabled only in docker-compose.override.yml (never in prod),
    # mirroring the HERD_FAULT_INJECTION seam convention.
    webhook_test_sink_enabled: bool = False

    log_level: str = "INFO"


settings = Settings()
