"""The config service's request body limit (issue #1466).

Every other service reads `MAX_REQUEST_BODY_BYTES` through its Settings model
(`herd_common.base_settings.HerdBaseSettings.max_request_body_bytes`), which refuses
to start on a value that is not a positive integer. The config service has no
Settings model and must not import herd_common, so it reads the same variable from
the environment here, the way it reads `CONFIG_LOGIN_MAX_ATTEMPTS`: unset or blank is
the default, and a value that is not a positive integer logs a warning and is the
default too, so a typo cannot keep the first-startup page from serving. The default
is the shared one, from the copy of `herd_common.validation_errors`.
"""

from __future__ import annotations

import logging
import os

from app.validation_errors import DEFAULT_MAX_REQUEST_BODY_BYTES

logger = logging.getLogger(__name__)

MAX_REQUEST_BODY_BYTES_ENV = "MAX_REQUEST_BODY_BYTES"


def max_request_body_bytes() -> int:
    """The body limit `app/main.py` installs: `MAX_REQUEST_BODY_BYTES`, or the
    shared default when it is unset, blank, or not a positive integer."""
    raw = os.environ.get(MAX_REQUEST_BODY_BYTES_ENV, "").strip()
    if not raw:
        return DEFAULT_MAX_REQUEST_BODY_BYTES
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if value < 1:
        logger.warning(
            "%s=%r is not a positive integer; using %d",
            MAX_REQUEST_BODY_BYTES_ENV,
            raw,
            DEFAULT_MAX_REQUEST_BODY_BYTES,
        )
        return DEFAULT_MAX_REQUEST_BODY_BYTES
    return value
