"""Add reservation_fork.draft_restored_from_id for restore-to-draft (issue #622).

Restore-to-draft never appends a fork_versions row (a version is a save, per the
canvas PUT docstring and the wiring-heal reconciler's invariant that a fork_versions
row means something was reconciled). Instead the fork row itself carries a marker
naming the version its draft was last restored from; the next save consumes the
marker onto the ForkVersion it appends (restored_from_id) and clears it, while a
loose canvas PUT between a restore and a save leaves it in place (the user is still
editing the restored draft). Purely additive: a single nullable column, FK to
fork_versions.id within this schema, ON DELETE SET NULL so a pruned version does not
block a delete. Downgrade drops the column.

Revision ID: 0009
Revises: 0008
"""

import os

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

_schema = os.environ.get("DB_SCHEMA") or None


def _qualified(table: str) -> str:
    return f"{_schema}.{table}" if _schema else table


def upgrade() -> None:
    op.add_column(
        "reservation_fork",
        sa.Column(
            "draft_restored_from_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey(
                f"{_qualified('fork_versions')}.id",
                ondelete="SET NULL",
                name="fk_reservation_fork_draft_restored_from_id",
            ),
            nullable=True,
        ),
        schema=_schema,
    )


def downgrade() -> None:
    op.drop_column("reservation_fork", "draft_restored_from_id", schema=_schema)
