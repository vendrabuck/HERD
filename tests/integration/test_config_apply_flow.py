"""Config versions, a scheduled dry run, and its confirmation against a running stack
(issues #1100 and #1104).

The device runs the checked-in `drivers/frr_mgmt` package. In a dry run that driver
records every command as `simulated` and opens no SSH session (its module docstring
and `configure`), so the whole flow needs no router: inventory validates the version
against the driver's published schema (execution loads the package to read it), the
apply scheduler fires the job through execution's `POST /execute/internal`, the run
records the transcript, and the confirm writes a real job that the test cancels before
it is due.

The scheduler fires a job only while its creator still has authority on the device
(CFG-SCHED-6), and an admin has no standing of its own there, so the admin books an
ACTIVE reservation of the device first and names it on the job; that is also the
reservation_id check of CFG-JOB-4 passing against the real reservations service.

`docker-compose.override.yml` runs the dev and gate stacks' scheduler every 2 seconds
(production default 30) so the job fires inside the suite's 30 second cap.
"""

from __future__ import annotations

import asyncio
import io
import json
import tarfile
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from ._device_teardown import delete_device_checked

pytestmark = pytest.mark.asyncio

FRR_MGMT_DIR = Path(__file__).resolve().parents[2] / "drivers" / "frr_mgmt"
COMMANDS = ["ip route 192.0.2.0/24 blackhole", "ip route 198.51.100.0/24 blackhole"]
TERMINAL = {"success", "failed", "skipped", "cancelled"}
ADMIN_MISMATCH_DETAIL = (
    "reservation_id must reference an active reservation that includes this device"
)


def _frr_mgmt_tarball() -> bytes:
    """Package the checked-in drivers/frr_mgmt directory for upload, unchanged."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name in ("driver.py", "driver_metadata.json"):
            tf.add(FRR_MGMT_DIR / name, arcname=name)
    return buf.getvalue()


@pytest.fixture(scope="session")
async def frr_mgmt_template(base_url, admin_token):
    """The frr_mgmt driver and a device template on it, once per session."""
    async with httpx.AsyncClient(
        base_url=base_url,
        verify=False,
        timeout=30.0,
        headers={"Authorization": f"Bearer {admin_token}"},
    ) as client:
        metadata = json.loads((FRR_MGMT_DIR / "driver_metadata.json").read_text())
        assert metadata["supports_dry_run"] is True
        drv = await client.post(
            "/inventory/drivers",
            files={"file": ("frr_mgmt.tar.gz", _frr_mgmt_tarball(), "application/gzip")},
            data={
                "name": f"int-frr-mgmt-{uuid.uuid4().hex[:8]}",
                "connection_type": "Management",
                "description": "integration: frr_mgmt for scheduled dry runs",
            },
        )
        drv.raise_for_status()
        driver = drv.json()
        assert driver["supports_dry_run"] is True
        tpl = await client.post(
            "/inventory/templates",
            json={
                "name": f"int-frr-mgmt-tpl-{uuid.uuid4().hex[:8]}",
                "template_type": "device",
                "driver_id": driver["id"],
                "vendor": "FRRouting",
                "model": "IntegrationRouter",
                "sections": [
                    {
                        "name": "General",
                        "fields": [{"key": "model", "label": "Model", "type": "string"}],
                    }
                ],
            },
        )
        tpl.raise_for_status()
        template = tpl.json()
        yield template
        await client.delete(f"/inventory/templates/{template['id']}")
        await client.delete(f"/inventory/drivers/{driver['id']}")


@pytest.fixture
async def frr_device(admin_client, frr_mgmt_template):
    resp = await admin_client.post(
        "/inventory/devices",
        json={
            "name": f"int-frr-dev-{uuid.uuid4().hex[:8]}",
            "template_id": frr_mgmt_template["id"],
            "topology_type": "PHYSICAL",
            "status": "AVAILABLE",
            "field_data": {"model": "test"},
        },
    )
    resp.raise_for_status()
    device = resp.json()
    try:
        yield device
    finally:
        # Deleting the device also removes its versions and apply jobs (CFG-VER-15).
        await delete_device_checked(admin_client, device["id"])


async def _book(admin_client, device_id: str) -> dict:
    now = datetime.now(timezone.utc)
    resp = await admin_client.post(
        "/reservations/",
        json={
            "device_ids": [device_id],
            "purpose": f"config apply flow {uuid.uuid4().hex[:6]}",
            "start_time": now.isoformat(),
            "end_time": (now + timedelta(hours=1)).isoformat(),
        },
    )
    assert resp.status_code == 201, resp.text
    reservation = resp.json()
    assert reservation["status"] == "ACTIVE", reservation
    return reservation


async def _wait_for_terminal(admin_client, job_id: str, budget_seconds: float) -> dict:
    deadline = time.monotonic() + budget_seconds
    while True:
        resp = await admin_client.get(f"/inventory/apply-jobs/{job_id}")
        assert resp.status_code == 200, resp.text
        job = resp.json()
        if job["status"] in TERMINAL or time.monotonic() >= deadline:
            return job
        await asyncio.sleep(0.5)


async def test_scheduled_dry_run_fires_and_its_confirm_queues_a_real_apply(
    admin_client, frr_device
):
    device_id = frr_device["id"]
    reservation = await _book(admin_client, device_id)
    promoted_id: str | None = None
    try:
        version = await admin_client.post(
            f"/inventory/devices/{device_id}/config-versions",
            json={"config": {"commands": COMMANDS}, "description": "int dry run"},
        )
        assert version.status_code == 201, version.text
        version_id = version.json()["id"]

        scheduled = await admin_client.post(
            f"/inventory/devices/{device_id}/config-versions/{version_id}/schedule",
            json={
                "scheduled_for": (datetime.now(timezone.utc) + timedelta(seconds=2)).isoformat(),
                "dry_run": True,
                "reservation_id": reservation["id"],
            },
        )
        assert scheduled.status_code == 201, scheduled.text
        job = scheduled.json()
        assert job["status"] == "pending"
        assert job["dry_run"] is True
        assert job["reservation_id"] == reservation["id"]

        fired = await _wait_for_terminal(admin_client, job["id"], budget_seconds=18)
        assert fired["status"] == "success", fired
        assert fired["error"] is None
        run_id = fired["run_id"]
        assert run_id

        run = await admin_client.get(f"/execution/runs/{run_id}")
        assert run.status_code == 200, run.text
        run_body = run.json()
        assert run_body["status"] == "SUCCESS", run_body
        assert run_body["action"] == "configure"
        assert run_body["reservation_id"] == reservation["id"]
        assert run_body["input_params"]["dry_run"] is True
        assert run_body["input_params"]["config_version_id"] == version_id

        commands = await admin_client.get(f"/execution/runs/{run_id}/commands")
        assert commands.status_code == 200, commands.text
        transcript = commands.json()
        assert [row["command"] for row in transcript] == COMMANDS
        assert {row["exit_status"] for row in transcript} == {"simulated"}

        confirmed = await admin_client.post(f"/inventory/apply-jobs/{job['id']}/confirm")
        assert confirmed.status_code == 201, confirmed.text
        promoted = confirmed.json()
        promoted_id = promoted["id"]
        assert promoted_id != job["id"]
        assert promoted["status"] == "pending"
        assert promoted["dry_run"] is False
        assert promoted["version_id"] == version_id
        assert promoted["reservation_id"] == reservation["id"]

        # The promoted job is due 10 seconds out; cancel it so nothing is pushed
        # to a router that does not exist. A 204 means it never fires (CFG-STATE-4).
        cancelled = await admin_client.delete(f"/inventory/apply-jobs/{promoted_id}")
        assert cancelled.status_code == 204, cancelled.text
        promoted_id = None

        source = await admin_client.get(f"/inventory/apply-jobs/{job['id']}")
        assert source.json()["status"] == "success"
        assert source.json()["dry_run"] is True
    finally:
        if promoted_id is not None:
            await admin_client.delete(f"/inventory/apply-jobs/{promoted_id}")
        await admin_client.delete(f"/reservations/{reservation['id']}")


async def test_schedule_refuses_a_reservation_that_does_not_hold_the_device(
    admin_client, fresh_devices
):
    """CFG-JOB-4 (issue #1104) against the real reservations service: an ACTIVE
    reservation of another device is refused on this one, and no job is written."""
    held, other = await fresh_devices(2)
    reservation = await _book(admin_client, held["id"])
    try:
        version = await admin_client.post(
            f"/inventory/devices/{other['id']}/config-versions",
            json={"config": {"hostname": "int-scope"}},
        )
        assert version.status_code == 201, version.text
        resp = await admin_client.post(
            f"/inventory/devices/{other['id']}/config-versions/{version.json()['id']}/schedule",
            json={
                "scheduled_for": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
                "reservation_id": reservation["id"],
            },
        )
        assert resp.status_code == 422, resp.text
        assert resp.json() == {"detail": ADMIN_MISMATCH_DETAIL}
        jobs = await admin_client.get(f"/inventory/devices/{other['id']}/apply-jobs")
        assert jobs.status_code == 200, jobs.text
        assert jobs.json()["total"] == 0
    finally:
        await admin_client.delete(f"/reservations/{reservation['id']}")
