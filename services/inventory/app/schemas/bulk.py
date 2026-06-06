"""Schemas for bulk import and export of devices and templates.

The import report is shared across resource types: every import endpoint, in
dry-run or commit mode, returns a `BulkImportReport` whose `rows` carry one
`RowResult` per input row. A row outcome is one of:

- create: the row would create (dry-run) or created (commit) a new resource.
- update: the row matched an existing resource by identity and would update
  (dry-run) or updated (commit) it.
- skip: the row matched an existing resource and is byte-identical, so no write
  is needed.
- reject: the row failed validation or reference resolution; `reason` explains
  why. A reject never aborts the batch; remaining rows are still processed.

Identity keys (used for cross-instance reference resolution, since UUIDs do not
match across instances):

- device: `name` (unique). Its `template_id` reference resolves by template `name`.
- template: `name` (unique). Its `driver_id` reference resolves by driver `name`.
"""

from typing import Literal

from pydantic import BaseModel

RowAction = Literal["create", "update", "skip", "reject"]


class RowResult(BaseModel):
    row: int
    action: RowAction
    identity: str | None = None
    reason: str | None = None


class BulkImportReport(BaseModel):
    dry_run: bool
    total: int
    created: int
    updated: int
    skipped: int
    rejected: int
    rows: list[RowResult]
