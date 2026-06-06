"""Schemas for bulk import and export of topologies.

The per-row import report mirrors the inventory service's report so the two
import surfaces are consistent for the frontend. A row outcome is one of create,
update, skip, or reject; a reject carries a reason and never aborts the batch.

Identity key (cross-instance reference resolution):

- topology: `name`. Device references inside the canvas resolve by device
  `name` (looked up against the target instance's inventory), since UUIDs do not
  match across instances.
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
