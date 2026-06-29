"""Shared outbound-webhook signing helper.

A receiver authenticates an outbound HERD webhook by recomputing an HMAC-SHA256
over the exact request body bytes, keyed by the shared secret, and comparing it
to the `X-HERD-Signature` header. This is the standard incoming-webhook
verification scheme (GitHub, Stripe, and friends use the same shape).

Factored into herd_common so the integration service (outbound registered
webhooks, issue #33) and the notifications service (instance-level webhook
channel) sign identically. The signature is over the raw bytes that go over the
wire, so the caller must serialize once and sign and send the same bytes.
"""

import hashlib
import hmac

WEBHOOK_SIGNATURE_HEADER = "X-HERD-Signature"


def sign_body(body: bytes, secret: str) -> str:
    """Return the X-HERD-Signature value for a raw request body.

    signature = "sha256=" + hex HMAC-SHA256 of the exact bytes that go over the
    wire, keyed by the shared secret.
    """
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


__all__ = ["WEBHOOK_SIGNATURE_HEADER", "sign_body"]
