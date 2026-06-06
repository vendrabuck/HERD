"""Normalize device lists into a reservation_devices join table.

Revision ID: 0008
Revises: 0007

Creates the `reservation_devices` join table (the single source of truth for a
reservation's devices), backfills it from the existing JSONB `device_ids`
column, then drops that column and the GIN index added in migration 0005.

The backfill is Postgres-only: SQLite is the unit-test backend (schema is built
from the models via Base.metadata.create_all, not these migrations) and carries
no production data to migrate.
"""

import os

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

_schema = os.environ.get("DB_SCHEMA") or None


def _qualified(table: str) -> str:
    return f"{_schema}.{table}" if _schema else table


def upgrade() -> None:
    reservations_ref = _qualified("reservations")

    op.create_table(
        "reservation_devices",
        sa.Column(
            "reservation_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey(f"{reservations_ref}.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("device_id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("added_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("reservation_id", "device_id"),
        schema=_schema,
    )
    op.create_index(
        "ix_reservation_devices_device_id",
        "reservation_devices",
        ["device_id"],
        schema=_schema,
    )

    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        rd_ref = _qualified("reservation_devices")
        # device_ids is JSONB after migration 0005. jsonb_array_elements_text
        # yields one UUID-string row per array element; cast it to uuid.
        op.execute(
            f"""
            INSERT INTO {rd_ref} (reservation_id, device_id)
            SELECT r.id, elem::uuid
            FROM {reservations_ref} r,
                 jsonb_array_elements_text(r.device_ids) AS elem
            ON CONFLICT DO NOTHING
            """
        )
        op.execute(f"DROP INDEX IF EXISTS {_qualified('ix_reservations_device_ids')}")

    op.drop_column("reservations", "device_ids", schema=_schema)


def downgrade() -> None:
    reservations_ref = _qualified("reservations")
    bind = op.get_bind()

    col_type = sa.JSON().with_variant(JSONB, "postgresql")
    # Re-add nullable first so we can backfill, then tighten to NOT NULL.
    op.add_column(
        "reservations",
        sa.Column("device_ids", col_type, nullable=True),
        schema=_schema,
    )

    if bind.dialect.name == "postgresql":
        rd_ref = _qualified("reservation_devices")
        op.execute(
            f"""
            UPDATE {reservations_ref} r
            SET device_ids = COALESCE(
                (SELECT jsonb_agg(rd.device_id::text)
                 FROM {rd_ref} rd
                 WHERE rd.reservation_id = r.id),
                '[]'::jsonb
            )
            """
        )
        op.execute(
            f"CREATE INDEX IF NOT EXISTS ix_reservations_device_ids "
            f"ON {reservations_ref} USING GIN (device_ids jsonb_path_ops)"
        )

    op.alter_column("reservations", "device_ids", nullable=False, schema=_schema)
    op.drop_index(
        "ix_reservation_devices_device_id",
        table_name="reservation_devices",
        schema=_schema,
    )
    op.drop_table("reservation_devices", schema=_schema)
