"""Static pin: every live-Postgres suite is actually run by the gate phase.

No database, no stack, no Docker: this only parses the Makefile, in the style of
tests/unit/test_nos_lab_ci_wiring.py.

A `*_live_pg.py` suite skips itself when no Postgres is reachable, which is what
makes it safe to leave in the tree, and also what makes it invisible when nobody
wires it up: it would be written, reviewed, merged, and then silently never run.
The `_gate-pg-live-tests` phase of `make master` and `make everything` is the only
place these suites execute with HERD_TEST_PG_REQUIRED=1, so membership in that
recipe is the thing worth pinning.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MAKEFILE_PATH = REPO_ROOT / "Makefile"
SERVICES_DIR = REPO_ROOT / "services"


def _gate_recipe() -> str:
    """The body of the _gate-pg-live-tests recipe (its indented lines)."""
    lines = MAKEFILE_PATH.read_text().splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("_gate-pg-live-tests:"))
    body: list[str] = []
    for line in lines[start + 1 :]:
        if line and not line.startswith(("\t", " ", "#")):
            break
        body.append(line)
    return "\n".join(body)


def _live_pg_suites() -> list[Path]:
    return sorted(SERVICES_DIR.glob("*/tests/*_live_pg.py"))


def test_every_live_pg_suite_is_in_the_gate_phase():
    recipe = _gate_recipe()
    missing = [p for p in _live_pg_suites() if p.name not in recipe]
    assert not missing, (
        "these live-Postgres suites are in no gate phase, so they never run with a "
        "real database: " + ", ".join(str(p.relative_to(REPO_ROOT)) for p in missing)
    )


def test_the_gate_phase_names_only_suites_that_exist():
    recipe = _gate_recipe()
    named = set(re.findall(r"tests/(\w+_live_pg\.py)", recipe))
    on_disk = {p.name for p in _live_pg_suites()}
    assert named - on_disk == set(), "the gate phase names a live-pg suite that is gone"


def test_every_gate_suite_runs_hard_required():
    """HERD_TEST_PG_REQUIRED=1 is what turns "no Postgres" from a silent skip into a
    gate failure; a line without it would pass the gate while proving nothing."""
    recipe = _gate_recipe()
    for line in recipe.splitlines():
        if "_live_pg.py" in line:
            continue
        if "uv run pytest" in line and "_live_pg" in line:
            assert "HERD_TEST_PG_REQUIRED=1" in line, line
    # The env is set on the `(cd ...` line preceding each pytest line, so check pairs.
    chunks = [c for c in recipe.split("&&") if "_live_pg.py" in c]
    assert chunks, "the gate phase runs no live-pg suites at all"
    for chunk in chunks:
        assert "HERD_TEST_PG_REQUIRED=1" in chunk, chunk
