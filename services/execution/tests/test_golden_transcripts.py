"""Golden-transcript regression suite (ROADMAP #16 iter 3 Stage 5).

Each JSON fixture under tests/golden_transcripts/ pins the exact per-command
transcript a driver should emit for a specific (driver, action, kwargs,
dry_run) tuple. The parametrized test below runs each tuple through the
real driver_sandbox and asserts the captured transcript matches.

The intent is a "tested without hardware" regression net: if the mock_ios
driver's CLI translation drifts (e.g. someone changes "switchport mode
trunk" to "switchport mode trk"), the golden tests fail loudly. Someone
either fixes the regression or, if the change was intentional, regenerates
the fixtures with `python tests/regenerate_golden_transcripts.py`.

The transcripts only compare {seq, command, response, exit_status}.
Timing-dependent fields (duration_ms, created_at, run id) are excluded.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from app.services.driver_sandbox import execute_driver_method

FIXTURE_DIR = Path(__file__).parent / "golden_transcripts"
DRIVER_ROOT = Path(__file__).parent / "fixtures" / "drivers"


def _load_fixtures() -> list[tuple[str, dict]]:
    """Discover golden fixtures. Returns [(fixture_id, payload), ...]
    suitable for parametrize ids.
    """
    if not FIXTURE_DIR.exists():
        return []
    out: list[tuple[str, dict]] = []
    for path in sorted(FIXTURE_DIR.glob("*.json")):
        with path.open(encoding="utf-8") as f:
            payload = json.load(f)
        out.append((path.stem, payload))
    return out


def _materialize_driver(driver_name: str, tmp_path: Path) -> str:
    """Copy a fixture driver package to a fresh temp dir and return the path.

    The sandbox sets PYTHONPATH to this dir; copying isolates each test from
    bytecode caches in the source tree.
    """
    src = DRIVER_ROOT / driver_name
    if not src.exists():
        raise FileNotFoundError(f"unknown fixture driver: {driver_name}")
    dest = tmp_path / driver_name
    shutil.copytree(src, dest)
    return str(dest)


def _strip_volatile(transcript: list[dict]) -> list[dict]:
    """Drop fields that vary run-to-run before comparing against goldens."""
    out: list[dict] = []
    for idx, row in enumerate(transcript, start=1):
        out.append(
            {
                "seq": idx,
                "command": row.get("command"),
                "response": row.get("response"),
                "exit_status": row.get("exit_status", "ok"),
            }
        )
    return out


@pytest.mark.parametrize(
    ("fixture_id", "fixture"),
    _load_fixtures(),
    ids=lambda v: v if isinstance(v, str) else "",
)
def test_golden_transcript(fixture_id: str, fixture: dict, tmp_path: Path):
    """The transcript a driver emits must match its golden fixture exactly.

    Each fixture file is canonical. Drift means either the driver was
    intentionally changed (regenerate goldens) or the driver regressed
    (find and fix).
    """
    driver_name = fixture["driver"]
    action = fixture["action"]
    method_kwargs = fixture.get("method_kwargs", {}) or {}
    dry_run = bool(fixture.get("dry_run", False))
    expected = fixture["expected_commands"]

    driver_path = _materialize_driver(driver_name, tmp_path)
    metadata_path = Path(driver_path) / "driver_metadata.json"
    driver_metadata = (
        json.loads(metadata_path.read_text(encoding="utf-8")) if metadata_path.exists() else {}
    )

    result = execute_driver_method(
        driver_path=driver_path,
        action=action,
        context={},
        method_kwargs=method_kwargs or None,
        dry_run=dry_run,
        driver_metadata=driver_metadata,
    )
    assert result["success"] is True, f"driver failed: {result.get('error')}"

    actual = _strip_volatile(result.get("transcript") or [])
    assert actual == expected, (
        f"\nFixture {fixture_id} drift detected.\n"
        f"Expected: {json.dumps(expected, indent=2)}\n"
        f"Actual:   {json.dumps(actual, indent=2)}\n"
        f"If the change is intentional, run: "
        f"python services/execution/tests/regenerate_golden_transcripts.py"
    )


def test_fixture_directory_exists():
    """Sanity: the parametrized test silently no-ops if the dir is missing."""
    assert FIXTURE_DIR.exists(), (
        f"Golden fixtures missing at {FIXTURE_DIR}. "
        "Run regenerate_golden_transcripts.py to bootstrap them."
    )


def test_fixture_set_covers_each_l2_action():
    """The mock_ios driver implements the full L2 contract; every method
    that emits commands should have at least one golden in real mode.
    Coverage signal, not a hard assertion of how many goldens exist."""
    fixtures = _load_fixtures()
    if not fixtures:
        pytest.skip("no fixtures present yet")
    mock_ios = [f for _id, f in fixtures if f.get("driver") == "mock_ios"]
    actions = {f["action"] for f in mock_ios if not f.get("dry_run")}
    # login + logout are also covered indirectly via dry-run pairs; this
    # asserts the *mutating* actions are pinned in real mode.
    required = {"create_vlan", "add_to_vlan", "remove_from_vlan", "delete_vlan"}
    missing = required - actions
    assert not missing, f"mock_ios actions without a real-mode golden: {missing}"
