from herd_common.base_settings import HerdBaseSettings


class Settings(HerdBaseSettings):
    database_url: str  # db login, password, and url go here via DATABASE_URL env var
    db_schema: str = "cabling"
    secret_key: str
    algorithm: str = "HS256"
    cors_origins: str = ""
    internal_api_token: str = ""
    reservations_service_url: str = "http://reservations:8000"
    inventory_service_url: str = "http://inventory:8000"
    # When true, a connection is rejected if its two devices belong to device
    # groups that share none (cross-lab cabling). Enforced at create time only;
    # existing connections are never re-validated. See docs/GAPS.md.
    enforce_device_group_boundaries: bool = True

    log_level: str = "INFO"


settings = Settings()
