"""Re-export the device-config validator from herd_common so existing import
paths in this service keep working. The schemas + validation logic now live in
`herd_common.device_config` so the inventory service can use the same allowlist
without depending on the AI orchestrator.
"""

from herd_common.device_config import (
    ALLOWED_CONFIG_KEYS,
    CONFIG_SCHEMAS,
    ConfigValidationError,
    validate_device_config,
)

__all__ = [
    "ALLOWED_CONFIG_KEYS",
    "CONFIG_SCHEMAS",
    "ConfigValidationError",
    "validate_device_config",
]
