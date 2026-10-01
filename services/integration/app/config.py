from herd_common.base_settings import HerdBaseSettings
from herd_common.jetstream import parse_nak_backoff_schedule, validate_ack_wait_seconds
from pydantic import field_validator


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
    # for the full rationale (shared NATS_NAK_BACKOFF_SECONDS env name across
    # all three NATS-consuming services; the validator normalizes and
    # re-validates via herd_common.jetstream.parse_nak_backoff_schedule).
    nats_nak_backoff_seconds: str = "1,5,15,60,120"

    @field_validator("nats_nak_backoff_seconds", mode="before")
    @classmethod
    def _validate_nak_backoff_schedule(cls, v: object) -> str:
        return ",".join(str(n) for n in parse_nak_backoff_schedule(v))

    # Durable consumers' ack_wait in seconds (issue #944), shared env name
    # NATS_ACK_WAIT_SECONDS across execution/notifications/integration. The
    # in-progress heartbeat runs at half of it (herd_common.jetstream
    # .heartbeat_interval). Production keeps 30; docker-compose.override.yml
    # pins a short value for integration only, so a live test can outlast it.
    # Refuses to boot below herd_common.jetstream.MIN_ACK_WAIT_SECONDS.
    nats_ack_wait_seconds: int = 30

    @field_validator("nats_ack_wait_seconds")
    @classmethod
    def _validate_ack_wait(cls, v: int) -> int:
        return validate_ack_wait_seconds(v)

    webhook_delivery_timeout_seconds: float = 10.0
    webhook_delivery_attempts: int = 4
    # Test-only in-network 2xx sink for the live webhook delivery test. Off by
    # default and enabled only in docker-compose.override.yml (never in prod),
    # mirroring the HERD_FAULT_INJECTION seam convention.
    webhook_test_sink_enabled: bool = False

    log_level: str = "INFO"


settings = Settings()
