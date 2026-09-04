"""Lab purpose classification (issue #646 phase 1): taxonomy validation.

The taxonomy is a plain configured string list (`settings.purpose_categories`),
not a Postgres enum and not a categories table: a row keeps whatever value it
was written with even if that value is later dropped from the configured list
(decision recorded for ADR 0013). This module owns the one validation rule
every write path (reservation create, the PATCH purpose-category endpoint)
applies.
"""

from app.config import settings


def validate_purpose_category(value: str | None) -> str | None:
    """Return `value` unchanged if it is None or in the configured taxonomy.

    Raises ValueError with a pinned message otherwise, mirroring the rest of
    this service's business-rule layer (create_reservation and friends raise
    ValueError for a caller-fixable 422; the router maps it to
    HTTPException(422, detail=str(exc))).
    """
    if value is None:
        return None
    allowed = settings.purpose_categories
    if value not in allowed:
        raise ValueError(f"Unknown purpose_category '{value}'; allowed: {', '.join(allowed)}")
    return value
