"""Gather a thin seed view of a reservation for the AI assistant.

Calls the reservations and inventory services with the caller's JWT so RBAC
and device visibility apply normally. Returns a typed seed plus a pure render
function so tests can assert structure and snapshot the rendered prompt.

The seed is the opening user message for the iter-2 agentic loop: reservation
metadata plus a flat list of devices with (id, name, template_name,
template_vendor, template_model, status).
Per-device detail, current configs, ports, execution-run history, and
pathfind answers are pulled by the model on demand via the ToolDispatcher.
"""

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


DEVICE_FETCH_CONCURRENCY = 8
GATHER_DEADLINE_SECONDS = 30.0
HTTP_TIMEOUT_SECONDS = 15.0

_SEED_RESERVATION_FIELDS = (
    "id",
    "status",
    "start_time",
    "end_time",
    "topology_id",
    "topology_type",
    "purpose",
    "owner_name",
)
_SEED_DEVICE_FIELDS = (
    "id",
    "name",
    "template_name",
    "template_vendor",
    "template_model",
    "status",
)


class ContextError(Exception):
    """Base for context-gathering errors mapped to HTTP responses by the route."""


class ContextDeadlineExceededError(ContextError):
    pass


class ReservationNotFoundError(ContextError):
    pass


@dataclass
class ReservationSeed:
    """Thin opening context: reservation metadata + flat device list.

    Per-device detail, current configs, ports, executions, and pathfind
    answers are pulled by the model on demand via the ToolDispatcher.
    """

    reservation: dict[str, Any]
    devices: list[dict[str, Any]] = field(default_factory=list)


def _whitelist(raw: dict[str, Any], allowed: tuple[str, ...]) -> dict[str, Any]:
    return {k: raw[k] for k in allowed if k in raw}


async def _fetch_reservation(
    client: httpx.AsyncClient, token: str, reservation_id: uuid.UUID
) -> dict[str, Any]:
    base = settings.reservations_service_url.rstrip("/")
    resp = await client.get(
        f"{base}/{reservation_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    if resp.status_code == 404:
        raise ReservationNotFoundError(str(reservation_id))
    resp.raise_for_status()
    return resp.json()


async def _fetch_device(
    client: httpx.AsyncClient,
    token: str,
    device_id: str,
    semaphore: asyncio.Semaphore,
) -> dict[str, Any] | None:
    base = settings.inventory_service_url.rstrip("/")
    async with semaphore:
        resp = await client.get(
            f"{base}/devices/{device_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()


async def gather_reservation_seed(token: str, reservation_id: uuid.UUID) -> ReservationSeed:
    """Fetch the thin seed bundle for the agentic assistant loop.

    Raises ReservationNotFoundError if the caller cannot read the reservation.
    Raises ContextDeadlineExceededError if the gather exceeds GATHER_DEADLINE_SECONDS.
    No topology fetch is performed; the model retrieves topology details
    through the find_path tool.
    """
    try:
        async with asyncio.timeout(GATHER_DEADLINE_SECONDS):
            return await _gather_seed_inner(token, reservation_id)
    except asyncio.TimeoutError as exc:
        raise ContextDeadlineExceededError(
            f"reservation seed gather exceeded {GATHER_DEADLINE_SECONDS}s"
        ) from exc


async def _gather_seed_inner(token: str, reservation_id: uuid.UUID) -> ReservationSeed:
    semaphore = asyncio.Semaphore(DEVICE_FETCH_CONCURRENCY)
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
        reservation_raw = await _fetch_reservation(client, token, reservation_id)
        reservation = _whitelist(reservation_raw, _SEED_RESERVATION_FIELDS)

        device_ids = reservation_raw.get("device_ids", []) or []
        device_tasks = [_fetch_device(client, token, str(did), semaphore) for did in device_ids]
        device_raws = await asyncio.gather(*device_tasks) if device_tasks else []
        devices = [_whitelist(dr, _SEED_DEVICE_FIELDS) for dr in device_raws if dr is not None]

    return ReservationSeed(reservation=reservation, devices=devices)


def render_seed_block(seed: ReservationSeed) -> str:
    """Render the seed as XML-delimited blocks for the opening user message.

    No size ceiling: the seed is bounded by the device count times a small
    constant (~40 chars per device), so even hundreds of devices fit well
    inside the model's context window.
    """
    parts: list[str] = []

    parts.append("<reservation>")
    for key in _SEED_RESERVATION_FIELDS:
        if key in seed.reservation:
            parts.append(f"  {key}: {seed.reservation[key]}")
    parts.append("</reservation>")

    parts.append("<devices>")
    if not seed.devices:
        parts.append("  (no devices)")
    for dev in seed.devices:
        fields_str = ", ".join(f"{k}={dev[k]}" for k in _SEED_DEVICE_FIELDS if k in dev)
        parts.append(f"  - {fields_str}")
    parts.append("</devices>")

    return "\n".join(parts)
