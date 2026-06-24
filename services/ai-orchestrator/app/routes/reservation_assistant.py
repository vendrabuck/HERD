"""POST /reservations/{id}/assistant: multi-turn chat about a reservation.

Branch 3 of ROADMAP #16: the route now persists conversations in the
ai_orchestrator schema so the model can hold context across turns. The
seed is rendered once at conversation creation and pinned as the position-0
user message; each request appends either a new conversation (when
AssistantRequest.conversation_id is None) or another turn on an existing
one (404 when the caller does not own it).

The AIClient takes a pre-built messages list now, runs the same tool-use
loop, and returns segmented per-iteration content blocks that the route
persists via conversation_repo. Eviction (turn cap + token budget) runs
after persistence; the seed is pinned so the model never loses grounding.
"""

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.responses import StreamingResponse
from herd_common.auth import make_auth_dependencies
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.schemas.assistant import (
    AssistantRequest,
    AssistantResponse,
    PendingApply,
    ToolCallSummary,
)
from app.services import conversation_repo, usage_repo
from app.services.ai_client import AIClient, AIError, ai_is_configured, get_ai_client
from app.services.reservation_context import (
    ContextDeadlineExceededError,
    ReservationNotFoundError,
    ReservationSeed,
    gather_reservation_seed,
    render_seed_block,
)
from app.services.tools import ToolDispatcher

logger = logging.getLogger(__name__)

get_current_user, _require_admin = make_auth_dependencies(
    secret_key=settings.secret_key,
    algorithm=settings.algorithm,
)

router = APIRouter(prefix="/reservations", tags=["assistant"])


async def _bearer_token(authorization: str = Header(...)) -> str:
    if not authorization.lower().startswith("bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Bearer token required")
    return authorization.split(" ", 1)[1]


def get_reservation_seed_dep(
    reservation_id: uuid.UUID,
    token: str = Depends(_bearer_token),
):
    """Injectable factory: returns an awaitable the route invokes only on
    first-turn requests (subsequent turns read the persisted seed from the
    DB). Tests override this dep to return a coroutine yielding a canned
    seed, with no real HTTP hit.
    """

    async def _gather() -> ReservationSeed:
        try:
            return await gather_reservation_seed(token, reservation_id)
        except ReservationNotFoundError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Reservation not found") from exc
        except ContextDeadlineExceededError as exc:
            raise HTTPException(
                status.HTTP_504_GATEWAY_TIMEOUT,
                "Reservation seed gather exceeded its deadline",
            ) from exc

    return _gather


async def _prepare_turn(
    *,
    reservation_id: uuid.UUID,
    body: AssistantRequest,
    user_id: uuid.UUID,
    db: AsyncSession,
    seed_gatherer,
):
    """Shared setup for both the buffered and streaming endpoints.

    Enforces quota, resolves or creates the conversation, appends the new user
    turn, and returns (conversation, messages) ready for the tool loop. Raises
    HTTPException on 404/quota exactly as before so both endpoints behave the
    same up to the point where one buffers and the other streams.

    The new user turn is only FLUSHED here, never committed: the commit is
    deferred to _persist_turn, which writes the assistant reply in the same
    transaction. Keeping the whole turn in one transaction makes it atomic, if
    the AI loop fails (provider raises, returns no text, or times out) the
    session closes without committing and the user message never persists. That
    is the fix for the wedge bug: a committed orphan trailing user message would
    make the next turn's reconstructed history put two user messages in a row,
    which the provider rejects with a 400, permanently jamming the conversation.
    """
    await usage_repo.enforce_quota(db, user_id)

    # Resolve or create the conversation. A non-None conversation_id that
    # does not match (user_id, reservation_id) returns 404 to avoid leaking
    # existence; a missing one creates fresh (gather seed first since the
    # seed is the position-0 user message).
    if body.conversation_id is None:
        seed = await seed_gatherer()
        seed_block = render_seed_block(seed)
        conversation = await conversation_repo.create(
            db,
            user_id=user_id,
            reservation_id=reservation_id,
            seed_block=seed_block,
            commit=False,
        )
        is_new_conversation = True
    else:
        conversation = await conversation_repo.get_or_404(
            db,
            user_id=user_id,
            reservation_id=reservation_id,
            conversation_id=body.conversation_id,
        )
        if conversation is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Conversation not found")
        is_new_conversation = False

    # Append the new user turn before loading messages so the LLM sees the
    # latest question as the most recent user message.
    if is_new_conversation:
        # The seed message at position 0 carries the user's first question
        # too: combine seed + wrapped question into one opening user
        # message so the model has both grounding and a concrete prompt.
        await conversation_repo.set_seed_with_question(
            db, conversation=conversation, question=body.question
        )
    else:
        await conversation_repo.append_user_text(db, conversation=conversation, text=body.question)
    # Flush, do NOT commit: the user turn must persist atomically with the
    # assistant reply written in _persist_turn. load_messages reads the flushed
    # (uncommitted) rows from this same session, so the AI loop still sees the
    # full history.
    await db.flush()

    messages = await conversation_repo.load_messages(db, conversation_id=conversation.id)
    return conversation, messages


async def _persist_turn(
    *,
    db: AsyncSession,
    conversation,
    turn,
    user_id: uuid.UUID,
    question: str,
    dispatcher: ToolDispatcher,
) -> PendingApply | None:
    """Persist a completed turn's segments, evict to budget, record usage, and
    surface any pending scheduled-apply. Shared by both endpoints so the
    persistence shape is identical whether the answer was buffered or streamed.
    """
    for segment in turn.segments:
        await conversation_repo.append_assistant_turn(
            db,
            conversation=conversation,
            assistant_blocks=segment.assistant_blocks,
            tool_result_blocks=segment.tool_result_blocks or None,
        )
    await conversation_repo.evict_to_budget(db, conversation=conversation)
    await conversation_repo.touch(db, conversation=conversation)
    await db.commit()

    await usage_repo.record_usage(db, user_id, turn.usage, fallback_text=question + turn.answer)

    pending_apply: PendingApply | None = None
    for entry in reversed(dispatcher.side_effects):
        if entry.get("kind") == "scheduled_apply":
            pending_apply = PendingApply(
                job_id=entry["job_id"],
                version_id=entry["version_id"],
                device_id=entry["device_id"],
                dry_run=entry["dry_run"],
                scheduled_for=entry["scheduled_for"],
            )
            break
    return pending_apply


@router.post("/{reservation_id}/assistant", response_model=AssistantResponse)
async def reservation_assistant(
    reservation_id: uuid.UUID,
    body: AssistantRequest,
    user=Depends(get_current_user),
    token: str = Depends(_bearer_token),
    ai: AIClient = Depends(get_ai_client),
    db: AsyncSession = Depends(get_db),
    seed_gatherer=Depends(get_reservation_seed_dep),
) -> AssistantResponse:
    if not ai_is_configured():
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "AI orchestrator is not configured",
        )

    user_id = uuid.UUID(user["sub"])
    conversation, messages = await _prepare_turn(
        reservation_id=reservation_id,
        body=body,
        user_id=user_id,
        db=db,
        seed_gatherer=seed_gatherer,
    )

    pending_apply: PendingApply | None = None
    try:
        async with asyncio.timeout(settings.assistant_overall_deadline_s):
            async with ToolDispatcher(
                token=token,
                reservation_id=reservation_id,
                char_cap=settings.assistant_tool_result_char_cap,
            ) as dispatcher:
                turn = await ai.answer_reservation_question_with_tools(
                    messages=messages,
                    dispatcher=dispatcher,
                    max_iterations=settings.assistant_max_tool_iterations,
                    per_call_timeout_s=settings.assistant_per_call_timeout_s,
                )
                pending_apply = await _persist_turn(
                    db=db,
                    conversation=conversation,
                    turn=turn,
                    user_id=user_id,
                    question=body.question,
                    dispatcher=dispatcher,
                )
                call_log = list(dispatcher.call_log)
    except asyncio.TimeoutError as exc:
        # Wedge-bug fix: discard the flushed-but-uncommitted user turn so a
        # timed-out turn leaves no orphan trailing user message. If we committed
        # the user turn without the assistant reply, the next turn's reconstructed
        # message history would have two consecutive user messages, causing a 400
        # from the provider and permanently jamming the conversation.
        await db.rollback()
        raise HTTPException(
            status.HTTP_504_GATEWAY_TIMEOUT,
            f"Assistant did not respond within {settings.assistant_overall_deadline_s:.0f}s",
        ) from exc
    except AIError as exc:
        # Roll back first so the failed turn's user message never persists, then
        # log the exception detail server-side and return a generic message so a
        # backend exception string is never exposed to the client (CWE-209).
        await db.rollback()
        logger.exception("ai_assistant_failed")
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            "Assistant call failed",
        ) from exc

    tool_calls = [
        ToolCallSummary(
            name=rec.name,
            arguments_summary=rec.arguments_summary,
            duration_ms=rec.duration_ms,
            error=rec.error,
        )
        for rec in call_log
    ]

    logger.info(
        "ai_reservation_assistant",
        extra={
            "reservation_id": str(reservation_id),
            "conversation_id": str(conversation.id),
            "model": settings.ai_model,
            "input_tokens": turn.usage.input_tokens,
            "output_tokens": turn.usage.output_tokens,
            "stop_reason": turn.stop_reason,
            "question_length": len(body.question),
            "tool_iterations": turn.iteration,
            "tool_call_count": len(call_log),
            "turn_count": conversation.turn_count,
        },
    )

    return AssistantResponse(
        answer=turn.answer,
        model=settings.ai_model,
        input_tokens=turn.usage.input_tokens,
        output_tokens=turn.usage.output_tokens,
        stop_reason=turn.stop_reason,
        tool_calls=tool_calls,
        tool_iterations=turn.iteration,
        conversation_id=str(conversation.id),
        pending_apply=pending_apply,
    )


def _sse(event: str, data: dict) -> str:
    """Frame one Server-Sent Event. `data` is JSON; the client reads e.data."""
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


@router.post("/{reservation_id}/assistant/stream")
async def reservation_assistant_stream(
    reservation_id: uuid.UUID,
    body: AssistantRequest,
    user=Depends(get_current_user),
    token: str = Depends(_bearer_token),
    ai: AIClient = Depends(get_ai_client),
    db: AsyncSession = Depends(get_db),
    seed_gatherer=Depends(get_reservation_seed_dep),
) -> StreamingResponse:
    """Streaming twin of POST .../assistant.

    Emits Server-Sent Events: `status` (analyzing / running tools, with an
    `interim` flag telling the client to discard provisional tokens before a
    tool turn), `token` (final-answer text as it streams), `done` (the full
    AssistantResponse payload), and `error` (a failure after streaming began,
    where an HTTP status can no longer be set). Setup (auth, quota, conversation
    resolution) happens BEFORE the stream so 404/503/quota still surface as real
    HTTP statuses; only post-setup failures become an `error` event.
    """
    if not ai_is_configured():
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "AI orchestrator is not configured",
        )

    user_id = uuid.UUID(user["sub"])
    conversation, messages = await _prepare_turn(
        reservation_id=reservation_id,
        body=body,
        user_id=user_id,
        db=db,
        seed_gatherer=seed_gatherer,
    )

    async def _event_stream() -> AsyncIterator[str]:
        try:
            async with asyncio.timeout(settings.assistant_overall_deadline_s):
                async with ToolDispatcher(
                    token=token,
                    reservation_id=reservation_id,
                    char_cap=settings.assistant_tool_result_char_cap,
                ) as dispatcher:
                    turn = None
                    async for ev in ai.answer_reservation_question_streaming(
                        messages=messages,
                        dispatcher=dispatcher,
                        max_iterations=settings.assistant_max_tool_iterations,
                        per_call_timeout_s=settings.assistant_per_call_timeout_s,
                    ):
                        if ev.type == "status":
                            yield _sse(
                                "status",
                                {"message": ev.message, "tools": ev.tools, "interim": ev.interim},
                            )
                        elif ev.type == "token":
                            yield _sse("token", {"text": ev.text})
                        elif ev.type == "done":
                            turn = ev.result

                    if turn is None:
                        # No assistant reply assembled (stream ended without a done event):
                        # wedge-bug fix, same as the buffered endpoint. Roll back the
                        # flushed user turn so it does not persist without a reply and
                        # wedge the next turn's role alternation.
                        await db.rollback()
                        yield _sse("error", {"message": "Assistant produced no answer"})
                        return

                    # Persist inside the generator: this is the only scope where
                    # the assembled turn (from the done event), the open
                    # ToolDispatcher, and the db session all coexist. The
                    # async-with blocks (timeout and ToolDispatcher) close once the
                    # generator returns, and _persist_turn needs the dispatcher's
                    # tool-call results, so the write must happen here, before the
                    # final done frame.
                    pending_apply = await _persist_turn(
                        db=db,
                        conversation=conversation,
                        turn=turn,
                        user_id=user_id,
                        question=body.question,
                        dispatcher=dispatcher,
                    )
                    payload = AssistantResponse(
                        answer=turn.answer,
                        model=settings.ai_model,
                        input_tokens=turn.usage.input_tokens,
                        output_tokens=turn.usage.output_tokens,
                        stop_reason=turn.stop_reason,
                        tool_calls=[
                            ToolCallSummary(
                                name=rec.name,
                                arguments_summary=rec.arguments_summary,
                                duration_ms=rec.duration_ms,
                                error=rec.error,
                            )
                            for rec in dispatcher.call_log
                        ],
                        tool_iterations=turn.iteration,
                        conversation_id=str(conversation.id),
                        pending_apply=pending_apply,
                    )
                    yield _sse("done", payload.model_dump(mode="json"))
        except asyncio.TimeoutError:
            # Discard the flushed-but-uncommitted user turn so a timed-out turn
            # leaves no orphan trailing user message.
            await db.rollback()
            yield _sse(
                "error",
                {
                    "message": (
                        f"Assistant did not respond within "
                        f"{settings.assistant_overall_deadline_s:.0f}s"
                    )
                },
            )
        except AIError:
            # Log the exception detail server-side; the client-facing error frame
            # carries a generic message so no backend exception string leaks
            # (CWE-209 stack-trace exposure). Roll back first so the failed turn's
            # user message never persists.
            await db.rollback()
            logger.exception("ai_assistant_stream_failed")
            yield _sse("error", {"message": "Assistant call failed"})

    return StreamingResponse(
        _event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# --- helpers ---
