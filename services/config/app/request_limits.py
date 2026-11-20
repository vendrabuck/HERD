"""The config service's request body limit (issue #1466).

Every other service reads `MAX_REQUEST_BODY_BYTES` through its Settings model
(`herd_common.base_settings.HerdBaseSettings.max_request_body_bytes`), which refuses
to start on a value it does not accept. The config service has no Settings model and
must not import herd_common, so it reads the same variable from the environment here,
the way it reads `CONFIG_LOGIN_MAX_ATTEMPTS`. Both read the text the same way, with
the copy's `max_body_bytes_from_text`: ASCII decimal digits, surrounding whitespace
ignored, at least `MIN_MAX_REQUEST_BODY_BYTES` (65536). They differ only in what a
value they do not accept does: here, unset or blank is the default, and any other
value logs a warning and is the default too, so a typo cannot keep the first-startup
page from serving. Config reads only its environment: a `MAX_REQUEST_BODY_BYTES` key
written into `config.json` by hand reaches the other services' Settings but not this
reader.
"""

from __future__ import annotations

import logging
import os

from app.validation_errors import DEFAULT_MAX_REQUEST_BODY_BYTES, max_body_bytes_from_text

logger = logging.getLogger(__name__)

MAX_REQUEST_BODY_BYTES_ENV = "MAX_REQUEST_BODY_BYTES"


def max_request_body_bytes() -> int:
    """The body limit `app/main.py` installs: `MAX_REQUEST_BODY_BYTES`, or the shared
    default when it is unset, blank, or not a value `max_body_bytes_from_text`
    accepts."""
    raw = os.environ.get(MAX_REQUEST_BODY_BYTES_ENV, "")
    if not raw.strip():
        return DEFAULT_MAX_REQUEST_BODY_BYTES
    value = max_body_bytes_from_text(raw)
    if value is None:
        logger.warning(
            "%s=%r is not a whole number of at least the minimum; using %d",
            MAX_REQUEST_BODY_BYTES_ENV,
            raw,
            DEFAULT_MAX_REQUEST_BODY_BYTES,
        )
        return DEFAULT_MAX_REQUEST_BODY_BYTES
    return value
