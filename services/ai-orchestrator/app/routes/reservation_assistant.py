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
import logging
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, status
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
from app.services import conversation_repo
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
    await db.commit()
    await db.refresh(conversation)

    messages = await conversation_repo.load_messages(db, conversation_id=conversation.id)

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

                # Persist every iteration of the loop as its own assistant
                # turn (plus tool_result echo when present). Ordering matters
                # so the next turn replays the same shape.
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

                # Surface the most recent scheduled_apply side-effect for the
                # frontend confirmation modal; same contract as iter 3.
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
                call_log = list(dispatcher.call_log)
    except asyncio.TimeoutError as exc:
        raise HTTPException(
            status.HTTP_504_GATEWAY_TIMEOUT,
            f"Assistant did not respond within {settings.assistant_overall_deadline_s:.0f}s",
        ) from exc
    except AIError as exc:
        logger.exception("ai_assistant_failed")
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"Assistant call failed: {exc}",
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


# --- helpers ---
