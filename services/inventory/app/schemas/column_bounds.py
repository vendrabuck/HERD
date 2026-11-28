"""Request field types bounded by the columns their values are written to.

A value that passes an inventory request schema is one its column accepts
(docs/specs/inventory.md section 8.17):

- a string field written to a `String(n)` column is at most `n` characters, equal to
  the column's length in `app/models`, an equality `test_column_bounds.py` pins
  (`column_string`);
- an integer field written to an `Integer` column is within Postgres's `integer`
  range (`INT32_MAX`);
- a string field written to a text or varchar column holds no NUL character,
  which neither type can store (`ColumnText`, `column_string`);
- a value written to a `json` column holds no number that is not finite (NaN,
  Infinity, or -Infinity), which the column's input refuses (`JsonObject`,
  `refuse_non_finite`). A `json` column stores a NUL and any string, so nothing
  else in such a value is bounded here.

Each refusal is the ordinary 422 item naming the field, with fixed wording.
"""

import math
from typing import Annotated, Any

from pydantic import AfterValidator, Field

# Postgres `integer` is a signed 32-bit integer.
INT32_MAX = 2**31 - 1

NUL_CHARACTER_MESSAGE = "must not contain a NUL character"
NON_FINITE_NUMBER_MESSAGE = "must not hold a number that is not finite (NaN or Infinity)"


def _refuse_nul(value: str) -> str:
    if "\x00" in value:
        raise ValueError(NUL_CHARACTER_MESSAGE)
    return value


def _holds_non_finite(value: Any) -> bool:
    """True when `value`, or anything nested in it, is a float that is not finite.
    Walks with its own stack, so a deeply nested value cannot exhaust the
    interpreter's recursion limit."""
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, float):
            if not math.isfinite(item):
                return True
        elif isinstance(item, dict):
            stack.extend(item.values())
        elif isinstance(item, (list, tuple)):
            stack.extend(item)
    return False


def refuse_non_finite(value: Any) -> Any:
    """The validator for a value written to a `json` column."""
    if _holds_non_finite(value):
        raise ValueError(NON_FINITE_NUMBER_MESSAGE)
    return value


# A string written to a `Text` column: no length bound of its own.
ColumnText = Annotated[str, AfterValidator(_refuse_nul)]

# A JSON object written to a `json` column (`field_data`).
JsonObject = Annotated[dict[str, Any], AfterValidator(refuse_non_finite)]


def column_string(length: int, *, min_length: int | None = None) -> Any:
    """A string written to a `String(length)` column."""
    return Annotated[
        str, Field(min_length=min_length, max_length=length), AfterValidator(_refuse_nul)
    ]
