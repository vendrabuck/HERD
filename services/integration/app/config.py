from typing import Annotated

from herd_common.base_settings import HerdBaseSettings
from herd_common.jetstream import parse_nak_backoff_schedule
from pydantic import Field, field_validator
from pydantic_settings import NoDecode


class Settings(HerdBaseSettings):
    database_url: str  # db login, password, and url go here via DATABASE_URL env var
    db_schema: str = "integration"
    secret_key: str
    algorithm: str = "HS256"
    cors_origins: str = ""
    reservations_service_url: str = "http://reservations:8000"

    # Outbound webhook delivery (issue #33, phase 4).
    nats_url: str = "nats://nats:4222"
    # NAK-delay schedule for both durable consumers' (reservations and health)
    # transient-error branch (issue #895). See execution/app/config.py's field
    # for the full rationale (list[str] storage for compose-parity-test
    # comparability, shared NATS_NAK_BACKOFF_SECONDS env name across all three
    # NATS-consuming services, herd_common.jetstream.parse_nak_backoff_schedule
    # validates and later re-parses to list[int]).
    nats_nak_backoff_seconds: Annotated[list[str], NoDecode] = Field(
        default=["1", "5", "15", "60", "120"], validate_default=True
    )

    @field_validator("nats_nak_backoff_seconds", mode="before")
    @classmethod
    def _validate_nak_backoff_schedule(cls, v: object) -> list[str]:
        return [str(n) for n in parse_nak_backoff_schedule(v)]

    webhook_delivery_timeout_seconds: float = 10.0
    webhook_delivery_attempts: int = 4
    # Test-only in-network 2xx sink for the live webhook delivery test. Off by
    # default and enabled only in docker-compose.override.yml (never in prod),
    # mirroring the HERD_FAULT_INJECTION seam convention.
    webhook_test_sink_enabled: bool = False

    log_level: str = "INFO"


settings = Settings()
