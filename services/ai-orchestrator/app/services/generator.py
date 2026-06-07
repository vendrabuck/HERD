"""Orchestrates inventory fetch + AI call + validation + resolution."""

import logging
from collections import Counter, defaultdict

from pydantic import ValidationError

from app.schemas.generate import ExtractedFile, GenerateResponse
from app.services.ai_client import AIClient, AIError
from app.services.extractor import render_file_context
from app.services.inventory_client import InventorySummary, fetch_available_devices
from app.services.llm_provider import Usage

logger = logging.getLogger(__name__)


class GeneratorError(Exception):
    """Raised for validation failures the caller should surface as 4xx/5xx."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


# How many times to re-prompt the model after a repairable validation failure.
# 1 retry (2 attempts total) covers the common case (a hallucinated template
# name or an over-count) without unbounded latency; weak local models that
# ignore the schema enum still get one corrective shot.
MAX_REPAIR_ATTEMPTS = 1


async def generate_topology(
    *,
    prompt: str,
    inventory: InventorySummary,
    ai: AIClient,
    user_bearer_token: str,
    extracted_files: list[ExtractedFile] | None = None,
) -> tuple[GenerateResponse, Usage]:
    extracted_files = extracted_files or []
    file_context = render_file_context(extracted_files)
    template_names = sorted(inventory.template_names)

    # Guard the impossible case: no template has an available device. Without
    # this the model is handed a "(no templates available)" prompt, dutifully
    # returns nothing usable, and the failure surfaces as an opaque empty
    # result (or a downstream 409 from _resolve_devices). Fail loudly instead.
    if not any(count > 0 for count in inventory.template_counts.values()):
        raise GeneratorError(
            409,
            "No device templates with available devices in inventory. "
            "Add devices (or run the seed) before generating a topology.",
        )

    # Propose -> schema-validate -> inventory-validate, retrying with corrective
    # feedback when the model produces a repairable mistake (unknown template,
    # over-count, duplicate role, dangling edge). Resolution (which can raise a
    # non-repairable 409 race) happens only after a clean proposal.
    repair_feedback = ""
    response: GenerateResponse | None = None
    # Accumulate token usage across every repair attempt: each attempt is a real
    # provider call that spends tokens, so the quota must see the sum, not just
    # the final successful call.
    total_usage = Usage()
    for attempt in range(MAX_REPAIR_ATTEMPTS + 1):
        try:
            raw, attempt_usage = await ai.propose_topology(
                inventory_block=inventory.to_prompt_block(),
                user_prompt=prompt,
                file_context=file_context,
                template_names=template_names,
                repair_feedback=repair_feedback,
            )
            total_usage.add(attempt_usage)
        except AIError as e:
            logger.exception("ai_error")
            raise GeneratorError(502, f"AI returned no usable response: {e}") from e
        except Exception as e:  # network / rate-limit / auth errors from the SDK
            logger.exception("ai_call_failed")
            raise GeneratorError(502, f"AI call failed: {e}") from e

        try:
            candidate = GenerateResponse.model_validate(raw)
        except ValidationError as e:
            logger.warning("ai_response_schema_violation", extra={"errors": e.errors()})
            raise GeneratorError(
                502, f"AI returned a response that did not match the expected schema: {e}"
            ) from e

        try:
            _validate_against_inventory(candidate, inventory)
        except GeneratorError as e:
            if attempt >= MAX_REPAIR_ATTEMPTS:
                raise
            logger.info("ai_proposal_repair_retry", extra={"attempt": attempt, "reason": e.message})
            repair_feedback = _repair_feedback(e.message, template_names)
            continue

        response = candidate
        break

    assert response is not None  # loop either sets response or raises
    await _resolve_devices(response, inventory, user_bearer_token)
    response.file_summaries = [
        {"filename": f.filename, "chars": len(f.text), "truncated": f.truncated}
        for f in extracted_files
    ]
    return response, total_usage


def _repair_feedback(error_message: str, template_names: list[str]) -> str:
    """Build the corrective note appended to the retry prompt."""
    allowed = ", ".join(template_names) if template_names else "(none available)"
    return (
        f"{error_message}\n"
        f"Use ONLY these template_name values, spelled exactly: {allowed}. "
        "Do not exceed the available count for any template, keep role names "
        "unique, and ensure every edge references a device role you defined."
    )


def _validate_against_inventory(response: GenerateResponse, inventory: InventorySummary) -> None:
    known = inventory.template_names
    unknown = [d.template_name for d in response.devices if d.template_name not in known]
    if unknown:
        raise GeneratorError(
            502,
            f"AI referenced unknown templates: {sorted(set(unknown))}",
        )

    per_template = Counter(d.template_name for d in response.devices)
    over = [
        f"{name} (requested {count}, available {inventory.template_counts[name]})"
        for name, count in per_template.items()
        if count > inventory.template_counts[name]
    ]
    if over:
        raise GeneratorError(
            502,
            f"AI proposed more devices than are available: {over}",
        )

    roles = [d.role for d in response.devices]
    dup_roles = [r for r, c in Counter(roles).items() if c > 1]
    if dup_roles:
        raise GeneratorError(
            502,
            f"AI returned duplicate role names: {sorted(dup_roles)}",
        )

    role_set = set(roles)
    for edge in response.edges:
        if edge.source_role not in role_set or edge.target_role not in role_set:
            raise GeneratorError(
                502,
                f"Edge references unknown role: {edge.source_role} -> {edge.target_role}",
            )


async def _resolve_devices(
    response: GenerateResponse,
    inventory: InventorySummary,
    user_bearer_token: str,
) -> None:
    """Assign a concrete AVAILABLE device to every proposed role.

    Mutates `response.devices[i].device` in place. Fetches one batch per
    template (not per role) to keep HTTP calls proportional to the number
    of distinct templates, not the number of proposed devices.
    """
    by_template: dict[str, list[int]] = defaultdict(list)
    for idx, proposed in enumerate(response.devices):
        by_template[proposed.template_name].append(idx)

    for template_name, indices in by_template.items():
        template_id = inventory.template_ids.get(template_name)
        if not template_id:
            raise GeneratorError(
                409,
                f"Inventory shifted: template '{template_name}' is no longer available",
            )
        devices = await fetch_available_devices(user_bearer_token, template_id, len(indices))
        if len(devices) < len(indices):
            raise GeneratorError(
                409,
                (
                    f"Inventory shifted during generation: '{template_name}' has "
                    f"{len(devices)} available, need {len(indices)}"
                ),
            )
        for slot, device_idx in enumerate(indices):
            response.devices[device_idx].device = devices[slot]
