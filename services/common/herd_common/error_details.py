"""The ONE way a 4xx detail names a value outside a fixed set (issues #1458, #1459).

A detail built from request data must not echo the submitted value: an echo copies
whatever a caller sent (of any length) into the answer, a proxy log, and a toast.
When a value is refused because it is not one of a fixed set, the detail names the
field and the allowed values, which are the service's own data, and never the value:
`unknown_value_detail("purpose_category", allowed)` is
`Unknown purpose_category; allowed: qa_regression, other`.

An identifier the caller needs to see (a name in a 409 conflict, an id that was not
found) is not this case; such a detail may quote it.
"""

from __future__ import annotations

from collections.abc import Iterable


def unknown_value_detail(field: str, allowed: Iterable[str]) -> str:
    """`Unknown <field>; allowed: <a>, <b>`: the field and the allowed values, in the
    order given, never the submitted value."""
    return f"Unknown {field}; allowed: {', '.join(allowed)}"
