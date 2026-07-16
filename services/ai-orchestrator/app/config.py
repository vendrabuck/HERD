from typing import Literal

from herd_common.config_loader import HerdJsonConfigSource
from pydantic import field_validator
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
    # Idle-conversation retention (issue #338): conversations, including the
    # persisted question text in assistant_messages.content_blocks, live
    # until this many hours pass with no activity, then the sweeper
    # (app/tasks/conversation_sweeper.py) deletes them. Must stay positive;
    # 0 or negative does not mean "never retain", it would mean every
    # conversation is eligible for deletion the instant it is created, which
    # is rejected below rather than silently honored.
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
    # Canonical AI credential. Required for the hosted Anthropic API; a local
    # keyless Anthropic-compatible endpoint (vLLM) needs only ai_base_url.
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

    # AI-assisted recipe authoring (ADR 0005, issue #28). Default off: the
    # feature asks an LLM to draft code that will run against lab
    # infrastructure after admin approval, so an operator must opt in per
    # deployment. Enforced at the route boundary, mirroring
    # ai_write_tools_enabled.
    ai_recipe_authoring_enabled: bool = False
    # Bounded auto-repair: total drafting attempts (the initial draft plus
    # repair rounds fed the validator's report) one request may spend.
    ai_recipe_max_attempts: int = 3
    # The recipe validator is execution's internal endpoint, authenticated
    # with X-Internal-Token; this service historically ran on forwarded user
    # JWTs only, so the token is new here and must match the stack-wide value.
    internal_api_token: str = ""

    inventory_service_url: str = "http://inventory:8000"
    cabling_service_url: str = "http://cabling:8000"
    reservations_service_url: str = "http://reservations:8000"
    execution_service_url: str = "http://execution:8000"

    log_level: str = "INFO"

    model_config = {"env_file": ".env", "case_sensitive": False}

    @field_validator("assistant_conversation_ttl_hours")
    @classmethod
    def _validate_assistant_conversation_ttl_hours(cls, v: int) -> int:
        if v <= 0:
            raise ValueError(
                "assistant_conversation_ttl_hours must be a positive integer "
                f"(got {v}); 0 or negative would expire every conversation instantly"
            )
        return v

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
