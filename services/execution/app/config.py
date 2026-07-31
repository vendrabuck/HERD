from herd_common.base_settings import HerdBaseSettings


class Settings(HerdBaseSettings):
    database_url: str
    db_schema: str = "execution"
    secret_key: str
    algorithm: str = "HS256"
    cors_origins: str = ""
    internal_api_token: str = ""

    # Service URLs
    inventory_service_url: str = "http://inventory:8000"
    cabling_service_url: str = "http://cabling:8000"
    acl_service_url: str = "http://acl:8000"
    reservations_service_url: str = "http://reservations:8000"
    secrets_service_url: str = "http://secrets:8000"

    # NATS
    nats_url: str = "nats://nats:4222"

    # Execution settings
    driver_cache_path: str = "/data/driver-cache"
    execution_timeout_seconds: int = 30
    status_check_timeout_seconds: int = 10
    # Hypervisor recipe actions (create_instance/destroy_instance) wait on a
    # hypervisor API for minutes, not the 30s driver default (ADR 0004). This is
    # the wall-clock subprocess timeout; RLIMIT_CPU stays at 60s since waiting on
    # a remote API is not CPU time.
    recipe_timeout_seconds: int = 300

    # Recipe-package validation (ADR 0005, issue #28). The validate-package
    # internal endpoint checks unapproved (AI-drafted) packages: size cap on
    # the decoded archive, and a per-method wall-clock timeout for the
    # sandboxed dry-run lifecycle. Dry-run methods simulate and return, so
    # they get the short status-check-style timeout, not recipe_timeout.
    validate_package_max_bytes: int = 10_485_760
    validate_dry_run_timeout_seconds: int = 10

    # Driver-subprocess resource limits (POSIX rlimits applied inside the child
    # wrapper before any driver code is imported; see driver_sandbox._rlimit_pairs).
    # Each limit is applied to the driver child only; 0 leaves that limit
    # unlimited. RLIMIT_AS bounds virtual address space, so library-heavy
    # drivers (numpy/pandas/BLAS) may need it raised or set to 0 per deployment.
    driver_rlimit_as_bytes: int = 256 * 1024 * 1024
    driver_rlimit_cpu_seconds: int = 60
    driver_rlimit_nofile: int = 256
    # RLIMIT_NPROC is a per-UID ceiling that counts every thread/process the
    # service uid already has CONTAINER-WIDE, not just this child, so it must
    # clear the service's own transient baseline (uvicorn worker pool, the
    # health scheduler, asyncio executors) plus the driver's own threads. SSH
    # drivers (netmiko/paramiko) spawn worker threads; measured in-container,
    # even importing netmiko and starting one thread failed at 64 and at 256
    # ("can't start new thread") because the service baseline alone is already
    # above 256 in bursts, while 1024 has comfortable headroom. 1024 keeps a
    # real fork-bomb guard (a runaway driver still cannot exhaust the host).
    driver_rlimit_nproc: int = 1024

    # Whether a driver package's requirements.txt is pip-installed at execution
    # time. Default off: a runtime `pip install` pulls arbitrary code from the
    # network as the service uid, so it is opt-in. When off, drivers must vendor
    # their dependencies into the package's _deps/ directory instead.
    allow_driver_pip_install: bool = False

    # Health-polling scheduler (ROADMAP #13 iter 1)
    health_poll_scheduler_enabled: bool = True
    health_poll_scheduler_tick_seconds: int = 30
    health_poll_registry_refresh_seconds: int = 300
    health_poll_max_consecutive_failures: int = 3
    health_poll_backoff_cap_seconds: int = 3600
    health_poll_minimum_interval_seconds: int = 30

    # ROADMAP #13 iter 2: emit a NATS event on bad-news / recovery
    # transitions so notifications can fan out an alert. Toggle off to
    # silence alerts without rolling back the publisher code.
    health_poll_notify_enabled: bool = True

    # Fleet-scale polling (issue #24). batch_size caps how many due rows one
    # tick claims; max_concurrency caps how many driver polls (each a driver
    # subprocess) are in flight at once within this replica. Defaults match the
    # pre-#24 behavior exactly: 10 rows per tick, polls fired one at a time.
    health_poll_batch_size: int = 10
    health_poll_max_concurrency: int = 1

    # Event-driven tier intervals (issue #24): a device under an active
    # reservation polls on the in-use cadence, everything else on the idle
    # cadence, driven by the consumed reservation lifecycle events. 0 disables
    # the override so the registry-resolved interval (device or template
    # poll_interval_seconds) applies; both default 0 to preserve the pre-tier
    # cadence exactly.
    health_poll_in_use_interval_seconds: int = 0
    health_poll_idle_interval_seconds: int = 0

    # Fleet-scale template cache (issue #316). Devices sharing a template
    # (the common case) each re-fetched it from inventory on every poll,
    # so a lab with a few templates and many devices multiplied inventory
    # calls by O(polls). The health-poll path now caches a fetched template
    # by template_id for this many seconds; a miss or expiry re-fetches.
    # Matches the registry refresh cadence, since template metadata changes
    # about as rarely as the registry does.
    template_cache_ttl_seconds: int = 300

    # Run-mode split (issue #24): a poller-only replica skips mounting the HTTP
    # API routers at startup (only the bare /health liveness route remains) so
    # the same image can run a horizontally scaled poller fleet next to API
    # replicas (which set health_poll_scheduler_enabled=false).
    execution_poller_only: bool = False

    # Per-connection wiring auto-retry channel (ADR 0007 Decision 6 item 2, issue
    # #345 P3b phase 4). A background sweep reattempts hardware-retryable FAILED
    # l1_connection_assignments rows with the same in-line bounded backoff the apply
    # uses, up to a total-attempts cap; past the cap a row is parked FAILED for manual
    # retry only. Mirrors the health scheduler's run-mode posture: enabled by default
    # so a poller-only replica runs it, and set false on API replicas to keep the
    # background work on the poller fleet. batch_size bounds how many FAILED rows one
    # tick reattempts; interval is the seconds between ticks; max_attempts is the
    # cumulative driver-attempt cap a row may reach before it is manual-only.
    wiring_retry_enabled: bool = True
    wiring_retry_interval_seconds: int = 60
    wiring_retry_batch_size: int = 20
    wiring_retry_max_attempts: int = 10

    log_level: str = "INFO"


settings = Settings()
