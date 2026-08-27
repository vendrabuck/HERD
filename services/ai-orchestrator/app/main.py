"""
AI Orchestrator Service

Proposes lab topologies from natural-language prompts and answers free-text
questions about active reservations by calling a configurable LLM provider.
Two backends are supported via AI_PROVIDER: "anthropic" (the AsyncAnthropic
SDK against Anthropic's API) and "openai_compat" (the AsyncOpenAI SDK
against any compatible chat-completions endpoint, including vLLM, Ollama,
LM Studio, OpenAI proper, and Azure OpenAI). All three AI endpoints
(generate, suggest-identity, reservation-assistant) gate on ai_is_configured()
and 503 when the provider is not configured. The AI is called with tool_use
and a strict input_schema so the topology-generation response is structured
JSON. Proposed roles are validated against the live inventory summary and
resolved to concrete AVAILABLE devices using the caller's JWT so device
visibility rules apply.
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from herd_common.cors import add_cors_middleware
from herd_common.logging import RequestLoggingMiddleware, setup_logging
from herd_common.schema_init import create_all_and_stamp

from app.config import settings, warn_if_anthropic_api_key_unused
from app.database import Base, engine
from app.routes.commit import router as commit_router
from app.routes.generate import router as generate_router
from app.routes.quota import router as quota_router
from app.routes.recipes import router as recipes_router
from app.routes.reservation_assistant import router as reservation_assistant_router
from app.routes.template_identity import router as template_identity_router
from app.routes.usage import router as usage_router
from app.services.ai_client import ai_is_configured, get_provider_construction_status
from app.tasks.conversation_sweeper import conversation_sweeper_loop

setup_logging("ai-orchestrator", level=settings.log_level)

logger = logging.getLogger(__name__)

# Fires once at process startup, not per-request; see app/config.py.
warn_if_anthropic_api_key_unused()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Create schema + tables if missing (dev convention; production uses
    # Alembic migrations via `make migrate-ai-orchestrator`).
    if settings.db_schema and settings.database_url.startswith("postgresql"):
        from sqlalchemy import text

        async with engine.begin() as conn:
            await conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {settings.db_schema}"))
    await create_all_and_stamp(
        engine,
        Base.metadata,
        schema=settings.db_schema,
        script_location=Path(__file__).resolve().parents[1] / "migrations",
        log=logger,
    )

    sweeper_task = asyncio.create_task(
        conversation_sweeper_loop(interval_seconds=settings.assistant_sweeper_interval_seconds)
    )
    try:
        yield
    finally:
        sweeper_task.cancel()
        try:
            await sweeper_task
        except asyncio.CancelledError:
            pass


app = FastAPI(
    title="HERD AI Orchestrator Service",
    description="LLM-driven topology construction and device configuration",
    version="0.1.0",
    lifespan=lifespan,
)

add_cors_middleware(app, settings.cors_origins)

app.add_middleware(RequestLoggingMiddleware)

app.include_router(generate_router)
app.include_router(commit_router)
app.include_router(reservation_assistant_router)
app.include_router(template_identity_router)
app.include_router(quota_router)
app.include_router(usage_router)
app.include_router(recipes_router)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "ai-orchestrator"}


@app.get("/status")
async def ai_status():
    # ai_is_configured() only checks settings; a provider that looks
    # configured can still fail to CONSTRUCT (issue #280's bad ai_ca_cert
    # path, issue #592's SDK/http-client mismatch). Probe construction (cached,
    # see get_provider_construction_status) only when settings say configured,
    # so an unconfigured provider stays a plain enabled: false with no
    # construction attempt.
    configured = ai_is_configured()
    degraded = False
    reason: str | None = None
    if configured:
        degraded, reason = get_provider_construction_status()
    return {
        "enabled": configured and not degraded,
        "provider": settings.ai_provider,
        "model": settings.ai_model,
        # Conditional-UI signal for the DriversPage panel (ADR 0005): the
        # feature is usable only when this AND enabled are both true.
        "recipe_authoring": settings.ai_recipe_authoring_enabled,
        # Additive (issue #606): degraded means settings looked sufficient but
        # provider construction still failed; reason is the exception CLASS
        # NAME only, never the message, which can carry a base URL or key
        # material. Always present so the frontend/docs shape is stable.
        "degraded": degraded,
        "reason": reason,
    }
