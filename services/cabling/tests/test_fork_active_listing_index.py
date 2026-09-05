"""ReservationFork carries the ACTIVE-listing partial index (issue #710).

GET /internal/forks filters status = 'ACTIVE' and orders by created_at, and
reservations' expiration sweep drains every page of it every tick against a table
that grows without bound (fork rows are never deleted). Pins the index directly
against the model's table metadata, the same way execution's partial-unique indexes
are pinned in test_route_service.py, rather than relying on a functional query-plan
assertion SQLite cannot give us.
"""

from app.models.fork import ReservationFork


def _find_index(name: str):
    for index in ReservationFork.__table__.indexes:
        if index.name == name:
            return index
    return None


def test_active_created_at_index_present():
    index = _find_index("ix_reservation_fork_active_created_at")
    assert index is not None, "expected ix_reservation_fork_active_created_at on reservation_fork"


def test_active_created_at_index_covers_created_at_then_id():
    index = _find_index("ix_reservation_fork_active_created_at")
    columns = [c.name for c in index.columns]
    assert columns == ["created_at", "id"], (
        "the listing orders by created_at and needs id as a stable pagination "
        "tie-breaker for rows sharing a timestamp"
    )


def test_active_created_at_index_is_partial_on_active_status():
    index = _find_index("ix_reservation_fork_active_created_at")
    postgresql_where = index.dialect_options["postgresql"]["where"]
    sqlite_where = index.dialect_options["sqlite"]["where"]
    assert postgresql_where is not None
    assert sqlite_where is not None
    assert str(postgresql_where) == "status = 'ACTIVE'"
    assert str(sqlite_where) == "status = 'ACTIVE'"
