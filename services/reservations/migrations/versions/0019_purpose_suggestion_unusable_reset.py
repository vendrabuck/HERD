"""Reset stored purpose suggestions that are not a usable suggestion (issue #1543).

Before #1543 the purpose classifier stored whatever a 200 from the orchestrator
parsed to. A list, a string, or a number was stored verbatim, and every route that
read the row then answered 500 (the review list, accept, dismiss, and the owner's
`GET /{id}`). An object without a string `top_category` was stored too, and accept
with a null body then cleared the row's category. A JSON `null` stored nothing but
still set `purpose_suggested_at`. The classifier now stores a body only when it is a
usable suggestion (RES-PURPOSE-7 in `docs/specs/reservations.md`); this one-time data
pass repairs the rows
written before that, the way the issue's proposed fix (point 4) describes, so the
sweep classifies them again.

A row is reset when its stored `purpose_suggestion` is not SQL NULL and is not one the
classifier would store today (RES-PURPOSE-7's rule, frozen in `_usable`), or when it is
SQL NULL while `purpose_suggested_at` or `purpose_suggestion_dismissed_at` is set. The
stored text is not usable when Python cannot parse it (`ValueError`, such as an integer
over 4,300 digits, or `RecursionError`, nesting past the decoder's limit), when it is
anything but a JSON object whose `top_category` is a string, when it nests deeper than
32 levels (the response models refuse past about 255, and the old code stored any depth
that parsed), when it holds a number that is not finite, or when any string or key in
it holds a NUL or an unpaired surrogate (Postgres's `->` and `->>` refuse both, so one
such row made the review list fail for every admin). A reset stores
SQL NULL in all three columns; `purpose_classify_requested_at` and
`purpose_classify_attempts` are kept, so a stamped row under the attempt cap is
eligible for the sweep again (RES-PURPOSE-5) and keeps its place in the queue. The
dismissal is cleared because it declined a value that is gone: kept, it would hide
the row's next real suggestion from the review list.

The stored text is read back and judged in Python (`json.loads`), not in SQL: on
Postgres every operator that looks inside a `json` value (`->`, `->>`, `json_each`)
de-escapes it and fails on text such as `"\\u0000"`, which the `json` type stores,
and a `::jsonb` cast refuses the same text, so one such row would abort the whole
upgrade. Text that does not parse in Python counts as unusable. Rows are updated by
id in batches of `_BATCH`. Every stored suggestion's text is read once, so the pass
is linear in the number of rows that have one.

Data-only and safe to run twice: a second run matches no row. It needs a live
connection (it reads before it writes), so it refuses offline (`--sql`) mode with a
fixed message rather than emit SQL that would skip the repair.

Revision ID: 0019
Revises: 0018
Create Date: 2026-11-26 00:00:00.000000
"""

import json
import math
import os

import sqlalchemy as sa
from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None

_schema = os.environ.get("DB_SCHEMA") or None

_BATCH = 500


def _qualified(table: str) -> str:
    return f"{_schema}.{table}" if _schema else table


_MAX_DEPTH = 32


def _bad_text(text: str) -> bool:
    if "\x00" in text:
        return True
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return True
    return False


def _usable(stored_text: str) -> bool:
    """The rule the classifier applies before it stores (issue #1543), frozen here so
    a later change to the service cannot change what this revision did. Self-contained
    on purpose: it imports nothing from the service."""
    try:
        value = json.loads(stored_text)
    except (ValueError, RecursionError):
        return False
    if not isinstance(value, dict) or not isinstance(value.get("top_category"), str):
        return False
    pending = [(value, 1)]
    while pending:
        container, depth = pending.pop()
        if depth > _MAX_DEPTH:
            return False
        if isinstance(container, dict):
            if any(_bad_text(key) for key in container):
                return False
            children = container.values()
        else:
            children = container
        for child in children:
            if isinstance(child, (dict, list)):
                pending.append((child, depth + 1))
            elif isinstance(child, str):
                if _bad_text(child):
                    return False
            elif isinstance(child, float) and not math.isfinite(child):
                return False
    return True


def upgrade() -> None:
    if op.get_context().as_sql:
        raise RuntimeError(
            "Revision 0019 reads stored purpose suggestions before it resets them; "
            "run it against a live database, not in offline (--sql) mode."
        )
    bind = op.get_bind()
    table = _qualified("reservations")
    rows = bind.execute(
        sa.text(
            f"SELECT id, CAST(purpose_suggestion AS TEXT) FROM {table} "
            "WHERE purpose_suggestion IS NOT NULL"
        )
    )
    unusable = [row_id for row_id, stored_text in rows if not _usable(stored_text)]
    reset = (
        f"UPDATE {table} SET purpose_suggestion = NULL, purpose_suggested_at = NULL, "
        "purpose_suggestion_dismissed_at = NULL"
    )
    by_id = sa.text(f"{reset} WHERE id IN :ids").bindparams(sa.bindparam("ids", expanding=True))
    for start in range(0, len(unusable), _BATCH):
        bind.execute(by_id, {"ids": unusable[start : start + _BATCH]})
    bind.execute(
        sa.text(
            f"{reset} WHERE purpose_suggestion IS NULL AND "
            "(purpose_suggested_at IS NOT NULL OR purpose_suggestion_dismissed_at IS NOT NULL)"
        )
    )


def downgrade() -> None:
    # Data-only migration with no honest inverse: the values it cleared are not
    # recorded, and writing them back would restore the 500s. The code before this
    # revision reads SQL NULL as "no suggestion yet" and classifies the row again,
    # so leaving the rows as they are is correct for it. No-op.
    pass
