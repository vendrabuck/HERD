"""The one naive-or-aware to UTC conversion (issue #1595).

Before this module the rule was written out three times (reservations' schema
module, reservations' reporting service, and notifications' end-time label),
and a fourth hand copy is how a naive value read in the host's zone, or an
aware one left unconverted, comes back (issue #1564).
"""

from datetime import datetime, timezone


def to_utc_instant(dt: datetime) -> datetime:
    """The instant `dt` names, as an aware UTC datetime.

    An aware value is converted; a naive one is taken as UTC, never as the
    host's local zone (which is what astimezone would assume). Raises
    OverflowError when the instant has no UTC equivalent (for example
    0001-01-01T00:00:00+05:00, which is before year 1 in UTC); the caller
    decides the answer. Any other input, such as None or a date that is not a
    datetime, raises AttributeError, as the three copies it replaced did.
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)
