"""Ensure the unique index on device_config_versions (device_id, version_number).

Issue #1095. Migration 0013 created ix_device_config_versions_device_version, but
the model did not declare it, and herd_common.schema_init.create_all_and_stamp builds
a fresh schema from the models and stamps head. A schema born that way has no unique
index, so two concurrent creates for one device could both store the same number.

This revision adds the index wherever it is missing. A schema without it may already
hold duplicate numbers, so before creating the index every duplicate except the
earliest-created row of each (device_id, version_number) group is renumbered above the
device's current maximum, in creation order. No row is deleted and the earliest row
keeps its number. A schema built by 0013 already has the index and is left alone.

Downgrade is a no-op: 0013 owns the index, and the renumbering cannot be undone.

Revision ID: 0023
Revises: 0022
Create Date: 2026-10-08 00:00:00.000000
"""

import os

import sqlalchemy as sa
from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None

_schema = os.environ.get("DB_SCHEMA") or None
_INDEX = "ix_device_config_versions_device_version"


def upgrade() -> None:
    bind = op.get_bind()
    existing = {
        ix["name"] for ix in sa.inspect(bind).get_indexes("device_config_versions", schema=_schema)
    }
    if _INDEX in existing:
        return

    table = f"{_schema}.device_config_versions" if _schema else "device_config_versions"
    op.execute(
        sa.text(
            f"""
            WITH ranked AS (
                SELECT id, device_id, created_at,
                       row_number() OVER (
                           PARTITION BY device_id, version_number
                           ORDER BY created_at, id
                       ) AS rn
                FROM {table}
            ),
            extra AS (
                SELECT id, device_id,
                       row_number() OVER (
                           PARTITION BY device_id ORDER BY created_at, id
                       ) AS k
                FROM ranked
                WHERE rn > 1
            ),
            top AS (
                SELECT device_id, max(version_number) AS max_number
                FROM {table}
                GROUP BY device_id
            )
            UPDATE {table} AS v
            SET version_number = top.max_number + extra.k
            FROM extra JOIN top ON top.device_id = extra.device_id
            WHERE v.id = extra.id
            """
        )
    )
    op.create_index(
        _INDEX,
        "device_config_versions",
        ["device_id", "version_number"],
        unique=True,
        schema=_schema,
    )


def downgrade() -> None:
    pass
