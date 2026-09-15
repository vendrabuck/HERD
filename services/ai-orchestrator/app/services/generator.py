"""Orchestrates inventory fetch + AI call + validation + resolution."""

import logging
from collections import Counter, defaultdict
from typing import Any

from pydantic import ValidationError

from app.config import settings
from app.schemas.generate import ExtractedFile, GenerateResponse
from app.services.ai_client import (
    AI_PROVIDER_UNREACHABLE_DETAIL,
    AIClient,
    AIError,
    AIProviderUnavailableError,
)
from app.services.cabling_client import CablingUnavailableError, fetch_pathfind_batch
from app.services.extractor import render_file_context
from app.services.inventory_client import InventorySummary, fetch_available_devices
from app.services.llm_provider import Usage
from app.services.resolver import (
    ResolverEdge,
    ResolverPlan,
    ResolverRole,
    candidate_pairs,
    device_edges,
    plan_assignment,
    read_pathfind_results,
)

logger = logging.getLogger(__name__)

# Pinned 502 details for provider failures (issue #713). Never interpolate
# the exception: app/main.py states the rule that a provider message never
# leaves the service, and generator.py catches bare Exception, so the text
# could be anything.
AI_NO_USABLE_RESPONSE_DETAIL = "AI returned no usable response"
AI_CALL_FAILED_DETAIL = "AI call failed"


# Pinned 503 detail for a cabling outage during the feasibility check. The
# resolver fails CLOSED (see cabling_client), so an unanswerable pathfind
# stops generation rather than resolving devices that may not be cabled
# together at all.
CABLING_UNAVAILABLE_DETAIL = (
    "Could not verify cabling paths; no topology was generated. Retry the request."
)


class GeneratorError(Exception):
    """Raised for validation failures the caller should surface as 4xx/5xx.

    `detail` is the structured body the route sends instead of `message` when
    a failure carries machine-readable data the frontend renders (the
    unconnectable-topology 422 below). It stays None for every plain-string
    failure, so the route keeps its existing behavior for all of them.
    """

    def __init__(self, status_code: int, message: str, detail: object | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.detail = detail


class TopologyUnconnectableError(GeneratorError):
    """A proposal whose edges no available devices can carry (422).

    Lane's product decision: such a proposal FAILS generation. It is never
    returned flagged, and no edge is ever silently dropped, because both would
    hand the user a topology that cannot be reserved and make the cabling
    validator's later `no_path` refusal look like a different bug.

    Repairable: the model gets one corrective re-prompt naming the template
    pairs that have no cabled path, since choosing different templates is
    usually within its reach. `repair_feedback` is that note.
    """

    def __init__(self, pairs: list[dict[str, str]], message: str, repair_feedback: str) -> None:
        super().__init__(
            422,
            message,
            detail={"error": "topology_unconnectable", "pairs": pairs, "message": message},
        )
        self.pairs = pairs
        self.repair_feedback = repair_feedback


# How many times to re-prompt the model after a repairable validation failure
# is settings.ai_generate_max_repairs (env AI_GENERATE_MAX_REPAIRS, default 2).
# A hardcoded constant used to live here; it is now an operator-tunable
# setting so a weak local model that repeatedly ignores corrective feedback
# can be given more (or fewer) shots without a code change.


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

    # Propose, schema-validate, inventory-validate, then resolve to concrete
    # devices, retrying with corrective feedback when the model produces a
    # repairable mistake (unknown template, over-count, duplicate role,
    # dangling edge, or a topology the lab's cabling cannot carry). The
    # non-repairable outcomes of resolution (the 409 inventory race, the 503
    # cabling outage) are not about the proposal and propagate immediately.
    repair_feedback = ""
    response: GenerateResponse | None = None
    # Accumulate token usage across every repair attempt: each attempt is a real
    # provider call that spends tokens, so the quota must see the sum, not just
    # the final successful call.
    total_usage = Usage()
    max_repairs = settings.ai_generate_max_repairs
    for attempt in range(max_repairs + 1):
        try:
            raw, attempt_usage = await ai.propose_topology(
                inventory_block=inventory.to_prompt_block(),
                user_prompt=prompt,
                file_context=file_context,
                template_names=template_names,
                repair_feedback=repair_feedback,
            )
            total_usage.add(attempt_usage)
        except AIProviderUnavailableError as e:
            # Configured but unreachable endpoint (connect/DNS/TLS/timeout): a 503,
            # matching the issue #131 upstream-unreachable standardization, not the
            # 502 used for a live provider that returned an unusable response.
            logger.warning("ai_provider_unreachable: %s", e)
            raise GeneratorError(503, AI_PROVIDER_UNREACHABLE_DETAIL) from e
        except AIError as e:
            # Fixed detail (issue #713): the provider's status/body text stays
            # in the server log; a client never sees it (CWE-209).
            logger.exception("ai_error")
            raise GeneratorError(502, AI_NO_USABLE_RESPONSE_DETAIL) from e
        except Exception as e:  # network / rate-limit / auth errors from the SDK
            # Bare Exception: arbitrary internal exception text, so the same
            # rule applies with even more force.
            logger.exception("ai_call_failed")
            raise GeneratorError(502, AI_CALL_FAILED_DETAIL) from e

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
            if attempt >= max_repairs:
                raise
            logger.info("ai_proposal_repair_retry", extra={"attempt": attempt, "reason": e.message})
            repair_feedback = _repair_feedback(e.message, template_names)
            continue

        # Resolution runs INSIDE the loop because one of its outcomes is
        # repairable: a proposal whose edges no available devices can carry is
        # a modelling mistake the model can fix by choosing other templates.
        # Its other outcomes (the 409 inventory race, the 503 cabling outage)
        # are not about the proposal at all and propagate untouched, which is
        # why they are not caught here.
        try:
            await _resolve_devices(candidate, inventory, user_bearer_token)
        except TopologyUnconnectableError as e:
            if attempt >= max_repairs:
                raise
            logger.info(
                "ai_proposal_unconnectable_retry",
                extra={"attempt": attempt, "pairs": len(e.pairs)},
            )
            repair_feedback = e.repair_feedback
            continue

        response = candidate
        break

    assert response is not None  # loop either sets response or raises
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
        "unique across devices and elements, ensure every edge references a "
        "device or element role you defined, never connect two elements "
        "directly to each other, never connect a role to itself, and never "
        "propose the same device-to-device connection more than once."
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

    # Roles are unique across devices AND elements (D1): a device and an
    # element sharing a role name is just as ambiguous to the committer's
    # role_to_node_id map as two devices sharing one.
    device_roles = [d.role for d in response.devices]
    element_roles = [e.role for e in response.elements]
    dup_roles = [r for r, c in Counter(device_roles + element_roles).items() if c > 1]
    if dup_roles:
        raise GeneratorError(
            502,
            f"AI returned duplicate role names: {sorted(dup_roles)}",
        )

    device_role_set = set(device_roles)
    element_role_set = set(element_roles)
    role_set = device_role_set | element_role_set

    # Device-to-device edges seen so far, keyed by the unordered role pair, to
    # catch a duplicate connection between the same two devices (diagnosis
    # option 3: the committer emits one canvas edge per proposed edge with no
    # ports, and a repeated pair is never a legitimate topology). Element
    # attachments are excluded on purpose: several attachments from distinct
    # devices to the SAME element role are legal (that is the whole point of
    # a shared element), and two attachments from one device to one element
    # would already be rejected some other way (the committer only ever
    # claims one port per attachment, never producing a duplicate wire).
    seen_device_pairs: set[frozenset[str]] = set()

    for edge in response.edges:
        if edge.source_role not in role_set or edge.target_role not in role_set:
            raise GeneratorError(
                502,
                f"Edge references unknown role: {edge.source_role} to {edge.target_role}",
            )
        if edge.source_role == edge.target_role:
            raise GeneratorError(
                502,
                "AI proposed a self-loop edge, which is not allowed: a role cannot "
                f"connect to itself ({edge.source_role}).",
            )
        if edge.source_role in element_role_set and edge.target_role in element_role_set:
            raise GeneratorError(
                502,
                "AI proposed an element_to_element edge, which is not allowed: "
                f"{edge.source_role} to {edge.target_role}. Attach each element to a "
                "device instead.",
            )
        is_attachment = edge.source_role in element_role_set or edge.target_role in element_role_set
        if not is_attachment:
            pair = frozenset((edge.source_role, edge.target_role))
            if pair in seen_device_pairs:
                raise GeneratorError(
                    502,
                    "AI proposed a duplicate edge between the same two devices: "
                    f"{edge.source_role} and {edge.target_role} are already connected. "
                    "Remove the duplicate.",
                )
            seen_device_pairs.add(pair)


async def _resolve_devices(
    response: GenerateResponse,
    inventory: InventorySummary,
    user_bearer_token: str,
) -> None:
    """Assign a concrete AVAILABLE device to every proposed role.

    Mutates `response.devices[i].device` in place. Three steps:

    1. Fetch a CANDIDATE set per template (not one device per role), using the
       caller's JWT so device-group visibility keeps applying. One request per
       distinct template, not per role.
    2. Ask cabling's batch pathfinder which candidate device pairs the cable
       graph can connect, for every device-to-device edge in the proposal.
       Edges touching a network element are skipped: they never become hops,
       and the committer picks their device-side port at commit time.
    3. Search for one distinct device per role such that every edge lands on a
       reachable pair (see `resolver.plan_assignment`).

    Nothing consulted the cabling graph before this (issue: the AI builder
    diagnosis), so a proposal could resolve onto two devices with no path
    between them and only fail much later, at reservation creation, with
    cabling's `no_path`.
    """
    by_template: dict[str, list[int]] = defaultdict(list)
    for idx, proposed in enumerate(response.devices):
        by_template[proposed.template_name].append(idx)

    devices_by_template: dict[str, list[dict[str, Any]]] = {}
    for template_name, indices in by_template.items():
        template_id = inventory.template_ids.get(template_name)
        if not template_id:
            raise GeneratorError(
                409,
                f"Inventory shifted: template '{template_name}' is no longer available",
            )
        # Never fetch fewer than the number of roles: the candidate cap is a
        # search knob, and reading it as a shortfall would turn a small cap
        # into a bogus "inventory shifted" 409 on a large proposal.
        wanted = max(len(indices), settings.ai_resolver_candidates_per_template)
        devices = await fetch_available_devices(user_bearer_token, template_id, wanted)
        if len(devices) < len(indices):
            raise GeneratorError(
                409,
                (
                    f"Inventory shifted during generation: '{template_name}' has "
                    f"{len(devices)} available, need {len(indices)}"
                ),
            )
        devices_by_template[template_name] = devices

    device_by_id: dict[str, dict[str, Any]] = {}
    for devices in devices_by_template.values():
        for device in devices:
            device_by_id[str(device["id"])] = device

    roles = [
        ResolverRole(
            role=proposed.role,
            template_name=proposed.template_name,
            candidates=tuple(str(d["id"]) for d in devices_by_template[proposed.template_name]),
        )
        for proposed in response.devices
    ]
    edges = device_edges(
        [ResolverEdge(e.source_role, e.target_role) for e in response.edges],
        [r.role for r in roles],
    )

    reachable: set[tuple[str, str]] = set()
    pairs = candidate_pairs(roles, edges)
    if pairs:
        try:
            results = await fetch_pathfind_batch(user_bearer_token, pairs)
        except CablingUnavailableError as e:
            logger.warning("pathfind_unavailable: %s", e)
            raise GeneratorError(503, CABLING_UNAVAILABLE_DETAIL) from e
        reachable = read_pathfind_results(results)

    plan = plan_assignment(
        roles,
        edges,
        reachable,
        max_steps=settings.ai_resolver_max_search_steps,
    )
    if plan.assignment is None:
        raise _unconnectable_error(plan, roles)

    logger.info(
        "ai_proposal_resolved",
        extra={"roles": len(roles), "pairs": len(pairs), "search_steps": plan.steps},
    )
    for proposed in response.devices:
        proposed.device = device_by_id[plan.assignment[proposed.role]]


def _unconnectable_error(
    plan: ResolverPlan,
    roles: list[ResolverRole],
) -> TopologyUnconnectableError:
    """Build the structured 422 (and its repair note) for an infeasible plan."""
    template_by_role = {r.role: r.template_name for r in roles}
    pairs = [
        {
            "source_role": edge.source_role,
            "target_role": edge.target_role,
            "source_template": template_by_role.get(edge.source_role, ""),
            "target_template": template_by_role.get(edge.target_role, ""),
        }
        for edge in plan.unsatisfied_edges
    ]
    count = len(pairs)
    noun = "connection" if count == 1 else "connections"
    message = (
        f"The lab has no cabled path for {count} proposed {noun}; the topology "
        "cannot be built from the devices currently available."
    )
    return TopologyUnconnectableError(
        pairs=pairs,
        message=message,
        repair_feedback=_unconnectable_repair_feedback(pairs),
    )


def _unconnectable_repair_feedback(pairs: list[dict[str, str]]) -> str:
    """Corrective note for the retry prompt, one line per template pair.

    Deduplicated by template pair rather than by role pair: the model's fix is
    to pick different TEMPLATES (or drop the edge), and repeating the same
    sentence once per role pair only dilutes it.
    """
    lines: list[str] = []
    seen: set[tuple[str, str]] = set()
    for pair in pairs:
        source_template = pair["source_template"]
        target_template = pair["target_template"]
        key = (
            (source_template, target_template)
            if source_template <= target_template
            else (target_template, source_template)
        )
        if key in seen:
            continue
        seen.add(key)
        lines.append(
            f"- no cabled path exists between any available {source_template} and any "
            f"available {target_template} in this lab; choose different templates for "
            "those roles or drop the edge"
        )
    return (
        "Some proposed connections cannot be wired in this lab:\n"
        + "\n".join(lines)
        + "\nPropose a topology whose every edge connects devices the lab has cabled together."
    )
