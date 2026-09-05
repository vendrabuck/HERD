"""Index assistant_conversations on (reservation_id, created_at) (issue #712).

The purpose-classification transcript read
(app/services/purpose_signals.py, _gather_transcripts_block) filters
assistant_conversations on reservation_id alone and orders by created_at.
The only existing index touching reservation_id is the composite
(user_id, reservation_id) from 0001, which cannot serve a reservation_id-only
predicate as an access path. That composite is kept for now and dropped in a
follow-up once nothing else can regress onto it.

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-05 00:00:00.000000
"""

import os

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

_schema = os.environ.get("DB_SCHEMA") or None


def upgrade() -> None:
    op.create_index(
        "ix_assistant_conversations_reservation_created",
        "assistant_conversations",
        ["reservation_id", "created_at"],
        schema=_schema,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_assistant_conversations_reservation_created",
        table_name="assistant_conversations",
        schema=_schema,
    )
