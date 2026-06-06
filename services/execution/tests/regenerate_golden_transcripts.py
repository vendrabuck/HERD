"""Regenerate the golden-transcript fixtures.

WHEN TO RUN: only when a driver under tests/fixtures/drivers/ has been
intentionally changed and the new transcript shape is the new truth.
Running blindly will mask real driver regressions.

Usage from the execution service root (services/execution):
    uv run python tests/regenerate_golden_transcripts.py

Each entry in CASES defines a (driver, action, kwargs, dry_run) tuple.
The script runs each one through the real sandbox, captures the actual
transcript, and writes a JSON fixture under tests/golden_transcripts/.
Existing fixture files are overwritten in place; review the diff in your
VCS before committing.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

# Make app.services importable when the script is launched outside pytest.
# Python prepends the script's parent dir (tests/) to sys.path[0], which does
# not contain the app/ package. Force-prepend the service root unconditionally
# so `from app...` resolves before any other on-path service shadows it.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Settings need minimal env to import cleanly outside the test harness.
import os  # noqa: E402

os.environ.setdefault("DB_SCHEMA", "")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("SECRET_KEY", "regen-secret")
os.environ.setdefault("INTERNAL_API_TOKEN", "regen-token")

from app.services.driver_sandbox import execute_driver_method  # noqa: E402

FIXTURE_DIR = Path(__file__).parent / "golden_transcripts"
DRIVER_ROOT = Path(__file__).parent / "fixtures" / "drivers"


# Add a new entry here when you add a new (driver, action, payload) case.
# Fixture filenames derive from the `id` field; keep them lowercase + _-_-_.
CASES = [
    {
        "id": "mock_ios_login",
        "driver": "mock_ios",
        "action": "login",
        "method_kwargs": {},
        "dry_run": False,
    },
    {
        "id": "mock_ios_logout",
        "driver": "mock_ios",
        "action": "logout",
        "method_kwargs": {},
        "dry_run": False,
    },
    {
        "id": "mock_ios_create_vlan",
        "driver": "mock_ios",
        "action": "create_vlan",
        "method_kwargs": {"vlan_id": 100},
        "dry_run": False,
    },
    {
        "id": "mock_ios_create_vlan_dry_run",
        "driver": "mock_ios",
        "action": "create_vlan",
        "method_kwargs": {"vlan_id": 100},
        "dry_run": True,
    },
    {
        "id": "mock_ios_add_to_vlan_tagged",
        "driver": "mock_ios",
        "action": "add_to_vlan",
        "method_kwargs": {"port": "GigabitEthernet0/1", "vlan_id": 100, "tag": "tagged"},
        "dry_run": False,
    },
    {
        "id": "mock_ios_add_to_vlan_access",
        "driver": "mock_ios",
        "action": "add_to_vlan",
        "method_kwargs": {"port": "GigabitEthernet0/2", "vlan_id": 200, "tag": "access"},
        "dry_run": False,
    },
    {
        "id": "mock_ios_remove_from_vlan",
        "driver": "mock_ios",
        "action": "remove_from_vlan",
        "method_kwargs": {"port": "GigabitEthernet0/1", "vlan_id": 100},
        "dry_run": False,
    },
    {
        "id": "mock_ios_delete_vlan",
        "driver": "mock_ios",
        "action": "delete_vlan",
        "method_kwargs": {"vlan_id": 100},
        "dry_run": False,
    },
    {
        "id": "mock_ios_status",
        "driver": "mock_ios",
        "action": "status",
        "method_kwargs": {},
        "dry_run": False,
    },
]


def _materialize(driver_name: str, dest_parent: Path) -> str:
    src = DRIVER_ROOT / driver_name
    dest = dest_parent / driver_name
    shutil.copytree(src, dest)
    return str(dest)


def _strip_volatile(transcript: list[dict]) -> list[dict]:
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


def _run_case(case: dict) -> dict:
    driver_name = case["driver"]
    with tempfile.TemporaryDirectory(prefix="herd_regen_") as tmp:
        driver_path = _materialize(driver_name, Path(tmp))
        metadata_path = Path(driver_path) / "driver_metadata.json"
        driver_metadata = (
            json.loads(metadata_path.read_text(encoding="utf-8")) if metadata_path.exists() else {}
        )
        result = execute_driver_method(
            driver_path=driver_path,
            action=case["action"],
            context={},
            method_kwargs=case.get("method_kwargs") or None,
            dry_run=bool(case.get("dry_run", False)),
            driver_metadata=driver_metadata,
        )
    if not result["success"]:
        raise RuntimeError(f"case {case['id']!r} failed in sandbox: {result.get('error')}")
    return {
        "driver": driver_name,
        "action": case["action"],
        "method_kwargs": case.get("method_kwargs", {}),
        "dry_run": bool(case.get("dry_run", False)),
        "expected_commands": _strip_volatile(result.get("transcript") or []),
    }


def main() -> int:
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    written = 0
    for case in CASES:
        fixture = _run_case(case)
        out_path = FIXTURE_DIR / f"{case['id']}.json"
        out_path.write_text(json.dumps(fixture, indent=2) + "\n", encoding="utf-8")
        written += 1
        print(f"wrote {out_path.name}: {len(fixture['expected_commands'])} commands")
    print(f"\nregenerated {written} fixtures under {FIXTURE_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
