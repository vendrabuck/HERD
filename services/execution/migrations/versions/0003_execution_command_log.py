"""Create execution_command_log table for per-command driver transcripts.

Revision ID: 0003
Revises: 0002
Create Date: 2026-05-26 00:00:00.000000
"""

import os

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

_schema = os.environ.get("DB_SCHEMA") or None
_run_fk = f"{_schema}.execution_runs.id" if _schema else "execution_runs.id"


def upgrade() -> None:
    op.create_table(
        "execution_command_log",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column(
            "run_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey(_run_fk, ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("command", sa.Text(), nullable=False),
        sa.Column("response", sa.Text(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("exit_status", sa.String(20), nullable=False, server_default="ok"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("run_id", "seq", name="uq_command_log_run_seq"),
        schema=_schema,
    )

    op.create_index(
        "ix_command_log_run_id",
        "execution_command_log",
        ["run_id"],
        schema=_schema,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_command_log_run_id",
        table_name="execution_command_log",
        schema=_schema,
    )
    op.drop_table("execution_command_log", schema=_schema)
