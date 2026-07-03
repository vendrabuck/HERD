from herd_common.config_loader import HerdJsonConfigSource
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource


class Settings(BaseSettings):
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

    # Driver-subprocess resource limits (POSIX rlimits applied via preexec_fn).
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
