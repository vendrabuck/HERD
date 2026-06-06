"""Webhook dispatcher: POST a notification to an instance-level endpoint, signed.

Peer of InAppDispatcher. POSTs the notification as a JSON body to
`settings.outbound_webhook_url` and signs the raw body with an HMAC-SHA256 over
`settings.webhook_signing_secret`, sent as `X-HERD-Signature: sha256=<hex>`. The
receiver recomputes the HMAC over the received bytes to authenticate the payload,
the standard incoming-webhook verification scheme.

The channel is "configured" only when both the URL and the signing secret are
set; an unsigned outbound webhook is never sent. Per-user opt-in is enforced
upstream; the destination is instance-level.
"""

import hashlib
import hmac
import json
import logging

import httpx

from app.config import settings
from app.services.dispatchers.base import DispatchMessage
from app.services.dispatchers.outbound import run_outbound

logger = logging.getLogger(__name__)


def _is_configured() -> bool:
    return bool(settings.outbound_webhook_url and settings.webhook_signing_secret)


def sign_body(body: bytes, secret: str) -> str:
    """Return the X-HERD-Signature value for a raw request body.

    Exposed for receivers and tests: signature = "sha256=" + hex HMAC-SHA256 of
    the exact bytes that go over the wire, keyed by the shared secret.
    """
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


class WebhookDispatcher:
    channel = "webhook"

    async def send(self, session_factory, message: DispatchMessage) -> None:
        if not _is_configured():
            logger.info(
                "Webhook channel enabled for user but outbound webhook not configured; skipping",
                extra={
                    "action": "webhook_not_configured",
                    "user_id": str(message.user_id),
                    "event_type": message.event_type,
                },
            )
            return

        payload = {
            "user_id": str(message.user_id),
            "event_type": message.event_type,
            "title": message.title,
            "body": message.body,
            "data": message.data,
            "dedupe_key": message.dedupe_key,
        }
        # Serialize once so the signature covers the exact bytes sent.
        body = json.dumps(payload, default=str, separators=(",", ":")).encode()
        signature = sign_body(body, settings.webhook_signing_secret)

        async def _do_send() -> None:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    settings.outbound_webhook_url,
                    content=body,
                    headers={
                        "Content-Type": "application/json",
                        "X-HERD-Signature": signature,
                    },
                    timeout=settings.webhook_timeout_seconds,
                )
                resp.raise_for_status()

        await run_outbound(session_factory, self.channel, message, _do_send)
