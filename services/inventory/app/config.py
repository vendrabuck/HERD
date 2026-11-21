from herd_common.auth_settings import AuthSecretKey, JwtAlgorithm
from herd_common.base_settings import HerdBaseSettings


class Settings(HerdBaseSettings):
    database_url: str  # db login, password, and url go here via DATABASE_URL env var
    db_schema: str = "inventory"
    secret_key: AuthSecretKey
    algorithm: JwtAlgorithm = "HS256"
    cors_origins: str = ""
    internal_api_token: str = ""
    auth_service_url: str = "http://auth:8000"
    execution_service_url: str = "http://execution:8000"
    reservations_service_url: str = "http://reservations:8000"
    cabling_service_url: str = "http://cabling:8000"
    acl_service_url: str = "http://acl:8000"
    secrets_service_url: str = "http://secrets:8000"
    apply_scheduler_interval_seconds: int = 30
    apply_scheduler_enabled: bool = True
    # How far into the future a config-apply job may be scheduled (issue
    # #704). Unbounded scheduling let a job sit queued far longer than any
    # reservation window or ACL grant is likely to remain valid, widening
    # the gap the fire-time authority re-check has to cover.
    apply_job_max_horizon_days: int = 30
    minio_endpoint: str = ""
    minio_access_key: str = ""
    minio_secret_key: str = ""
    minio_bucket: str = "herd-drivers"
    minio_use_ssl: bool = False
    driver_max_size_bytes: int = 10_485_760
    driver_storage_path: str = "/data/drivers"
    # Ceiling for a bulk device or template import upload (issue #1417). The
    # export of a real lab inventory is orders of magnitude smaller; a mistaken
    # upload (the wrong file, or an export of a huge table) is held in memory
    # several times over — bytes, decoded string, parsed objects — and then
    # parsed on the event loop, the loop every reservation and topology read
    # depends on.
    bulk_import_max_bytes: int = 10_485_760

    log_level: str = "INFO"


settings = Settings()
