"""One judgement of an execution answer to a config apply, for both apply paths.

Issue #1094. The immediate apply (`device_configs.apply_config_version`) and the
scheduled apply (`apply_scheduler.fire_job`) push the same thing to the same
execution endpoint family, so they must agree on two questions:

1. Did the apply succeed? Only a 2xx answer whose JSON body is an object carrying a
   run `status` of `SUCCESS` (any case) is a success. A missing or null status, a
   body that is not JSON, or JSON that is not an object is a failure, never a
   success by default (the scheduler's issue #720 rule, now the only rule).
2. Which version does the device have applied? `devices.current_config_version_id`
   moves to the version on every successful apply that pushed for real, whichever
   path pushed it; a dry run pushes nothing and leaves the pointer alone
   (`move_current_config_pointer`).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.device import Device

# Pinned (issues #720, #1094): tests match on these exact strings.
MALFORMED_ANSWER_ERROR = "execution returned malformed JSON"
NON_SUCCESS_ERROR = "execution returned non-success status"


@dataclass(frozen=True)
class ApplyOutcome:
    """What one execution answer means for an apply.

    `succeeded` is the one success rule. `run_status` is the run's own status in
    lower case when execution named one (`success`, `failed`, `timeout`), else
    `failed`. `run_id` is null when the answer named none or named a non-UUID.
    `error` is null on success.
    """

    succeeded: bool
    run_status: str
    run_id: uuid.UUID | None
    error: str | None

    @property
    def job_status(self) -> str:
        """The apply job's terminal status: `success` or `failed`."""
        return "success" if self.succeeded else "failed"


def _parse_run_id(value: object) -> uuid.UUID | None:
    if not value:
        return None
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError):
        return None


def judge_success_answer(resp) -> ApplyOutcome:
    """Judge a 2xx answer from execution `POST /execute` or `/execute/internal`."""
    try:
        data = resp.json()
    except ValueError:
        return ApplyOutcome(False, "failed", None, MALFORMED_ANSWER_ERROR)
    if not isinstance(data, dict):
        return ApplyOutcome(False, "failed", None, MALFORMED_ANSWER_ERROR)
    run_id = _parse_run_id(data.get("id"))
    raw_status = data.get("status")
    if raw_status is None:
        return ApplyOutcome(False, "failed", run_id, NON_SUCCESS_ERROR)
    run_status = str(raw_status).lower()
    if run_status == "success":
        return ApplyOutcome(True, "success", run_id, None)
    error = data.get("error")
    return ApplyOutcome(
        False, run_status, run_id, error if isinstance(error, str) and error else NON_SUCCESS_ERROR
    )


async def move_current_config_pointer(
    db: AsyncSession, device_id: uuid.UUID, version_id: uuid.UUID
) -> None:
    """Point the device at the version it now has applied. The caller commits."""
    await db.execute(
        update(Device)
        .where(Device.id == device_id)
        .values(current_config_version_id=version_id)
        .execution_options(synchronize_session=False)
    )
