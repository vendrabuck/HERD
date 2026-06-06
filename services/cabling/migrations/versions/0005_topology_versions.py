"""Create topology_versions table with backfill.

Revision ID: 0005
Revises: 0004
"""

import os
import uuid

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

_schema = os.environ.get("DB_SCHEMA") or None


def _qualified(table: str) -> str:
    return f"{_schema}.{table}" if _schema else table


def upgrade() -> None:
    op.create_table(
        "topology_versions",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column(
            "topology_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey(f"{_qualified('topologies')}.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("canvas_data", sa.JSON(), nullable=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_by", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("author_name", sa.String(150), nullable=False, server_default=""),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "restored_from_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey(f"{_qualified('topology_versions')}.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.UniqueConstraint(
            "topology_id", "version_number", name="uq_topology_versions_topology_version"
        ),
        schema=_schema,
    )

    bind = op.get_bind()
    topologies_table = _qualified("topologies")
    versions_table = _qualified("topology_versions")

    rows = bind.execute(
        sa.text(
            f"SELECT id, name, created_by, owner_name, canvas_data, updated_at "
            f"FROM {topologies_table} WHERE canvas_data IS NOT NULL"
        )
    ).fetchall()

    if rows:
        insert_sql = sa.text(
            f"INSERT INTO {versions_table} "
            f"(id, topology_id, version_number, canvas_data, name, description, "
            f"created_by, author_name, created_at) "
            f"VALUES (:id, :topology_id, 1, CAST(:canvas_data AS JSON), :name, "
            f":description, :created_by, :author_name, :created_at)"
        )
        import json

        for row in rows:
            canvas = row[4]
            if canvas is not None and not isinstance(canvas, str):
                canvas = json.dumps(canvas)
            bind.execute(
                insert_sql,
                {
                    "id": uuid.uuid4(),
                    "topology_id": row[0],
                    "canvas_data": canvas,
                    "name": row[1],
                    "description": "Initial snapshot",
                    "created_by": row[2],
                    "author_name": row[3] or "",
                    "created_at": row[5],
                },
            )


def downgrade() -> None:
    op.drop_table("topology_versions", schema=_schema)
