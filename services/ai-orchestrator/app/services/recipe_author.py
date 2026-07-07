"""Recipe drafting orchestration (ADR 0005, issue #28, phase 2).

The bounded draft-validate-repair loop: ask the model for a recipe package,
assemble it, send it to execution's internal validate-package endpoint, and
on a red report feed the errors back to the model, up to
settings.ai_recipe_max_attempts total attempts. The final draft persists
regardless of color; a red report is presentable and the reviewing admin
sees exactly what failed.

Trust properties enforced here rather than trusted from the model:
- connection_type, supports_dry_run, and provenance (generated_by,
  draft_id, generated_at) are injected by this module into the metadata; the
  model only supplies name/version/notes.
- Nothing here uploads. The archive is returned base64-encoded for the
  admin's explicit upload through inventory's existing admin endpoint.
"""

import base64
import io
import json
import logging
import uuid
import zipfile
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.recipe_draft import RecipeDraft
from app.services.llm_provider import ToolSchema, Usage

if TYPE_CHECKING:
    from app.services.ai_client import AIClient

logger = logging.getLogger(__name__)

RECIPE_VALIDATOR_UNREACHABLE_DETAIL = "Recipe validator is unreachable"

# A compact, complete reference recipe modeled on drivers/mock_hypervisor/,
# embedded so the drafting prompt is self-contained in the container (the
# repo's drivers/ directory does not ship in the image).
_REFERENCE_RECIPE = """
class Driver:
    def __init__(self, context):
        self.context = context
        self.dry_run = bool(context.get("dry_run", False))
        self.endpoint = context.get("HERD_endpoint", "")
        self.username = context.get("HERD_username", "")
        self.password = context.get("HERD_password", "")

    def login(self):
        if self.dry_run:
            return {"success": True, "simulated": True}
        # ... authenticate against self.endpoint ...
        return {"success": True}

    def logout(self):
        if self.dry_run:
            return {"success": True, "simulated": True}
        return {"success": True}

    def create_instance(self, **_):
        request_id = str(self.context.get("HERD_request_id", ""))
        if self.dry_run:
            return {
                "success": True,
                "instance_ref": "sim-" + request_id[:8],
                "field_data": {},
                "simulated": True,
            }
        # ... create the instance, idempotent on request_id ...
        return {"success": True, "instance_ref": "vm-1234", "field_data": {}}

    def destroy_instance(self, instance_ref=None, **_):
        if self.dry_run:
            return {"success": True, "simulated": True}
        # ... destroying an already-absent instance must return success ...
        return {"success": True}

    def status(self):
        if self.dry_run:
            return {"success": True, "state": "simulated"}
        return {"success": True, "state": "reachable"}
"""

RECIPE_SYSTEM_PROMPT = f"""You draft HERD service-recipe packages: driver packages that provision \
dynamic instances on a hypervisor. You produce exactly two files via the \
draft_recipe tool: the complete driver.py source and a small metadata \
subset. An administrator reviews everything you produce before it can run \
anywhere; be correct and conservative, not clever.

THE CONTRACT (violations fail validation):
- driver.py defines a top-level class named Driver with __init__(self, context) \
and the methods login, logout, create_instance, destroy_instance, status.
- create_instance returns {{"success": bool, "instance_ref": str, "field_data": dict}}. \
instance_ref is the hypervisor-side identity; field_data carries instance \
attributes (management address, etc.) for the materialized device. Make \
creation idempotent keyed on context["HERD_request_id"] where the API allows.
- destroy_instance(self, instance_ref=None, **_) must be idempotent: \
destroying an already-absent instance returns success.
- Every method that would touch the network MUST honor context["dry_run"]: \
when true, simulate, mark results simulated, and perform no I/O at all. \
Dry-run is how your draft is validated, so a draft that skips it fails.
- Standard-library imports only (urllib, json, ssl, and friends). No third \
party packages, no _deps, no requirements.txt. You MAY use \
`from driver_transcript import record_command` to record each logical \
command and response; do so, it is the transcript the reviewer reads.
- Credentials and parameters arrive ONLY via the context dict under HERD_ \
keys (HERD_endpoint, HERD_username, HERD_password, plus the template's \
field keys). NEVER hardcode a credential, token, or secret string literal.
- Optionally define a config_schema() classmethod returning a JSON Schema \
dict describing any configure vocabulary the recipe accepts.
- Return dicts, never raise for expected failures: report them as \
{{"success": False, "error": "..."}}.

REFERENCE SHAPE (a minimal valid recipe; match its structure):
{_REFERENCE_RECIPE}
Write real logic for the requested hypervisor API using stdlib HTTP \
(urllib.request) with explicit timeouts. Keep the code readable; the \
reviewer must be able to audit every line."""

DRAFT_RECIPE_TOOL = ToolSchema(
    name="draft_recipe",
    description=(
        "Return the complete recipe package: the full driver.py source, the "
        "metadata subset, and a short explanation for the reviewing "
        "administrator."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "driver_py": {
                "type": "string",
                "description": "The complete driver.py file contents.",
            },
            "driver_metadata": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "version": {"type": "string"},
                    "notes": {"type": "string"},
                },
                "required": ["name", "version"],
            },
            "explanation": {
                "type": "string",
                "description": (
                    "What the recipe does, which API endpoints it calls, and "
                    "anything the reviewer should verify by hand."
                ),
            },
        },
        "required": ["driver_py", "driver_metadata", "explanation"],
    },
)


class RecipeAuthorError(Exception):
    """Drafting failed in a way the route maps to an HTTP status."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def build_metadata(model_metadata: dict, draft_id: uuid.UUID) -> dict:
    """The final driver_metadata.json: model-supplied subset plus owned fields.

    connection_type, supports_dry_run, and provenance are ALWAYS this
    service's values; a model emitting them differently is overwritten, so
    the generated-recipe contract cannot be weakened from the prompt side.
    """
    metadata = {
        "name": str(model_metadata.get("name") or "generated-recipe"),
        "version": str(model_metadata.get("version") or "0.1.0"),
        "connection_type": "Hypervisor",
        "supports_dry_run": True,
        "generated_by": settings.ai_model,
        "draft_id": str(draft_id),
        "generated_at": datetime.now(UTC).isoformat(),
    }
    notes = model_metadata.get("notes")
    if notes:
        metadata["notes"] = str(notes)
    return metadata


def assemble_package_b64(driver_py: str, metadata: dict) -> str:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("driver.py", driver_py)
        zf.writestr("driver_metadata.json", json.dumps(metadata, indent=2) + "\n")
    return base64.b64encode(buf.getvalue()).decode()


async def validate_with_execution(package_b64: str) -> dict:
    """POST the package to execution's internal validate-package endpoint.

    Raises RecipeAuthorError(503) when the validator is unreachable or
    errors; a draft must never be presented as reviewed when it was not
    validated.
    """
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(
                f"{settings.execution_service_url}/internal/validate-package",
                json={"package_b64": package_b64, "connection_type": "Hypervisor"},
                headers={"X-Internal-Token": settings.internal_api_token},
            )
    except httpx.HTTPError as exc:
        logger.warning("recipe validator unreachable: %s", exc)
        raise RecipeAuthorError(503, RECIPE_VALIDATOR_UNREACHABLE_DETAIL) from exc
    if resp.status_code != 200:
        logger.warning("recipe validator returned %s: %s", resp.status_code, resp.text[:300])
        raise RecipeAuthorError(503, RECIPE_VALIDATOR_UNREACHABLE_DETAIL)
    return resp.json()


def _report_feedback(report: dict) -> str:
    """Flatten a red validation report into repair-prompt feedback."""
    lines: list[str] = []
    for section in ("structural", "policy"):
        for err in (report.get(section) or {}).get("errors", []):
            lines.append(f"{section}: {err}")
    dry_run = report.get("dry_run") or {}
    for method in dry_run.get("methods", []):
        if not method.get("passed"):
            detail = method.get("error")
            output = method.get("output")
            if detail is None and isinstance(output, dict):
                detail = output.get("error")
            lines.append(f"dry_run: {method.get('action')} failed: {detail or 'no error detail'}")
    if not lines:
        lines.append("validation failed; see the full report")
    return "\n".join(lines)


async def author_recipe(
    ai: "AIClient",
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    prompt: str,
    hypervisor_type: str | None = None,
    existing: RecipeDraft | None = None,
    admin_feedback: str = "",
) -> tuple[RecipeDraft, Usage]:
    """Run the bounded draft-validate-repair loop and persist the result.

    A fresh call inserts a new RecipeDraft; a refine call (existing given)
    updates it in place, seeding the model with the stored files plus the
    admin's feedback. Returns the persisted draft and the accumulated Usage
    across every attempt so the route meters all of them.
    """
    draft_id = existing.id if existing else uuid.uuid4()
    total_usage = Usage()

    previous_py = existing.driver_py if existing else None
    previous_meta = existing.driver_metadata_json if existing else None
    feedback = admin_feedback

    driver_py = ""
    metadata: dict = {}
    explanation: str | None = None
    report: dict | None = None
    attempts = 0

    for attempt in range(1, settings.ai_recipe_max_attempts + 1):
        attempts = attempt
        data, usage = await ai.draft_recipe(
            prompt=prompt,
            hypervisor_type=hypervisor_type,
            previous_driver_py=previous_py,
            previous_metadata_json=previous_meta,
            repair_feedback=feedback,
        )
        total_usage.add(usage)

        driver_py = str(data.get("driver_py") or "")
        explanation = str(data.get("explanation") or "") or None
        model_metadata = data.get("driver_metadata")
        metadata = build_metadata(
            model_metadata if isinstance(model_metadata, dict) else {}, draft_id
        )

        package_b64 = assemble_package_b64(driver_py, metadata)
        report = await validate_with_execution(package_b64)
        if report.get("valid"):
            break

        # Seed the next round with this attempt's files and the report.
        previous_py = driver_py
        previous_meta = json.dumps(metadata)
        feedback = _report_feedback(report)
        logger.info(
            "recipe draft attempt %s failed validation",
            attempt,
            extra={"draft_id": str(draft_id)},
        )

    valid = bool(report and report.get("valid"))
    if existing:
        draft = existing
        draft.prompt = prompt
        draft.hypervisor_type = hypervisor_type or draft.hypervisor_type
        draft.driver_py = driver_py
        draft.driver_metadata_json = json.dumps(metadata)
        draft.explanation = explanation
        draft.validation_json = json.dumps(report) if report is not None else None
        draft.valid = valid
        draft.attempts = draft.attempts + attempts
        draft.model = settings.ai_model
    else:
        draft = RecipeDraft(
            id=draft_id,
            user_id=user_id,
            prompt=prompt,
            hypervisor_type=hypervisor_type,
            driver_py=driver_py,
            driver_metadata_json=json.dumps(metadata),
            explanation=explanation,
            validation_json=json.dumps(report) if report is not None else None,
            valid=valid,
            attempts=attempts,
            model=settings.ai_model,
        )
        db.add(draft)
    await db.commit()
    await db.refresh(draft)

    logger.info(
        "recipe draft persisted",
        extra={"draft_id": str(draft.id), "valid": draft.valid, "attempts": attempts},
    )
    return draft, total_usage
