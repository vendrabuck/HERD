"""Chat dispatcher: post a notification to a Slack-style incoming webhook.

Peer of InAppDispatcher. Transport is a single instance-level incoming-webhook
URL (`settings.chat_webhook_url`); the message is POSTed as JSON in the common
`{"text": ...}` shape that Slack, Mattermost, and Rocket.Chat all accept. Per
the issue, per-user chat credentials are out of scope: the channel is shared and
the per-user toggle only gates whether a user's events are forwarded.

The recipient's username is resolved from auth and prefixed so a shared channel
shows who the event is for. The channel is "configured" only when the URL is
set; otherwise it logs once and no-ops.
"""

import logging

import httpx

from app.config import settings
from app.services.contact_client import get_contact_client
from app.services.dispatchers.base import DispatchMessage
from app.services.dispatchers.outbound import run_outbound

logger = logging.getLogger(__name__)


def _is_configured() -> bool:
    return bool(settings.chat_webhook_url)


class ChatDispatcher:
    channel = "chat"

    async def send(self, session_factory, message: DispatchMessage) -> None:
        if not _is_configured():
            logger.info(
                "Chat channel enabled for user but chat webhook not configured; skipping",
                extra={
                    "action": "chat_not_configured",
                    "user_id": str(message.user_id),
                    "event_type": message.event_type,
                },
            )
            return

        # Username is best-effort context for the shared channel; the message is
        # still delivered if it cannot be resolved.
        contact = await get_contact_client().get(message.user_id)
        who = contact.username if contact else str(message.user_id)
        text = f"[{who}] {message.title}: {message.body}"

        async def _do_send() -> None:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    settings.chat_webhook_url,
                    json={"text": text},
                    timeout=settings.chat_timeout_seconds,
                )
                resp.raise_for_status()

        await run_outbound(session_factory, self.channel, message, _do_send)
