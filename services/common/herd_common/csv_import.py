"""The one CSV reader for an uploaded import file (issue #1574).

Inventory's device and template importers and cabling's topology importer read a
caller's CSV through ``read_csv_rows``. Three things make it safe for any text a
client can upload:

- The text is opened with ``newline=""``, the csv module's documented way to read
  input, so ``\\n``, ``\\r\\n``, and bare ``\\r`` line endings are all read as line
  ends (a file saved with classic Mac line endings imports), a bare ``\\r`` in an
  unquoted cell ends the line the way a spreadsheet reads it, and a line break
  inside a quoted cell is kept.
- The csv module's field limit (process-wide, 131,072 characters by default) is
  raised for the read to the length of the text, so no field of the file can pass
  it: a cell is never longer than the text it sits in. The previous limit is put
  back before the function returns or raises. The read runs without an ``await``,
  so no other coroutine on the event loop runs while the raised limit is in place;
  a reader on another thread could see it, and the services that call this read
  no other CSV.
- Anything the csv module still refuses (``csv.Error``) answers 422 with a fixed
  detail naming the physical line the reader had reached, never the module's own
  text and never a cell.
"""

from __future__ import annotations

import csv
import io

from fastapi import HTTPException

# The 422 detail for a CSV the csv module cannot read. `{line}` is the number of
# physical lines the reader had read when it stopped, the header being line 1; it is
# a count, never text from the file.
CSV_UNREADABLE_DETAIL = "Invalid CSV: line {line} could not be read; fix it and retry"


def _field_limit_for(text: str) -> int:
    """The field limit for reading ``text``: never below the module's current one,
    and never below the length of the text, so no field of ``text`` can exceed it."""
    return max(csv.field_size_limit(), len(text))


def read_csv_rows(text: str) -> list[dict]:
    """Read ``text`` (an already decoded import file) as CSV with a header row.

    Returns one dict per data row, keyed by the header, as ``csv.DictReader``
    builds it (a short row's missing cells are ``None``; a long row's extra cells
    sit under the key ``None``). Raises ``HTTPException(422)`` with
    ``CSV_UNREADABLE_DETAIL`` when the csv module refuses the text.
    """
    previous = csv.field_size_limit(_field_limit_for(text))
    reader = csv.DictReader(io.StringIO(text, newline=""))
    try:
        return list(reader)
    except csv.Error:
        # `reader.line_num` is updated only after a row is returned; the inner
        # reader's count includes the line it was reading when it refused.
        raise HTTPException(
            status_code=422, detail=CSV_UNREADABLE_DETAIL.format(line=reader.reader.line_num)
        ) from None
    finally:
        csv.field_size_limit(previous)


__all__ = ["CSV_UNREADABLE_DETAIL", "read_csv_rows"]
