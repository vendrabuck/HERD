import logging
import uuid
from datetime import datetime

from app.services.dispatchers.base import DispatchMessage
from app.services.health_recipients import resolve_health_recipients

logger = logging.getLogger(__name__)


def _format_end_time(end_time: str | None) -> str:
    if not end_time:
        return "unknown end"
    try:
        dt = datetime.fromisoformat(end_time.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    except ValueError:
        return end_time


def _device_summary(device_ids) -> str:
    count = len(device_ids or [])
    if count == 0:
        return "no devices"
    if count == 1:
        return "1 device"
    return f"{count} devices"


def _render_created(event: dict) -> tuple[str, str]:
    title = "Reservation confirmed"
    body = (
        f"Reservation for {_device_summary(event.get('device_ids'))} is active "
        f"until {_format_end_time(event.get('end_time'))}."
    )
    return title, body


def _render_updated(event: dict) -> tuple[str, str]:
    added = event.get("added_device_ids") or []
    removed = event.get("removed_device_ids") or []
    parts: list[str] = []
    if added:
        parts.append(f"added {len(added)}")
    if removed:
        parts.append(f"removed {len(removed)}")
    if event.get("end_time"):
        parts.append(f"ends {_format_end_time(event.get('end_time'))}")
    body = "Reservation updated: " + (", ".join(parts) if parts else "details changed.")
    return "Reservation updated", body


def _render_cancelled(event: dict) -> tuple[str, str]:
    return (
        "Reservation cancelled",
        f"Reservation for {_device_summary(event.get('device_ids'))} was cancelled.",
    )


def _render_completed(event: dict) -> tuple[str, str]:
    return (
        "Reservation completed",
        f"Reservation for {_device_summary(event.get('device_ids'))} has ended.",
    )


def _render_health_transition(event: dict) -> tuple[str, str]:
    name = event.get("device_name") or "device"
    new_status = event.get("new_status", "UNKNOWN")
    if event.get("transition_kind") == "recovery":
        return (
            f"Device {name} recovered",
            f"{name} returned to {new_status} after a health failure.",
        )
    fails = event.get("consecutive_failures", "?")
    return (
        f"Device {name} unhealthy",
        f"{name} is now {new_status} after {fails} consecutive failed health checks.",
    )


_RENDERERS = {
    "reservation.created": _render_created,
    "reservation.updated": _render_updated,
    "reservation.cancelled": _render_cancelled,
    "reservation.completed": _render_completed,
    "device.health_transition": _render_health_transition,
}


def _should_emit_update(event: dict) -> bool:
    if event.get("added_device_ids") or event.get("removed_device_ids"):
        return True
    if event.get("end_time"):
        return True
    return False


async def _build_health_messages(event: dict) -> list[DispatchMessage]:
    """Fan out a device.health_transition event to admins + active holders.

    Returns zero messages if no recipients could be resolved (e.g. auth
    and reservations both unreachable). Each recipient gets one message.
    """
    raw_device_id = event.get("device_id")
    if not raw_device_id:
        logger.warning(
            "device.health_transition event missing device_id; skipping",
            extra={"action": "health_event_no_device_id"},
        )
        return []
    try:
        device_id = uuid.UUID(str(raw_device_id))
    except (ValueError, TypeError):
        logger.warning(
            "device.health_transition event has invalid device_id; skipping",
            extra={"action": "health_event_invalid_device_id"},
        )
        return []

    recipients = await resolve_health_recipients(device_id)
    if not recipients:
        return []

    title, body = _render_health_transition(event)
    return [
        DispatchMessage(
            user_id=uid,
            event_type="device.health_transition",
            title=title,
            body=body,
            data=dict(event),
        )
        for uid in recipients
    ]


async def build_messages(event: dict) -> list[DispatchMessage]:
    """Map an event into zero or more DispatchMessage records.

    Reservation events route to a single recipient (the owner). Device
    health transitions fan out to admins + active reservation holders;
    see `_build_health_messages`.
    """
    event_type = event.get("event")
    if event_type == "device.health_transition":
        return await _build_health_messages(event)

    renderer = _RENDERERS.get(event_type)
    if renderer is None:
        logger.debug("No renderer for event %s; skipping", event_type)
        return []

    if event_type == "reservation.updated" and not _should_emit_update(event):
        return []

    raw_user_id = event.get("user_id")
    if not raw_user_id:
        return []
    try:
        user_id = uuid.UUID(str(raw_user_id))
    except (ValueError, TypeError):
        logger.warning(
            "Invalid user_id in event; skipping",
            extra={"action": "event_invalid_user_id", "event": event_type},
        )
        return []

    title, body = renderer(event)
    return [
        DispatchMessage(
            user_id=user_id,
            event_type=event_type,
            title=title,
            body=body,
            data=dict(event),
        )
    ]
