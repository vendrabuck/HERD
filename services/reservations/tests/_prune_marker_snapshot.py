"""The foreign-row comparison of the pending fork-prune live suite (issue #1522).

Kept out of `test_pending_fork_prune_live_pg.py` so the unit test of the comparison
(`test_prune_marker_snapshot.py`) runs in every suite run without importing a module
that connects to Postgres at import time.

A marker state is `SQL_NULL` or the raw stored JSON text.
"""

from __future__ import annotations

import json

SQL_NULL = "SQL NULL"


def holds_a_list(state: str) -> bool:
    """Whether a stored state is a non-empty JSON list (something pending)."""
    if state == SQL_NULL:
        return False
    try:
        value = json.loads(state)
    except ValueError:
        return False
    return isinstance(value, list) and len(value) > 0


def foreign_marker_violations(before: dict, after: dict) -> list[str]:
    """Committed foreign markers before and after a test, compared by row id.

    Only the rows in `before` are compared, so a reservation created during the test
    is ignored. Per row of `before`:

    - deleted since (absent from `after`): ignored. The suite commits nothing, so it
      cannot have deleted one (its in-transaction comparison, which is exact, would
      show it); another writer's delete is not this suite's concern.
    - held a non-empty list: may hold anything after, since the running sweeper may
      prune and clear it.
    - held any other non-NULL value (nothing actionable): unchanged, or SQL NULL, since
      the running sweeper's heal stores SQL NULL for it.
    - was SQL NULL: still SQL NULL.

    Returns one line per violating row; an empty list means the rows are as expected.
    """
    violations = []
    for row_id, was in before.items():
        if row_id not in after or holds_a_list(was):
            continue
        now = after[row_id]
        if now == was or (was != SQL_NULL and now == SQL_NULL):
            continue
        violations.append(f"{row_id}: {was!r} became {now!r}")
    return violations
