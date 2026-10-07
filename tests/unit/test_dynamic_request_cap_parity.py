"""Static pin: the frontend's dynamic-request cap equals the backend's (issue #998).

`ReservationCreate.dynamic_requests` in services/reservations/app/schemas/reservation.py
carries `max_length=50` (rule RES-DYN-2), and
frontend/src/components/reservations/CreateReservationModal.tsx mirrors it as
`MAX_DYNAMIC_REQUESTS` to disable the "add" control at the cap. A drift either lets the
form build a request the API refuses with 422 or refuses one the API would accept.

No import of the reservations package (every service names its package `app`); the
schema is read with `ast` and the TypeScript constant with a regular expression, the
same static posture as tests/unit/test_poll_floor_parity.py.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = REPO_ROOT / "services/reservations/app/schemas/reservation.py"
MODAL_PATH = REPO_ROOT / "frontend/src/components/reservations/CreateReservationModal.tsx"


def _backend_cap() -> int:
    tree = ast.parse(SCHEMA_PATH.read_text())
    for cls in ast.walk(tree):
        if not (isinstance(cls, ast.ClassDef) and cls.name == "ReservationCreate"):
            continue
        for stmt in cls.body:
            if (
                isinstance(stmt, ast.AnnAssign)
                and isinstance(stmt.target, ast.Name)
                and stmt.target.id == "dynamic_requests"
                and isinstance(stmt.value, ast.Call)
            ):
                for kw in stmt.value.keywords:
                    if kw.arg == "max_length":
                        return ast.literal_eval(kw.value)
    raise AssertionError(
        f"ReservationCreate.dynamic_requests max_length not found in {SCHEMA_PATH}"
    )


def _frontend_cap() -> int:
    match = re.search(r"^const MAX_DYNAMIC_REQUESTS = (\d+);$", MODAL_PATH.read_text(), re.M)
    assert match, f"MAX_DYNAMIC_REQUESTS not found in {MODAL_PATH}"
    return int(match.group(1))


def test_dynamic_request_cap_is_fifty_on_both_sides():
    assert _backend_cap() == 50
    assert _frontend_cap() == _backend_cap(), (
        f"{MODAL_PATH.name} MAX_DYNAMIC_REQUESTS={_frontend_cap()} but "
        f"{SCHEMA_PATH.name} ReservationCreate.dynamic_requests max_length={_backend_cap()}"
    )
