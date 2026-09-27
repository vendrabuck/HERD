"""CSV formula-injection neutralization, shared by every backend CSV writer.

Issue #910: a CSV cell whose text begins with a spreadsheet formula trigger
(``=``, ``+``, ``-``, ``@``, a tab, or a carriage return) is evaluated as a
formula when the file is opened in Excel, Google Sheets, or LibreOffice
Calc, not rendered as literal text. Several HERD CSV exports place
user-writable or admin-writable free text (topology names, device names,
port names, owner names, descriptions) into cells without checking for this,
so an authenticated caller who controls one of those values can plant a
formula (for example an ``=HYPERLINK(...)`` or a legacy DDE payload) that
runs when someone else opens the export.

``csv_safe_cell`` is the OWASP-recommended neutralization: prefix a single
quote onto a text value whose first character is a trigger. Spreadsheet
applications treat a leading ``'`` as a text-cell marker and never evaluate
what follows, and the character is not shown to the user. ``csv_unsafe_cell``
is the exact inverse, used by an importer that reads HERD's own CSV export
back in, so an exported value round-trips to the original byte-for-byte:
strip exactly one leading single quote, but only when the remainder still
begins with a trigger character, so a value that legitimately starts with an
apostrophe (for example a name like ``'quoted``) is read back unchanged
rather than losing its quote.

LEADING-WHITESPACE DECISION: a value that starts with one or more ASCII
space characters and then a trigger (for example ``" =1+1"``) is treated as
equally dangerous and neutralized. Some spreadsheet engines strip leading
spaces before deciding whether a cell is a formula, so a check that only
looks at character zero would miss that case. The quote is still prefixed to
the very front of the value (before the leading spaces), which is enough to
mark the whole cell as text and preserves the original spacing on the round
trip. A tab or carriage return as the leading whitespace is not separately
stripped for this check, because both are already trigger characters in
their own right and are caught directly.

Only strings are inspected. ``int``, ``float``, ``bool``, and ``None`` pass
through completely untouched, so a formatted numeric or boolean column (for
example an hours total or an ``exclusive`` flag) is never quoted, and a CSV
writer should call this helper only on the free-text columns it emits, never
on a column it already formats as a number or a fixed enumeration.
"""

from __future__ import annotations

from typing import Any

# The trigger set is OWASP's CSV-injection list: the four characters a
# spreadsheet reads as the start of a formula, plus tab and carriage return,
# which several spreadsheet engines also treat as formula-introducing in a
# quoted or copy-pasted context.
_TRIGGER_CHARS = ("=", "+", "-", "@", "\t", "\r")


def _leading_trigger(value: str) -> bool:
    """True if `value`, ignoring any leading ASCII spaces, starts with a trigger."""
    stripped = value.lstrip(" ")
    return bool(stripped) and stripped[0] in _TRIGGER_CHARS


def csv_safe_cell(value: Any) -> Any:
    """Neutralize a text cell that would otherwise open as a spreadsheet formula.

    A string whose first non-space character is a formula trigger is
    returned with a single quote prefixed to its original (unstripped)
    value. Every other string, and every non-string value (``int``,
    ``float``, ``bool``, ``None``), is returned unchanged.
    """
    if not isinstance(value, str):
        return value
    if _leading_trigger(value):
        return "'" + value
    return value


def csv_unsafe_cell(value: Any) -> Any:
    """Invert `csv_safe_cell` when reading a HERD-exported CSV back in.

    Strips exactly one leading single quote, but only when the character
    after it (ignoring leading spaces) is a formula trigger, i.e. only when
    the quote is one `csv_safe_cell` could have added. A value that starts
    with a single quote for its own reason (an apostrophe-led name) has no
    trigger character after the quote and is returned unchanged, so it is
    never mangled by an import.
    """
    if not isinstance(value, str) or not value.startswith("'"):
        return value
    rest = value[1:]
    if _leading_trigger(rest):
        return rest
    return value


__all__ = ["csv_safe_cell", "csv_unsafe_cell"]
