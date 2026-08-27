"""Shared UUID coercion helper for the execution service's assignment ledgers.

Promoted from four near-identical copies (route_service.py, l1_assignment_service.py,
l2_membership_service.py, dynamic_instance_service.py) to one definition: the
dynamic_instance_service.py variant, which accepts any value (not just
`uuid.UUID | str`) and str()-wraps it before parsing, a strict superset of what the
other three copies accepted.
"""

import uuid


def as_uuid(value) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
