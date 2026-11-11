"""Shared Pydantic Settings base class for every HERD service.

Every service-owned Settings model repeated two byte-identical blocks: a
settings_customise_sources classmethod that only forwards to
herd_settings_sources, and model_config = {"env_file": ".env",
"case_sensitive": False}. That duplication meant any future change to the
shared settings policy (adding env_prefix handling, for example) was an
11-file lockstep edit applied inconsistently (issue #384).

HerdBaseSettings centralizes both once. A service becomes
`class Settings(HerdBaseSettings)` declaring only its own fields; pydantic v2
merges model_config down the inheritance chain, so a subclass may still
override or extend it locally.
"""

from pydantic import Field
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource

from herd_common.config_loader import herd_settings_sources
from herd_common.database import (
    DEFAULT_MAX_OVERFLOW,
    DEFAULT_POOL_PRE_PING,
    DEFAULT_POOL_SIZE,
)


class HerdBaseSettings(BaseSettings):
    """Base class for every service's Settings model.

    Wires the canonical HERD settings-source order (see
    herd_common.config_loader.herd_settings_sources for the file-vs-env
    ordering rules) and the shared .env model_config once. The config
    service has no Settings model and does not use this base class.

    `hide_input_in_errors` keeps the refused value out of the text of a
    ValidationError, which a service prints when its Settings fail to load; the
    message still names the field and the rule.
    """

    model_config = {"env_file": ".env", "case_sensitive": False, "hide_input_in_errors": True}

    # Database connection pool (issue #1352), passed to herd_common.database.make_database
    # by every service's app/database.py. One process holds at most
    # db_pool_size + db_max_overflow connections; db_pool_pre_ping tests a pooled
    # connection before use, so one that died in a Postgres restart is replaced.
    db_pool_size: int = Field(default=DEFAULT_POOL_SIZE, ge=1)
    db_max_overflow: int = Field(default=DEFAULT_MAX_OVERFLOW, ge=0)
    db_pool_pre_ping: bool = DEFAULT_POOL_PRE_PING

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return herd_settings_sources(
            settings_cls,
            init_settings,
            env_settings,
            dotenv_settings,
            file_secret_settings,
        )
