from herd_common.config_loader import HerdJsonConfigSource
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource


class Settings(BaseSettings):
    database_url: str  # db login, password, and url go here via DATABASE_URL env var
    db_schema: str = "reservations"
    secret_key: str
    algorithm: str = "HS256"
    cors_origins: str = ""
    inventory_service_url: str = "http://inventory:8000"
    auth_service_url: str = "http://auth:8000"
    execution_service_url: str = "http://execution:8000"
    cabling_service_url: str = "http://cabling:8000"
    nats_url: str = "nats://nats:4222"
    internal_api_token: str = ""
    expiration_interval_seconds: int = 60
    # Transactional outbox relay (issue #21). The relay drains unpublished outbox
    # rows to JetStream every tick and prunes published rows past the retention
    # window. Defaults match herd_common.outbox.run_outbox_relay.
    outbox_relay_tick_seconds: float = 5.0
    outbox_batch_size: int = 100
    outbox_retention_seconds: float = 7 * 24 * 3600
    # ROADMAP #40: lead window before end_time in which the expiration task
    # emits a reservation.expiring_soon reminder onto HERD_RESERVATIONS. An
    # ACTIVE reservation whose end_time is within this many seconds of now (and
    # still in the future) gets exactly one reminder, deduped via
    # expiry_reminder_sent_at. 0 disables the reminder.
    expiry_reminder_lead_seconds: int = 3600

    # Dynamic-resource provisioning backstop (ADR 0004, issue #32). A
    # PENDING_PROVISION reservation carrying dynamic requests that has not
    # received the execution service's provision-result callback within this
    # many seconds is failed by the expiration task, so a lost callback never
    # strands a reservation. 0 disables the backstop.
    provision_timeout_seconds: int = 900

    # Reservation create-time window validation.
    # A start_time earlier than now minus this grace is rejected, so a user
    # cannot book a window that already passed. The grace tolerates clock skew
    # and "start now" requests; the expiration task still activates PENDING
    # reservations whose start has ticked past.
    reservation_start_grace_seconds: int = 300
    # Maximum reservation duration (end_time - start_time). 0 disables the cap.
    reservation_max_duration_seconds: int = 30 * 24 * 3600

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
