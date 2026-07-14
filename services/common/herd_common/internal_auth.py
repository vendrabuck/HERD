"""Shared constant-time verification for the X-Internal-Token shared secret.

Every service that gates a `/internal` endpoint with `X-Internal-Token` compares
the header value against `settings.internal_api_token`. A plain `==`/`!=` string
comparison short-circuits on the first differing byte, so the comparison time
leaks how many leading bytes of a guess match the real token: an attacker who
can measure response latency across many requests can recover the shared
secret byte by byte (issue #311). `hmac.compare_digest` runs in time
independent of where the strings first differ.

Usage in a service router:
    from herd_common.internal_auth import internal_token_matches

    if not internal_token_matches(x_internal_token, settings.internal_api_token):
        raise HTTPException(status_code=403, detail="Invalid internal token")

A service that reports a distinct status code when the server-side token is
unset (vs. a genuine mismatch) keeps that branch of its own guard and calls
this helper only for the byte comparison; see
services/auth/app/routers/internal.py and services/execution/app/routers/executions.py
for the worked examples.
"""

import hmac


def internal_token_matches(provided: str | None, configured: str | None) -> bool:
    """Return True only if `provided` equals `configured`, compared in constant time.

    Fails closed: a missing/empty `provided` header or a missing/empty
    `configured` server-side token never matches, so an unconfigured
    INTERNAL_API_TOKEN refuses every caller instead of accidentally
    authenticating one (an empty header would otherwise satisfy an empty
    configured value under a naive comparison).
    """
    if not provided or not configured:
        return False
    return hmac.compare_digest(provided, configured)


__all__ = ["internal_token_matches"]
