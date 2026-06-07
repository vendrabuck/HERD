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

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from herd_common.logging import RequestLoggingMiddleware, setup_logging

from app.config import settings
from app.database import Base, engine
from app.routes.commit import router as commit_router
from app.routes.generate import router as generate_router
from app.routes.quota import router as quota_router
from app.routes.reservation_assistant import router as reservation_assistant_router
from app.routes.template_identity import router as template_identity_router
from app.services.ai_client import ai_is_configured
from app.tasks.conversation_sweeper import conversation_sweeper_loop

setup_logging("ai-orchestrator", level=settings.log_level)

logger = logging.getLogger(__name__)

if settings.anthropic_api_key and not settings.ai_api_key:
    logger.warning(
        "ANTHROPIC_API_KEY is deprecated; set AI_API_KEY instead. "
        "The legacy variable will be removed in the next release.",
        extra={"event": "anthropic_api_key_deprecated"},
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Create schema + tables if missing (dev convention; production uses
    # Alembic migrations via `make migrate-ai-orchestrator`).
    if settings.db_schema and settings.database_url.startswith("postgresql"):
        from sqlalchemy import text

        async with engine.begin() as conn:
            await conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {settings.db_schema}"))
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

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

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.cors_origins.split(",") if o.strip()],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.add_middleware(RequestLoggingMiddleware)

app.include_router(generate_router)
app.include_router(commit_router)
app.include_router(reservation_assistant_router)
app.include_router(template_identity_router)
app.include_router(quota_router)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "ai-orchestrator"}


@app.get("/status")
async def ai_status():
    return {
        "enabled": ai_is_configured(),
        "provider": settings.ai_provider,
        "model": settings.ai_model,
    }
