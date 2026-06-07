from typing import Literal

from herd_common.config_loader import HerdJsonConfigSource
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource


class Settings(BaseSettings):
    secret_key: str = "ai-orchestrator-dev-secret"
    algorithm: str = "HS256"
    cors_origins: str = ""

    # First-time DB integration for ai-orchestrator (Branch 3: multi-turn chat).
    # Conversations and their messages live in the `ai_orchestrator` schema in
    # the shared Postgres; the rest of the service still operates statelessly.
    database_url: str = "sqlite+aiosqlite:///:memory:"
    db_schema: str = "ai_orchestrator"

    # Multi-turn assistant: bounds applied on every turn before the call.
    assistant_conversation_ttl_hours: int = 24
    assistant_max_turns: int = 40
    assistant_history_token_budget: int = 60000
    assistant_sweeper_interval_seconds: int = 3600

    # AI provider selection. "anthropic" uses the AsyncAnthropic SDK against
    # Anthropic's API; "openai_compat" uses AsyncOpenAI against any compatible
    # chat-completions endpoint (vLLM, Ollama, LM Studio, OpenAI, Azure OpenAI).
    ai_provider: Literal["anthropic", "openai_compat"] = "anthropic"
    # Required when ai_provider="openai_compat"; ignored otherwise.
    ai_base_url: str = ""
    # Canonical AI credential. If blank, ANTHROPIC_API_KEY is honored as a
    # fallback for one release with a deprecation warning at startup.
    ai_api_key: str = ""
    ai_model: str = "claude-sonnet-4-6"
    ai_max_tokens: int = 4096
    # Per-user daily token budget (input + output) across all AI features:
    # topology generation, the reservation assistant, and template-identity
    # suggestions. 0 (default) disables enforcement entirely and writes no
    # usage rows, so behavior is unchanged until an operator opts in. When
    # positive, a caller whose accumulated tokens for the current UTC day
    # already meet or exceed this value is rejected with HTTP 429 before the
    # provider is called. Counts reset implicitly on the UTC day boundary.
    ai_daily_token_quota: int = 0
    # Verify the TLS cert of ai_base_url. Set false only for an on-prem
    # openai_compat endpoint behind a self-signed cert (e.g. a local vLLM
    # server); ignored for the anthropic provider.
    ai_tls_verify: bool = True
    # Optional CA bundle path (inside the container) to verify ai_base_url
    # against, e.g. a pinned self-signed on-prem cert. Takes precedence over
    # ai_tls_verify: when set, verification stays on and fails closed, which is
    # preferable to ai_tls_verify=false for a known on-prem endpoint.
    ai_ca_cert: str = ""

    # Deprecated; honored as a fallback for ai_api_key when the canonical var
    # is blank. Remove in the release after this one.
    anthropic_api_key: str = ""

    upload_max_file_bytes: int = 5 * 1024 * 1024
    upload_max_files: int = 5
    upload_max_extracted_chars: int = 80_000

    # Reservation assistant (iter 2: agentic read-only)
    assistant_max_tool_iterations: int = 8
    assistant_tool_result_char_cap: int = 8000
    assistant_overall_deadline_s: float = 90.0
    assistant_per_call_timeout_s: float = 20.0

    # Iter 3 write tools: gated off by default so the feature lands dark and
    # can be flipped per environment once a confirmation UI is in place.
    ai_write_tools_enabled: bool = False

    inventory_service_url: str = "http://inventory:8000"
    cabling_service_url: str = "http://cabling:8000"
    reservations_service_url: str = "http://reservations:8000"
    execution_service_url: str = "http://execution:8000"

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
