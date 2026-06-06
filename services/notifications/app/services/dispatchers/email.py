"""Email dispatcher: send a notification as a plain-text email over SMTP.

Peer of InAppDispatcher. Transport config (SMTP host/port/credentials,
from-address) is instance-level in `app.config.settings`; the recipient address
is resolved per-user from the auth service. Per-user opt-in is enforced upstream
in the consumer's `_dispatch` (the `email` channel toggle).

The channel is "configured" only when `smtp_host` and `email_from` are set; an
unconfigured channel logs once and no-ops so a user can opt in before the
operator wires up SMTP. smtplib is blocking, so the send runs in a thread to
avoid stalling the consumer event loop.
"""

import asyncio
import logging
import smtplib
from email.message import EmailMessage

from app.config import settings
from app.services.contact_client import get_contact_client
from app.services.dispatchers.base import DispatchMessage
from app.services.dispatchers.outbound import run_outbound

logger = logging.getLogger(__name__)


def _is_configured() -> bool:
    return bool(settings.smtp_host and settings.email_from)


def _send_smtp(to_addr: str, subject: str, body: str) -> None:
    """Blocking SMTP send. Runs in a thread via asyncio.to_thread."""
    msg = EmailMessage()
    msg["From"] = settings.email_from
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.set_content(body)

    with smtplib.SMTP(
        settings.smtp_host, settings.smtp_port, timeout=settings.smtp_timeout_seconds
    ) as server:
        if settings.smtp_use_tls:
            server.starttls()
        if settings.smtp_username:
            server.login(settings.smtp_username, settings.smtp_password)
        server.send_message(msg)


class EmailDispatcher:
    channel = "email"

    async def send(self, session_factory, message: DispatchMessage) -> None:
        if not _is_configured():
            logger.info(
                "Email channel enabled for user but SMTP not configured; skipping",
                extra={
                    "action": "email_not_configured",
                    "user_id": str(message.user_id),
                    "event_type": message.event_type,
                },
            )
            return

        contact = await get_contact_client().get(message.user_id)
        if contact is None or not contact.email:
            logger.warning(
                "Could not resolve email for user; skipping email channel",
                extra={
                    "action": "email_no_recipient",
                    "user_id": str(message.user_id),
                    "event_type": message.event_type,
                },
            )
            return

        async def _do_send() -> None:
            await asyncio.to_thread(_send_smtp, contact.email, message.title, message.body)

        await run_outbound(session_factory, self.channel, message, _do_send)
