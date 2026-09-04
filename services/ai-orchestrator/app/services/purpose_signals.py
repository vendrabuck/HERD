"""Signal gathering for lab purpose classification (issue #646 phase 2,
ADR 0013 points 8 to 11).

Two independent gatherers, one per pass:

- `gather_preview_signals`: the creation-pass preview endpoint's signals
  (purpose text, the topology's device names/templates and wiring shape via
  cabling + inventory using the caller's forwarded JWT, dynamic template
  names via inventory).
- `gather_internal_signals`: the end-of-reservation pass's signals (the
  same device/template/wiring/dynamic-template signals fetched through
  inventory's and cabling's internal-token endpoints, since this pass has
  no forwarded user JWT, plus duration and terminal status, config-apply
  job names and counts, the fork version count, and, when
  `settings.ai_purpose_include_transcripts` is true, this service's own
  reservation-assistant transcripts).

Every external fetch is independently wrapped: a failure (a non-2xx
response, a transport error, or a malformed body) is logged as a warning
and simply drops that signal from both the rendered prompt block and the
returned `signals_used` list. Per the fixed contract, a signal-fetch
failure must never fail the classification request.

The generation prompt for AI-built topologies is deliberately NOT a signal
here: nothing in this service or in cabling's Topology model persists the
prompt once a topology is committed (`app/services/committer.py` builds
canvas_data on the fly and never writes the prompt anywhere durable), so
there is nothing to fetch. See docs/AI_PURPOSE_CLASSIFICATION.md.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.conversation import AssistantConversation, AssistantMessage, MessageRole
from app.schemas.purpose import DynamicRequestItem

logger = logging.getLogger(__name__)

HTTP_TIMEOUT_SECONDS = 15.0
TRANSCRIPT_CHAR_BUDGET = 12_000
APPLY_JOB_NAME_DISPLAY_CAP = 20

# Canonical signal names surfaced on PurposeClassification.signals_used.
SIGNAL_PURPOSE_TEXT = "purpose_text"
SIGNAL_TOPOLOGY = "topology"
SIGNAL_DYNAMIC_TEMPLATES = "dynamic_templates"
SIGNAL_CONFIG_APPLY_JOBS = "config_apply_jobs"
SIGNAL_FORK = "fork"
SIGNAL_DURATION_STATUS = "duration_status"
SIGNAL_TRANSCRIPTS = "transcripts"


def _device_line(device: dict[str, Any]) -> str:
    name = device.get("name") or "unknown"
    template = device.get("template_name") or "unknown"
    vendor = device.get("template_vendor") or ""
    model = device.get("template_model") or ""
    identity = f" ({vendor} {model})".rstrip() if vendor or model else ""
    return f"  - {name}: template={template}{identity}"


def _layer_counts_block(layers: list[str]) -> str:
    if not layers:
        return "0 connections"
    counts: dict[str, int] = {}
    for layer in layers:
        counts[layer] = counts.get(layer, 0) + 1
    parts = ", ".join(f"{layer}: {n}" for layer, n in sorted(counts.items()))
    return f"{len(layers)} connections ({parts})"


def _device_ids_from_canvas(canvas_data: dict[str, Any] | None) -> list[str]:
    if not canvas_data:
        return []
    ids: list[str] = []
    for node in canvas_data.get("nodes") or []:
        device = (node.get("data") or {}).get("device") or {}
        device_id = device.get("id")
        if device_id:
            ids.append(str(device_id))
    return ids


def _layers_from_canvas(canvas_data: dict[str, Any] | None) -> list[str]:
    if not canvas_data:
        return []
    layers: list[str] = []
    for edge in canvas_data.get("edges") or []:
        layer = (edge.get("data") or {}).get("layer")
        layers.append(str(layer) if layer else "unknown")
    return layers


class _SignalBuilder:
    """Accumulates rendered blocks and the signal names that produced them."""

    def __init__(self) -> None:
        self.blocks: list[str] = []
        self.used: list[str] = []

    def add(self, name: str, block: str | None) -> None:
        if block is None:
            return
        self.used.append(name)
        self.blocks.append(block)

    def render(self) -> str:
        return "\n\n".join(self.blocks)


# --- Preview pass (creation, forwarded user JWT) ---


async def _fetch_topology_preview(
    client: httpx.AsyncClient, token: str, topology_id: uuid.UUID
) -> dict[str, Any] | None:
    base = settings.cabling_service_url.rstrip("/")
    resp = await client.get(
        f"{base}/topologies/{topology_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    resp.raise_for_status()
    return resp.json()


async def _fetch_devices_batch_preview(
    client: httpx.AsyncClient, token: str, device_ids: list[str]
) -> list[dict[str, Any]]:
    if not device_ids:
        return []
    base = settings.inventory_service_url.rstrip("/")
    resp = await client.post(
        f"{base}/devices/batch",
        json={"device_ids": device_ids},
        headers={"Authorization": f"Bearer {token}"},
    )
    resp.raise_for_status()
    return list(resp.json().get("items", []))


async def _fetch_template_preview(
    client: httpx.AsyncClient, token: str, template_id: uuid.UUID
) -> dict[str, Any] | None:
    base = settings.inventory_service_url.rstrip("/")
    resp = await client.get(
        f"{base}/templates/{template_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    resp.raise_for_status()
    return resp.json()


async def _gather_topology_block_preview(
    client: httpx.AsyncClient,
    token: str,
    *,
    topology_id: uuid.UUID | None,
    device_ids: list[uuid.UUID] | None,
) -> str | None:
    canvas_data: dict[str, Any] | None = None
    if topology_id is not None:
        try:
            topology = await _fetch_topology_preview(client, token, topology_id)
            canvas_data = (topology or {}).get("canvas_data")
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("purpose_signal_topology_fetch_failed: %s", exc)

    all_device_ids = list(
        dict.fromkeys(_device_ids_from_canvas(canvas_data) + [str(d) for d in (device_ids or [])])
    )
    if not all_device_ids:
        return None

    try:
        devices = await _fetch_devices_batch_preview(client, token, all_device_ids)
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("purpose_signal_devices_fetch_failed: %s", exc)
        return None

    if not devices:
        return None

    lines = ["<topology>", "  devices:"]
    lines.extend(_device_line(d) for d in devices)
    if canvas_data is not None:
        lines.append(f"  wiring: {_layer_counts_block(_layers_from_canvas(canvas_data))}")
    lines.append("</topology>")
    return "\n".join(lines)


async def _gather_dynamic_templates_block(
    fetch_template: Any,
    dynamic_requests: list[DynamicRequestItem] | None,
) -> str | None:
    if not dynamic_requests:
        return None
    lines = ["<dynamic_templates>"]
    any_resolved = False
    for item in dynamic_requests:
        try:
            template = await fetch_template(item.template_id)
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("purpose_signal_dynamic_template_fetch_failed: %s", exc)
            continue
        if not template:
            continue
        name = template.get("name") or str(item.template_id)
        lines.append(f"  - {name} x{item.count}")
        any_resolved = True
    if not any_resolved:
        return None
    lines.append("</dynamic_templates>")
    return "\n".join(lines)


async def gather_preview_signals(
    *,
    token: str,
    purpose: str | None,
    topology_id: uuid.UUID | None,
    device_ids: list[uuid.UUID] | None,
    dynamic_requests: list[DynamicRequestItem] | None,
) -> tuple[str, list[str]]:
    """Assemble the creation-pass prompt block and its signals_used list."""
    builder = _SignalBuilder()
    if purpose:
        builder.add(SIGNAL_PURPOSE_TEXT, f"<purpose_text>{purpose}</purpose_text>")

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
        topology_block = await _gather_topology_block_preview(
            client, token, topology_id=topology_id, device_ids=device_ids
        )
        builder.add(SIGNAL_TOPOLOGY, topology_block)

        async def _fetch_template(template_id: uuid.UUID) -> dict[str, Any] | None:
            return await _fetch_template_preview(client, token, template_id)

        dynamic_block = await _gather_dynamic_templates_block(_fetch_template, dynamic_requests)
        builder.add(SIGNAL_DYNAMIC_TEMPLATES, dynamic_block)

    return builder.render(), builder.used


# --- Internal pass (end of reservation, X-Internal-Token) ---


def _internal_headers() -> dict[str, str]:
    return {"X-Internal-Token": settings.internal_api_token}


async def _fetch_device_internal(
    client: httpx.AsyncClient, device_id: uuid.UUID
) -> dict[str, Any] | None:
    base = settings.inventory_service_url.rstrip("/")
    resp = await client.get(
        f"{base}/devices/{device_id}/internal",
        headers=_internal_headers(),
    )
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()


async def _fetch_template_internal(
    client: httpx.AsyncClient, template_id: uuid.UUID
) -> dict[str, Any] | None:
    base = settings.inventory_service_url.rstrip("/")
    resp = await client.get(
        f"{base}/templates/{template_id}/internal",
        headers=_internal_headers(),
    )
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()


async def _fetch_apply_jobs_summary_internal(
    client: httpx.AsyncClient, device_id: uuid.UUID
) -> dict[str, Any] | None:
    base = settings.inventory_service_url.rstrip("/")
    resp = await client.get(
        f"{base}/devices/{device_id}/apply-jobs/internal",
        headers=_internal_headers(),
    )
    resp.raise_for_status()
    return resp.json()


async def _fetch_fork_internal(
    client: httpx.AsyncClient, reservation_id: uuid.UUID
) -> dict[str, Any] | None:
    base = settings.cabling_service_url.rstrip("/")
    resp = await client.get(
        f"{base}/internal/forks/{reservation_id}",
        headers=_internal_headers(),
    )
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()


async def _gather_topology_block_internal(
    client: httpx.AsyncClient, device_ids: list[uuid.UUID]
) -> str | None:
    devices: list[dict[str, Any]] = []
    for device_id in device_ids:
        try:
            device = await _fetch_device_internal(client, device_id)
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("purpose_signal_device_fetch_failed: %s", exc)
            continue
        if device:
            devices.append(device)
    if not devices:
        return None
    lines = ["<topology>", "  devices:"]
    lines.extend(_device_line(d) for d in devices)
    lines.append("</topology>")
    return "\n".join(lines)


async def _gather_dynamic_templates_block_internal(
    client: httpx.AsyncClient, dynamic_requests: list[DynamicRequestItem] | None
) -> str | None:
    async def _fetch(template_id: uuid.UUID) -> dict[str, Any] | None:
        return await _fetch_template_internal(client, template_id)

    return await _gather_dynamic_templates_block(_fetch, dynamic_requests)


async def _gather_config_apply_jobs_block(
    client: httpx.AsyncClient, device_ids: list[uuid.UUID]
) -> str | None:
    lines = ["<config_apply_jobs>"]
    any_job = False
    for device_id in device_ids:
        try:
            summary = await _fetch_apply_jobs_summary_internal(client, device_id)
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("purpose_signal_apply_jobs_fetch_failed: %s", exc)
            continue
        if not summary or not summary.get("count"):
            continue
        any_job = True
        names = summary.get("names") or []
        names_display = ", ".join(names[:APPLY_JOB_NAME_DISPLAY_CAP]) or "(unnamed)"
        lines.append(f"  - device {device_id}: {summary['count']} jobs; names: {names_display}")
    if not any_job:
        return None
    lines.append("</config_apply_jobs>")
    return "\n".join(lines)


async def _gather_fork_block(client: httpx.AsyncClient, reservation_id: uuid.UUID) -> str | None:
    try:
        fork = await _fetch_fork_internal(client, reservation_id)
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("purpose_signal_fork_fetch_failed: %s", exc)
        return None
    if fork is None:
        return None
    connections = fork.get("connections") or []
    versions = fork.get("versions") or []
    layers = [c.get("layer") or "unknown" for c in connections]
    return (
        "<fork>\n"
        f"  wiring: {_layer_counts_block(layers)}\n"
        f"  version_count: {len(versions)}\n"
        "</fork>"
    )


def _duration_status_block(*, start_time: datetime, end_time: datetime, status: str) -> str:
    duration_hours = max(0.0, (end_time - start_time).total_seconds() / 3600.0)
    return (
        "<duration_status>\n"
        f"  status: {status}\n"
        f"  duration_hours: {duration_hours:.2f}\n"
        "</duration_status>"
    )


async def _gather_transcripts_block(db: AsyncSession, reservation_id: uuid.UUID) -> str | None:
    """Reservation-assistant transcripts for this reservation, oldest first,
    truncated to TRANSCRIPT_CHAR_BUDGET keeping the most recent turns.

    Only USER and ASSISTANT text is included; TOOL-role echo messages and
    ToolUseBlock/ToolResultBlock content are skipped, since a tool call's
    raw arguments/results are not the human-authored signal this feature
    wants and may carry device data better summarized elsewhere.
    """
    stmt = (
        select(AssistantMessage)
        .join(AssistantConversation, AssistantMessage.conversation_id == AssistantConversation.id)
        .where(AssistantConversation.reservation_id == reservation_id)
        .order_by(AssistantConversation.created_at, AssistantMessage.position)
    )
    rows = (await db.execute(stmt)).scalars().all()

    lines: list[str] = []
    for row in rows:
        if row.role not in (MessageRole.USER, MessageRole.ASSISTANT):
            continue
        text_parts = [
            block.get("text", "")
            for block in (row.content_blocks or [])
            if isinstance(block, dict) and block.get("type") == "text" and block.get("text")
        ]
        text = " ".join(text_parts).strip()
        if not text:
            continue
        role_label = "user" if row.role == MessageRole.USER else "assistant"
        lines.append(f"[{role_label}] {text}")

    if not lines:
        return None

    # Keep the most recent turns within budget, dropping from the front.
    kept: list[str] = []
    total = 0
    for line in reversed(lines):
        total += len(line) + 1
        if total > TRANSCRIPT_CHAR_BUDGET and kept:
            break
        kept.append(line)
    kept.reverse()

    return "<assistant_transcripts>\n" + "\n".join(kept) + "\n</assistant_transcripts>"


async def gather_internal_signals(
    db: AsyncSession,
    *,
    reservation_id: uuid.UUID,
    purpose: str | None,
    device_ids: list[uuid.UUID],
    dynamic_requests: list[DynamicRequestItem] | None,
    start_time: datetime,
    end_time: datetime,
    status: str,
) -> tuple[str, list[str]]:
    """Assemble the end-of-reservation prompt block and its signals_used list."""
    builder = _SignalBuilder()
    if purpose:
        builder.add(SIGNAL_PURPOSE_TEXT, f"<purpose_text>{purpose}</purpose_text>")

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
        builder.add(SIGNAL_TOPOLOGY, await _gather_topology_block_internal(client, device_ids))
        builder.add(
            SIGNAL_DYNAMIC_TEMPLATES,
            await _gather_dynamic_templates_block_internal(client, dynamic_requests),
        )
        builder.add(
            SIGNAL_CONFIG_APPLY_JOBS, await _gather_config_apply_jobs_block(client, device_ids)
        )
        builder.add(SIGNAL_FORK, await _gather_fork_block(client, reservation_id))

    builder.add(
        SIGNAL_DURATION_STATUS,
        _duration_status_block(start_time=start_time, end_time=end_time, status=status),
    )

    if settings.ai_purpose_include_transcripts:
        try:
            transcripts_block = await _gather_transcripts_block(db, reservation_id)
        except Exception as exc:  # pragma: no cover - defensive, DB errors are rare
            logger.warning("purpose_signal_transcripts_fetch_failed: %s", exc)
            transcripts_block = None
        builder.add(SIGNAL_TRANSCRIPTS, transcripts_block)

    return builder.render(), builder.used
