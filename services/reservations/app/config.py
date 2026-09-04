from typing import Annotated

from herd_common.base_settings import HerdBaseSettings
from pydantic import field_validator
from pydantic_settings import NoDecode


class Settings(HerdBaseSettings):
    database_url: str  # db login, password, and url go here via DATABASE_URL env var
    db_schema: str = "reservations"
    secret_key: str
    algorithm: str = "HS256"
    cors_origins: str = ""
    inventory_service_url: str = "http://inventory:8000"
    auth_service_url: str = "http://auth:8000"
    execution_service_url: str = "http://execution:8000"
    cabling_service_url: str = "http://cabling:8000"
    ai_orchestrator_service_url: str = "http://ai-orchestrator:8000"
    nats_url: str = "nats://nats:4222"
    # JetStream retention cap for HERD_RESERVATIONS (issue #620). 0 means no
    # cap. Only takes effect where JetStream state is durable (make prod, the
    # nats-data volume); the dev/test override starts every stream empty on
    # each recreate regardless of this setting.
    nats_stream_max_age_seconds: int = 7 * 24 * 3600
    internal_api_token: str = ""
    expiration_interval_seconds: int = 60
    # Transactional outbox relay (issue #21). The relay drains unpublished outbox
    # rows to JetStream every tick and prunes published rows past the retention
    # window. Defaults match herd_common.outbox.run_outbox_relay.
    outbox_relay_tick_seconds: float = 5.0
    outbox_batch_size: int = 100
    outbox_retention_seconds: float = 7 * 24 * 3600
    # Issue #682: wake the relay on a committed write (Postgres LISTEN/NOTIFY)
    # instead of waiting out the rest of the tick. True by default; the tick
    # stays the fallback cadence regardless. False is the ops escape hatch
    # back to tick-only behavior.
    outbox_wake_on_write: bool = True
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

    # Maximum span (range_end - range_start) accepted by GET /calendar (issue
    # #315). The endpoint has no LIMIT and no pagination, so an unbounded,
    # client-controlled window can load and hold an unbounded result set in
    # memory; a window wider than this is rejected with 422 rather than
    # silently loading everything. 0 disables the cap.
    calendar_max_span_days: int = 366

    # Maximum span (window_end - window_start) accepted by GET
    # /reports/utilization and /reports/utilization.csv (issue #389, the
    # deferred sibling of #315). The builder has no LIMIT and no pagination,
    # so an unbounded, client-controlled window can load and hold an
    # unbounded result set in memory; a window wider than this is rejected
    # with 422 rather than silently loading everything. 0 disables the cap.
    utilization_max_span_days: int = 366

    # Background purpose-classification sweep (issue #646 phase 2, ADR 0013
    # point 8's end-of-reservation pass). The reconciler in
    # app/tasks/expiration.py picks up to this many eligible rows (
    # purpose_classify_requested_at set, no purpose_suggestion yet, under the
    # attempt cap) per tick, oldest requested first, and calls the AI
    # orchestrator's internal classify-purpose endpoint for each.
    purpose_classify_batch_size: int = 20
    # Per-row cap on sweep attempts (a 5xx, a timeout, or a transport error
    # increments this; a 403 from the orchestrator, meaning the feature is
    # off, does NOT count as an attempt). A row at the cap is skipped until an
    # admin backfill or a taxonomy/config change makes it worth retrying by
    # hand; there is no automatic reset.
    purpose_classify_max_attempts: int = 3
    # Per-call timeout for the orchestrator's classify-purpose call.
    purpose_classify_timeout_seconds: float = 30.0

    # Lab purpose classification (issue #646 phase 1). The taxonomy is a plain
    # string list validated at write time, not a Postgres enum and not a
    # categories table (decision recorded for ADR 0013): a row keeps whatever
    # value it was written with even if that value is later dropped from this
    # list. PURPOSE_CATEGORIES overrides the whole list as a comma-separated
    # string; NoDecode stops pydantic-settings from trying to JSON-decode the
    # env value before the validator below can split it.
    purpose_categories: Annotated[list[str], NoDecode] = [
        "qa_regression",
        "support_case_replication",
        "feature_development",
        "customer_demo_poc",
        "training",
        "performance_benchmark",
        "other",
    ]

    log_level: str = "INFO"

    @field_validator("purpose_categories", mode="before")
    @classmethod
    def _split_purpose_categories(cls, v: object) -> object:
        if isinstance(v, str):
            return [item.strip() for item in v.split(",") if item.strip()]
        return v


settings = Settings()
