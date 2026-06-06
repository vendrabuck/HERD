import uuid
from datetime import datetime

from pydantic import BaseModel, Field, field_serializer


class AssistantRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=4000)
    # Branch 3: when None, the route creates a new conversation; when set,
    # the route looks up the existing conversation (404 on miss or wrong
    # owner) and appends this question as the next user turn.
    conversation_id: uuid.UUID | None = None


class ToolCallSummary(BaseModel):
    name: str
    arguments_summary: str
    duration_ms: int
    error: str | None = None


class PendingApply(BaseModel):
    """Side-channel surfaced when the assistant scheduled a dry-run apply.

    The frontend uses this to render the confirmation modal: poll the job
    until the dry-run run completes, fetch the command transcript, show it
    alongside the AI plan, and offer a Confirm button that promotes the
    dry-run into a real apply via a non-AI endpoint.
    """

    job_id: uuid.UUID
    version_id: uuid.UUID
    device_id: uuid.UUID
    dry_run: bool
    scheduled_for: datetime

    @field_serializer("job_id", "version_id", "device_id")
    def _ser_uuid(self, value: uuid.UUID) -> str:
        return str(value)


class AssistantResponse(BaseModel):
    answer: str
    model: str
    input_tokens: int
    output_tokens: int
    stop_reason: str
    tool_calls: list[ToolCallSummary] = Field(default_factory=list)
    tool_iterations: int = 0
    conversation_id: str | None = None
    pending_apply: PendingApply | None = None
