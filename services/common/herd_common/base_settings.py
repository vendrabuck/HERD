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

from pydantic_settings import BaseSettings, PydanticBaseSettingsSource

from herd_common.config_loader import herd_settings_sources


class HerdBaseSettings(BaseSettings):
    """Base class for every service's Settings model.

    Wires the canonical HERD settings-source order (see
    herd_common.config_loader.herd_settings_sources for the file-vs-env
    ordering rules) and the shared .env model_config once. The config
    service has no Settings model and does not use this base class.
    """

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
        return herd_settings_sources(
            settings_cls,
            init_settings,
            env_settings,
            dotenv_settings,
            file_secret_settings,
        )
